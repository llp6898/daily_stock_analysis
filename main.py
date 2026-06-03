"""
A股自选股智能分析系统 - API 服务入口

Flask API Server + 全量扫描定时任务
"""

from klines_route import klines_bp

import os, sys, json, time, logging, threading
from flask import Flask, jsonify, request, send_from_directory, redirect, render_template_string
from datetime import datetime
import requests
import tushare as ts
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== Flask App ==========
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stock-api")

# ========== CORS 跨域支持 ==========
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/favicon.ico")
def favicon():
    return "", 204

API_PORT = int(os.environ.get("PORT") or os.environ.get("API_PORT", 8000))
FEISHU_BOT_KEY = os.environ.get("FEISHU_BOT_KEY", "")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "b2d323ce6e8bf2c1549a72fd08538c1dc1ac4bf563550632c1a01759")
STOCK_LIST = os.environ.get("STOCK_LIST", "600162,301581,600143,601985").split(",")

pro = ts.pro_api(TUSHARE_TOKEN)

# ========== 飞书通知 ==========
def send_feishu(text):
    if not FEISHU_BOT_KEY:
        log.warning("FEISHU_BOT_KEY not set, skip")
        return
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_BOT_KEY}"
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error(f"Feishu failed: {e}")

# ========== 指标计算 ==========
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    delta = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gain = [max(d, 0) for d in delta]
    loss = [abs(min(d, 0)) for d in delta]
    avg_gain = sum(gain[-period:]) / period
    avg_loss = sum(loss[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)

def calc_macd(closes):
    if len(closes) < 30:
        return None, None, None
    ef = sum(closes[-12:]) / 12
    es = sum(closes[-26:]) / 26
    dif = ef - es
    dea = dif * 0.8
    macd_bar = 2 * (dif - dea)
    return round(dif, 4), round(dea, 4), round(macd_bar, 4)

def calc_ma(closes, n):
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)

# ========== SMC聪明钱 ==========
def smc_score(closes, highs, lows):
    if len(closes) < 60:
        return 0, "数据不足"
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    price = closes[-1]
    swing_highs = [highs[i] for i in range(5, len(highs)-5)
                   if highs[i] == max(highs[i-5:i+6])]
    swing_lows = [lows[i] for i in range(5, len(highs)-5)
                  if lows[i] == min(lows[i-5:i+6])]
    if not swing_highs or not swing_lows:
        return 0, "无结构"
    last_low = min(swing_lows[-3:])
    last_high = max(swing_highs[-3:])
    score = 0
    reason = []
    if price < ma20:
        score += 2; reason.append("价格<MA20")
    if price > ma20:
        score += 3; reason.append("价格>MA20")
    if price < ma60:
        score += 2; reason.append("价格<MA60")
    if ma20 > ma60:
        score += 3; reason.append("MA20>MA60多头")
    if price >= last_low * 0.98:
        score += 4; reason.append("接近支撑")
    if len(swing_lows) >= 3:
        swing_range = last_high - last_low
        pos = (price - last_low) / swing_range if swing_range > 0 else 0.5
        if pos < 0.3:
            score += 5; reason.append("低位支撑区")
        elif pos > 0.7:
            score += 2; reason.append("高位风险区")
    return min(score, 15), "; ".join(reason) if reason else "中性"

# ========== 新优选公式 v2.0 ==========
def new_formula_score(v):
    score = 0
    reasons = []
    if v["vr"] >= 1.8: score += 2; reasons.append("VR放量")
    if 45 <= v["rsi"] <= 68: score += 2; reasons.append("RSI最佳区间")
    if v["rsi"] > 75: score -= 2; reasons.append("RSI高位")
    dif, dea, macd_bar = v.get("macd_dif"), v.get("macd_dea"), v.get("macd_bar")
    if dif is not None and dea is not None and macd_bar is not None and dif < 0.1 and macd_bar > 0:
        score += 3; reasons.append("MACD低位金叉")
    if v["ma多头"]: score += 2; reasons.append("MA多头排列")
    if v["价格在MA20上"]: score += 1; reasons.append("价格在MA20上")
    if v["连续阳线"] >= 3: score += 2; reasons.append("连续阳线")
    if v["g60"] > 60: score -= 3; reasons.append("60日涨幅过大")
    return score, reasons

# ========== 获取持仓股RSI/MA ==========
def get_stock_indicators(code):
    try:
        df = ts.pro_bar(ts_code=code, freq="D", start_date="20250101", adj="qfq",
                        api=ts.pro_api(TUSHARE_TOKEN))
        if df is None or len(df) < 30:
            return None
        closes = list(df["close"].iloc[-60:])
        highs = list(df["high"].iloc[-60:])
        lows = list(df["low"].iloc[-60:])
        chg = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        rsi14 = calc_rsi(closes, 14)
        rsi26 = calc_rsi(closes, 26)
        ma5 = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        ma60 = calc_ma(closes, 60)
        dif, dea, macd_bar = calc_macd(closes)
        vr = round(df["vol"].iloc[-5:].sum() / max(df["vol"].iloc[-20:-5].sum(), 1), 2)
        g60 = round((closes[-1] / max(closes[-60], 1) - 1) * 100, 2) if len(closes) >= 60 else 0
        ma5_above = ma5 > ma10 > ma20 if all([ma5, ma10, ma20]) else False
        price_above_ma20 = closes[-1] > ma20 if ma20 else False
        smc_val, smc_reason = smc_score(closes, highs, lows)
        score, reasons = new_formula_score({
            "vr": vr, "rsi": rsi14 or 0, "ma多头": ma5_above,
            "价格在MA20上": price_above_ma20,
            "连续阳线": sum(1 for i in range(1, min(4, len(closes)))
                        if closes[-i] > closes[-i-1]),
            "g60": g60, "macd_dif": dif, "macd_dea": dea, "macd_bar": macd_bar
        })
        dif_val, dea_val, macd_bar_val = dif, dea, macd_bar
        return {
            "code": code, "name": "", "price": closes[-1], "chg": round(chg, 2),
            "rsi": rsi14, "rsi26": rsi26, "vr": vr, "g60": g60,
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "dif": dif_val, "dea": dea_val, "macd_bar": macd_bar_val,
            "ma多头": ma5_above, "价格在MA20上": price_above_ma20,
            "smc_score": smc_val, "smc_reason": smc_reason,
            "新公式评分": score, "新公式理由": reasons,
            "共振": "★★★" if score >= 8 and rsi14 and rsi14 <= 75 else
                   "★★" if score >= 6 else
                   "★" if score >= 4 else ""
        }
    except Exception as e:
        return {"code": code, "error": str(e)}

# ========== 持仓股分析 ==========
@app.route("/api/v1/stock/analysis", methods=["GET"])
def stock_analysis():
    code = request.args.get("code", "")
    v = get_stock_indicators(code)
    if not v:
        return jsonify({"error": "无数据"}), 404
    if "error" in v:
        return jsonify(v), 500
    return jsonify(v)

# ========== 批量扫描 ==========
@app.route("/api/v1/scan/batch", methods=["POST"])
def scan_batch():
    try:
        body = request.get_json() or {}
        codes = body.get("codes", STOCK_LIST)
        results = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(get_stock_indicators, c): c for c in codes}
            for fut in as_completed(futs):
                v = fut.result()
                if v:
                    results[v["code"]] = v
        ranked = sorted(results.values(), key=lambda x: x.get("新公式评分", 0), reverse=True)
        return jsonify({"results": results, "ranked": ranked, "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== 健康检查 ==========
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().strftime("%Y-%m-%d %H:%M")})

# ========== 市场数据 ==========
@app.route("/api/v1/market/summary", methods=["GET"])
def market_summary():
    try:
        idx_map = {
            "上证指数": "000001.SH", "沪深300": "000300.SH",
            "创业板": "399006.SZ", "深证成指": "399001.SZ"
        }
        data = {}
        for name, code in idx_map.items():
            try:
                df = pro.daily(ts_code=code, start_date="20250602", end_date="20250603")
                if len(df) >= 1:
                    row = df.iloc[-1]
                    prev = df.iloc[-2] if len(df) >= 2 else row
                    chg_pct = round((row["close"] - prev["close"]) / prev["close"] * 100, 2)
                    data[name] = {
                        "name": name, "close": round(row["close"], 2),
                        "chg_pct": chg_pct,
                        "volume": round(row["vol"] / 100000000, 2),
                        "code": code
                    }
            except Exception:
                pass
        return jsonify({
            "指数": list(data.keys()),
            "数据": data,
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== API文档 ==========
SWAGGER_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>A股分析API</title>
<style>
body{background:#0a0a0a;color:#e0e0e0;font-family:system-ui;max-width:900px;margin:0 auto;padding:20px}
h1{color:#60a5fa;border-bottom:2px solid #60a5fa;padding-bottom:8px}
h3{color:#34d399;margin-top:28px}
table{width:100%;border-collapse:collapse;margin:12px 0}
td{padding:8px 12px;border-bottom:1px solid #222;font-size:14px}
tr:hover background:#1a2}
.method-get td:first-child{color:#4ade80}
.method-post td:first-child{color:#f97316}
.desc{color:#888;font-size:12px;margin-top:4px}
pre{background:#111;padding:14px;border-radius:8px;color:#86efac;font-size:13px;overflow-x:auto}
</style></head>
<body>
<h1>📊 A股分析API</h1>
<p>服务状态: <span style="color:#4ade80">● Online</span> | Tushare: ✅ 已连接</p>

<h3>接口列表</h3>
<table>
<tr><td style="color:#4ade80">GET</td><td>/health</td><td>健康检查</td></tr>
<tr><td style="color:#4ade80">GET</td><td>/api/v1/market/summary</td><td>大盘指数实时行情</td></tr>
<tr><td style="color:#4ade80">GET</td><td>/api/v1/stock/analysis?code=600162.SH</td><td>单股技术分析</td></tr>
<tr><td style="color:#f97316">POST</td><td>/api/v1/scan/batch</td><td>批量技术扫描（≤100只）</td></tr>
</table>

<h3>批量扫描示例</h3>
<pre>POST /api/v1/scan/batch
Body: {"codes": ["600162.SH","601985.SH","601101.SH"]}</pre>

<h3>返回字段说明</h3>
<table>
<tr><td>rsi</td><td>RSI相对强弱指标（14日）</td></tr>
<tr><td>vr</td><td>量比（近5日均量/近20日均量）</td></tr>
<tr><td>ma多头</td><td>MA5>MA10>MA20多头排列</td></tr>
<tr><td>共振</td><td>★★★=强烈买入信号</td></tr>
<tr><td>新公式评分</td><td>6维度综合评分，≥6分启动买入窗口</td></tr>
</table>
</body></html>"""

@app.route("/docs")
def docs():
    return render_template_string(SWAGGER_HTML)

@app.route("/")
def index():
    return redirect("/docs")

# ========== 注册klines蓝图（修复404） ==========
app.register_blueprint(klines_bp)

if __name__ == "__main__":
    log.info(f"启动A股分析API服务器，端口:{API_PORT}")
    send_feishu(f"🚀 A股分析系统已启动！时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    app.run(host="0.0.0.0", port=API_PORT)