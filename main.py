"""
A股自选股智能分析系统 - API 服务入口
Flask API Server + 全量扫描定时任务
"""
import os, sys, json, time, logging, threading
from flask import Flask, jsonify, request, send_from_directory, redirect, render_template_string
from datetime import datetime
import requests
import tushare as ts

# ========== Flask App ==========
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stock-api")

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
    swing_lows = [lows[i] for i in range(5, len(lows)-5)
                  if lows[i] == min(lows[i-5:i+6])]
    if not swing_highs or not swing_lows:
        return 0, "无结构"
    last_low = min(swing_lows[-3:])
    last_high = max(swing_highs[-3:])
    range_size = last_high - last_low
    if range_size < price * 0.03:
        return 0, "区间过窄"
    if price > last_high * 0.98:
        ps = 3
    elif price > (last_high + last_low) / 2:
        ps = 2
    elif price < last_low * 1.02:
        ps = 1
    else:
        ps = 0
    ma_growth = (ma20 - ma60) / ma60 * 100
    ts_s = 2 if ma_growth > 5 else (1 if ma_growth > 0 else 0)
    zone_map = {3: "突破", 2: "区间上半", 1: "区间下限吸筹", 0: "区间下半"}
    return min(ps + ts_s, 5), zone_map.get(ps, "未知")

# ========== 新优选公式 ==========
def formula_score(rsi, vr, macd_bar, ma5, ma10, ma20, gain60, 连续阳):
    s, details = 0, []
    if vr >= 1.8:
        s += 2; details.append(f"VR{vr:.1f}+2")
    elif vr >= 1.2:
        s += 1; details.append(f"VR{vr:.1f}+1")
    if 45 <= rsi <= 68:
        s += 2; details.append(f"RSI{rsi:.0f}+2")
    elif 30 <= rsi < 45:
        s += 1; details.append(f"RSI{rsi:.0f}+1低")
    elif 68 < rsi <= 75:
        s += 1; details.append(f"RSI{rsi:.0f}+1")
    elif rsi > 75:
        s -= 2; details.append(f"RSI{rsi:.0f}-2高")
    if macd_bar is not None and macd_bar > 0:
        s += 3; details.append("MACD金叉+3")
    elif macd_bar is not None and macd_bar > -0.1:
        s += 1; details.append("MACD零轴+1")
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        s += 2; details.append("MA多头+2")
    elif ma5 and ma10 and ma5 > ma10:
        s += 1; details.append("MA拐头+1")
    if 连续阳 >= 3:
        s += 2; details.append(f"连阳{连续阳}+2")
    elif 连续阳 >= 1:
        s += 1; details.append(f"连阳{连续阳}+1")
    if gain60 > 60:
        s -= 3; details.append(f"g60{gain60:.0f}%-3")
    elif gain60 > 30:
        s -= 1; details.append(f"g60{gain60:.0f}%-1")
    return s, details

# ========== 黑马启动v2 ==========
def is_blackhorse(rsi, vr, ma5, ma10, ma20, gain_today, gain60):
    return (
        ma5 is not None and ma10 is not None and ma20 is not None and
        ma5 > ma10 > ma20 and vr >= 1.5 and
        30 <= rsi <= 80 and gain_today >= -2 and gain60 < 80
    )

# ========== 扫描单只 ==========
def scan_one(ts_code):
    try:
        df = pro.daily(ts_code=ts_code, end_date=datetime.now().strftime("%Y%m%d"), limit=80)
        if df is None or df.empty or len(df) < 30:
            return None
        closes = df["close"].iloc[::-1].values
        vols = df["vol"].iloc[::-1].values
        highs = df["high"].iloc[::-1].values
        lows = df["low"].iloc[::-1].values
        price = closes[-1]
        prev = closes[1]
        gain_today = (price - prev) / prev * 100 if prev else 0
        gain60 = (price - closes[min(60, len(closes)-1)]) / closes[min(60, len(closes)-1)] * 100
        vr = round(vols[-1] / (sum(vols[-5:]) / 5 or 1), 2)
        rsi = calc_rsi(closes[-15:])
        if rsi is None:
            return None
        _, _, macd_bar = calc_macd(closes)
        ma5 = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        连续阳 = sum(1 for i in range(1, min(6, len(closes)))
                     if closes[-i] >= closes[-i-1])
        smc_val, smc_zone = smc_score(closes, highs, lows)
        score, details = formula_score(rsi, vr, macd_bar, ma5, ma10, ma20, gain60, 连续阳)
        bh = is_blackhorse(rsi, vr, ma5, ma10, ma20, gain_today, gain60)
        共振 = "★★★" if (bh and score >= 6) else "★★" if (bh or score >= 6) else "★" if score >= 4 else "☆"
        return {
            "code": ts_code, "price": round(price, 2),
            "涨跌幅": round(gain_today, 2), "rsi": rsi, "vr": vr,
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "macd_bar": macd_bar, "smc": smc_val, "smc_zone": smc_zone,
            "score": score, "details": details, "共振": 共振,
            "gain60": round(gain60, 1), "连续阳": 连续阳, "blackhorse": bh
        }
    except Exception as e:
        return None

# ========== 健康检查 ==========
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().strftime("%Y-%m-%d %H:%M")})

# ========== 持仓股分析 ==========
@app.route("/api/v1/stock/analysis")
def stock_analysis():
    ts_code = request.args.get("code", "")
    if not ts_code:
        return jsonify({"error": "缺少 code 参数"}), 400
    try:
        df = pro.daily(ts_code=ts_code)
        if df is None or df.empty:
            return jsonify({"error": "无数据"}), 404
        closes = df["close"].iloc[::-1].values
        prices = df["close"].iloc[::-1].values
        ma5 = round(float(prices[-5:].mean()), 2) if len(prices) >= 5 else None
        ma20 = round(float(prices[-20:].mean()), 2) if len(prices) >= 20 else None
        rsi = calc_rsi(closes[-15:]) if len(closes) >= 15 else None
        chg = round(float(df.iloc[-1]["pct_chg"]), 2)
        return jsonify({
            "code": ts_code, "name": get_name(ts_code),
            "price": float(prices[-1]), "ma5": ma5, "ma20": ma20,
            "rsi14": rsi, "涨跌幅": chg,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    except Exception as e:
        log.error(f"Analysis error: {e}")
        return jsonify({"error": str(e)}), 500

# ========== 大盘摘要 ==========
@app.route("/api/v1/market/summary")
def market_summary():
    try:
        df = pro.index_daily(ts_code="000001.SH")
        latest = df.iloc[-1]
        return jsonify({
            "指数": "上证指数", "现价": float(latest["close"]),
            "涨跌幅": round(float(latest["pct_chg"]), 2),
            "成交量": f"{float(latest['vol'])/10000:.1f}万手",
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== 全市场批量扫描接口（同步，返回结果）============
@app.route("/api/v1/scan/batch", methods=["POST"])
def scan_batch():
    """接收股票代码列表，批量扫描，返回结果"""
    body = request.get_json() or {}
    codes = body.get("codes", [])
    if not codes:
        return jsonify({"error": "codes参数不能为空"}), 400
    if len(codes) > 100:
        return jsonify({"error": "单次最多100只股票"}), 400

    results = {}
    for code in codes:
        r = scan_one(code)
        if r:
            results[code] = r

    return jsonify({
        "count": len(results),
        "total_submitted": len(codes),
        "results": results,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

# ========== 全市场扫描接口（异步，后台运行）============
@app.route("/api/v1/scan/market", methods=["POST"])
def scan_market():
    """触发全市场扫描，结果写入 /tmp/scan_result.json"""
    body = request.get_json() or {}
    force = body.get("force", False)
    
    # 检查是否已有扫描在进行
    state_file = "/tmp/scan_state.json"
    state = {"status": "idle", "started_at": None, "finished_at": None, "count": 0}
    try:
        with open(state_file) as f:
            state = json.load(f)
    except:
        pass
    
    if state["status"] == "running" and not force:
        return jsonify({
            "error": "扫描正在进行",
            "started_at": state.get("started_at"),
            "message": "请等待当前扫描完成，或用 force=true 强制重启"
        }), 409
    
    # 后台启动扫描
    def run_scan():
        state["status"] = "running"
        state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(state_file, "w") as f:
            json.dump(state, f)
        
        try:
            log.info("开始全市场扫描...")
            df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
            all_stocks = [(row["ts_code"], row["name"]) for _, row in df.iterrows()]
            
            results = {"★★★": [], "★★": [], "★": [], "☆": []}
            for i, (ts_code, name) in enumerate(all_stocks):
                if i % 200 == 0 and i > 0:
                    log.info(f"扫描进度 {i}/{len(all_stocks)}")
                r = scan_one(ts_code)
                if r:
                    r["name"] = name
                    results[r["共振"]].append(r)
                if i % 50 == 0 and i > 0:
                    time.sleep(0.2)
            
            # 保存结果
            with open("/tmp/scan_result.json", "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            
            state["status"] = "done"
            state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["count"] = sum(len(v) for v in results.values())
            with open(state_file, "w") as f:
                json.dump(state, f)
            
            # 飞书推送
            fmt = format_feishu(results)
            send_feishu(fmt)
            log.info(f"全市场扫描完成! 共 {state['count']} 只股票入选")
            
        except Exception as e:
            log.error(f"全市场扫描失败: {e}")
            state["status"] = "error"
            state["error"] = str(e)
            with open(state_file, "w") as f:
                json.dump(state, f)
    
    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    
    return jsonify({
        "status": "started",
        "message": f"全市场扫描已启动，结果将保存到 /tmp/scan_result.json",
        "check_url": "/api/v1/scan/state",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

@app.route("/api/v1/scan/state")
def scan_state():
    """查询当前扫描状态"""
    try:
        with open("/tmp/scan_state.json") as f:
            state = json.load(f)
    except:
        state = {"status": "no_scan_yet"}
    return jsonify(state)

@app.route("/api/v1/scan/results")
def scan_results():
    """读取上次扫描结果"""
    try:
        with open("/tmp/scan_result.json") as f:
            results = json.load(f)
    except:
        return jsonify({"error": "暂无扫描结果，请先触发扫描"}), 404
    
    # 返回按评分排序的结果
    all_stocks = []
    for star in ["★★★", "★★", "★", "☆"]:
        for r in results.get(star, []):
            r["级别"] = star
            all_stocks.append(r)
    
    return jsonify({
        "total": len(all_stocks),
        "results": all_stocks,
        "summary": {star: len(results.get(star, [])) for star in ["★★★", "★★", "★", "☆"]}
    })

# ========== 辅助函数 ==========
def format_feishu(results):
    parts = []
    for star in ["★★★", "★★", "★", "☆"]:
        stocks = results.get(star, [])
        if not stocks:
            continue
        e = "🔴" if star == "★★★" else "🟠" if star == "★★" else "🟡" if star == "★" else "⚪"
        lines = [f"{e}{star} {len(stocks)}只"]
        for r in stocks[:5]:
            a = "✅买入" if star == "★★★" else "👀观察"
            lines.append(f"{a} {r.get('name',r.get('code'))}({r.get('code')}) {r.get('price')}元 RSI={r.get('rsi',0):.0f} VR={r.get('vr',0):.1f} 评分={r.get('score',0)}")
        if len(stocks) > 5:
            lines.append(f"等{len(stocks)-5}只...")
        parts.append("\n".join(lines))
    return f"📊 全市场共振扫描\n{datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n\n" + "\n\n".join(parts)

def get_name(ts_code):
    names = {
        "600162": "香江控股", "301581": "黄山谷捷",
        "600143": "金发科技", "601985": "中国核电",
        "601101": "昊华能源", "600027": "华电国际",
        "600121": "郑州煤电", "600280": "中央商场",
        "601016": "节能风电", "601600": "中国铝业",
        "601918": "新集能源", "600011": "华能国际",
    }
    code = ts_code.replace(".SH", "").replace(".SZ", "")
    return names.get(code, code)

# ========== 静态文件/Swagger Docs ==========
SWAGGER_HTML = """
<!DOCTYPE html>
<html><head><title>A股分析 API</title></head>
<body style="font-family: monospace; padding: 40px; background: #0a0a0a; color: #fff;">
<h2 style="color: #4ade80">📊 A股分析系统 API</h2>
<table style="border-collapse: collapse; width: 700px">
<tr style="border-bottom: 1px solid #333"><th style="text-align:left;padding:8px">方法</th><th style="text-align:left;padding:8px">路径</th><th style="text-align:left;padding:8px">说明</th></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#4ade80">GET</td><td style="padding:8px">/health</td><td style="padding:8px">健康检查</td></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#4ade80">GET</td><td style="padding:8px">/api/v1/market/summary</td><td style="padding:8px">大盘摘要</td></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#4ade80">GET</td><td style="padding:8px">/api/v1/stock/analysis?code=600162.SH</td><td style="padding:8px">持仓股分析</td></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#f59e0b">POST</td><td style="padding:8px">/api/v1/scan/batch</td><td style="padding:8px">批量扫描（≤100只）</td></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#f59e0b">POST</td><td style="padding:8px">/api/v1/scan/market</td><td style="padding:8px">全市场扫描（异步）</td></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#4ade80">GET</td><td style="padding:8px">/api/v1/scan/state</td><td style="padding:8px">查询扫描状态</td></tr>
<tr style="border-bottom: 1px solid #222"><td style="padding:8px;color:#4ade80">GET</td><td style="padding:8px">/api/v1/scan/results</td><td style="padding:8px">读取上次扫描结果</td></tr>
</table>
<h3 style="color:#f59e0b">批量扫描示例</h3>
<pre style="background:#1a1a1a;padding:16px;border-radius:8px;color:#86efac">
POST /api/v1/scan/batch
Body: {"codes": ["600162.SH","601985.SH","601101.SH"]}
</pre>
<h3 style="color:#f59e0b">全市场扫描示例</h3>
<pre style="background:#1a1a1a;padding:16px;border-radius:8px;color:#86efac">
POST /api/v1/scan/market
Body: {}   # 空对象即可，5-8分钟后结果推送到飞书
</pre>
</body></html>"""

@app.route("/docs")
def docs():
    return render_template_string(SWAGGER_HTML)

@app.route("/")
def index():
    return redirect("/docs")
if __name__ == "__main__":
    log.info(f"启动A股分析API服务器，端口:{API_PORT}")
    send_feishu(f"🚀 A股分析系统已启动！时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    app.run(host="0.0.0.0", port=API_PORT)