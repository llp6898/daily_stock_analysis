"""
A股自选股智能分析系统 - API 服务入口
Flask API Server + 全量扫描定时任务
"""
import os, sys, json, time, logging
from flask import Flask, jsonify, request
from datetime import datetime
import requests

# ========== Flask App ==========
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stock-api")

API_PORT = int(os.environ.get("PORT") or os.environ.get("API_PORT", 8000))
FEISHU_BOT_KEY = os.environ.get("FEISHU_BOT_KEY", "")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "b2d323ce6e8bf2c1549a72fd08538c1dc1ac4bf563550632c1a01759")
STOCK_LIST = os.environ.get("STOCK_LIST", "600162,301581,600143,601985").split(",")

# ========== 飞书通知 ==========
def send_feishu(text):
    if not FEISHU_BOT_KEY:
        log.warning("FEISHU_BOT_KEY not set, skip notification")
        return
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_BOT_KEY}"
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error(f"Feishu notification failed: {e}")

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
        import tushare as ts
        pro = ts.pro_api(TUSHARE_TOKEN)

        # 获取日线
        df = pro.daily(ts_code=ts_code)
        if df is None or df.empty:
            return jsonify({"error": "无数据"}), 404

        closes = df["close"].iloc[::-1].values
        prices = df["close"].iloc[::-1].values
        vols = df["vol"].iloc[::-1].values

        # 计算指标
        ma5 = round(float(prices[-5:].mean()), 2) if len(prices) >= 5 else None
        ma20 = round(float(prices[-20:].mean()), 2) if len(prices) >= 20 else None
        rsi = calc_rsi(closes[-15:]) if len(closes) >= 15 else None
        chg = round(float(df.iloc[-1]["pct_chg"]), 2)

        return jsonify({
            "code": ts_code,
            "name": get_name(ts_code),
            "price": float(prices[-1]),
            "ma5": ma5,
            "ma20": ma20,
            "rsi14": rsi,
            "涨跌幅": chg,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    except Exception as e:
        log.error(f"Analysis error: {e}")
        return jsonify({"error": str(e)}), 500

# ========== 大盘摘要 ==========
@app.route("/api/v1/market/summary")
def market_summary():
    try:
        import tushare as ts
        pro = ts.pro_api(TUSHARE_TOKEN)
        df = pro.index_daily(ts_code="000001.SH")
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        return jsonify({
            "指数": "上证指数",
            "现价": float(latest["close"]),
            "涨跌幅": round(float(latest["pct_chg"]), 2),
            "成交量": f"{float(latest['vol'])/10000:.1f}万手",
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== 辅助函数 ==========
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

def get_name(ts_code):
    names = {
        "600162": "香江控股", "301581": "黄山谷捷",
        "600143": "金发科技", "601985": "中国核电",
        "601101": "昊华能源", "601066": "中信建投",
        "600121": "郑州煤电", "600280": "中央商场"
    }
    code = ts_code.replace(".SH", "").replace(".SZ", "")
    return names.get(code, code)

# ========== 主程序 ==========
if __name__ == "__main__":
    log.info(f"启动A股分析API服务器，端口:{API_PORT}")
    send_feishu(f"🚀 A股分析系统已启动！时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    app.run(host="0.0.0.0", port=API_PORT)
