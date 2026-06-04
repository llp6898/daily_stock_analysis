"""
K线历史数据路由 - Tushare版本 v3.0
支持所有K线周期：1/5/15/30/60分钟 + 日/周/月线
数据源：Tushare Pro API（稳定可靠）
"""

from flask import Blueprint, request, jsonify
import tushare as ts
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

klines_bp = Blueprint("klines", __name__, url_prefix="/api/v1")

# Tushare Token（使用Render环境变量，fallback默认token）
TUSHARE_TOKEN = None  # 延迟初始化

def get_pro():
    """获取Tushare pro对象"""
    global TUSHARE_TOKEN
    if TUSHARE_TOKEN is None:
        import os
        TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "b2d323ce6e8bf2c1549a72fd08538c1dc1ac4bf563550632c1a01759")
    return ts.pro_api(TUSHARE_TOKEN)


def code_normalize(code: str) -> str:
    """统一股票代码格式，返回 XXXXXX.SH / XXXXXX.SZ（Tushare格式）"""
    code = code.upper().strip()
    # 第一步：去掉 .SH / .SZ / .BJ 后缀（标准Tushare格式变前缀）
    for suffix in [".SH", ".SZ", ".BJ"]:
        if code.endswith(suffix):
            code = code[:-len(suffix)]
            break
    # 第二步：去掉可能有的 SH/SZ/BJ 前缀（数字编码不应有前缀）
    for p in ["SH", "SZ", "BJ"]:
        if code.startswith(p):
            code = code[len(p):]
            break
    # 第三步：根据数字前缀判断交易所，返回 XXXXXX.EX 格式（Tushare标准）
    if code.startswith(("6", "5", "9")):
        return code + ".SH"   # 沪市
    elif code.startswith(("0", "3", "2")):
        return code + ".SZ"   # 深市
    elif code.startswith(("4", "8")):
        return code + ".BJ"   # 京市
    return code  # 纯数字（回测模式）


def ts_code_from_code(code: str) -> str:
    """将标准化后的纯数字代码转为Tushare格式 (600519.SH)"""
    code = code.upper().strip()
    # 去掉 .SH/.SZ/.BJ 后缀
    for suffix in [".SH", ".SZ", ".BJ"]:
        if code.endswith(suffix):
            code = code[:-len(suffix)]
            break
    # 去掉 SH/SZ/BJ 前缀
    for p in ["SH", "SZ", "BJ"]:
        if code.startswith(p):
            code = code[len(p):]
            break
    # 根据数字首位判断交易所，补回正确的 .SH/.SZ/.BJ 后缀
    if code.startswith(("6", "5", "9")):
        return code + ".SH"
    elif code.startswith(("0", "3", "2")):
        return code + ".SZ"
    elif code.startswith(("4", "8")):
        return code + ".BJ"
    # 回测模式：纯数字原样返回
    return code


def get_stock_name(ts_code: str) -> str:
    """获取股票名称"""
    try:
        pro = get_pro()
        df = pro.stock_basic(ts_code=ts_code, fields="name,ts_code")
        if df is not None and not df.empty:
            return df.iloc[0]["name"]
    except Exception:
        pass
    return ts_code


# ===== 指数列表（用于区分股票/指数） =====
INDEX_SET = {
    "000001.SH", "399001.SZ", "000300.SH", "000016.SH", "000905.SH",
    "399006.SZ", "000688.SH", "399005.SZ", "000688.SH",
}
# 对应的纯数字形式
INDEX_NUMBERS = {"000001", "399001", "000300", "000016", "000905",
                 "399006", "000688", "399005"}


def fetch_daily(ts_code: str, start_date: str, end_date: str, adjust: str = "qfq"):
    """日线数据（Tushare）"""
    pro = get_pro()
    adj_map = {"qfq": "qfq", "hfq": "hfq", "none": "none"}
    adj = adj_map.get(adjust, "qfq")

    try:
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return None
        # 按日期正序排列
        df = df.sort_values("trade_date")
        return df
    except Exception as e:
        return None


def fetch_weekly(ts_code: str, start_date: str, end_date: str):
    """周线数据（Tushare）"""
    pro = get_pro()
    try:
        df = pro.weekly(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date")
        return df
    except Exception:
        return None


def fetch_monthly(ts_code: str, start_date: str, end_date: str):
    """月线数据（Tushare）"""
    pro = get_pro()
    try:
        df = pro.monthly(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_date")
        return df
    except Exception:
        return None


def fetch_minute(ts_code: str, start_date: str, end_date: str, period: str = "60"):
    """分钟K线数据（Tushare pro_bar）"""
    # 限制窗口为5个交易日
    import datetime as _dt
    _start = _dt.datetime.strptime(start_date, "%Y%m%d")
    if (_dt.datetime.now() - _start).days > 5:
        start_date = (_dt.datetime.now() - _dt.timedelta(days=5)).strftime("%Y%m%d")
    # period: 1/5/15/30/60 分钟
    pro = get_pro()
    freq_map = {"1": "1min", "5": "5min", "15": "15min", "30": "30min", "60": "60min"}
    freq = freq_map.get(str(period), "60min")

    try:
        # 使用 pro_bar 获取分钟K线（仅限最近交易日）
        df = pro.pro_bar(
            ts_code=ts_code,
            adj="qfq",
            freq=freq,
            start_date=start_date,
            end_date=end_date,
            limit=3000  # 限制条数防止超限
        )
        if df is None or df.empty:
            return None
        df = df.sort_values("trade_time")
        return df
    except Exception:
        return None


# ============================================================
# 主接口：GET /api/v1/klines/<code>
# ============================================================
@klines_bp.route("/klines/<code>", methods=["GET"])
def get_klines(code: str):
    """
    全功能K线接口
    参数（URL query）:
      code       股票/指数代码，如 600519.SH
      period     K线周期: 1/5/15/30/60/daily/weekly/monthly
      start_date 开始日期 YYYYMMDD（默认2年前）
      end_date   结束日期 YYYYMMDD（默认今天）
      adjust     复权类型: qfq(前复权)/hfq(后复权)/none(不复权)
    """
    try:
        period     = request.args.get("period", "daily")
        start_date = request.args.get("start_date", (datetime.now() - timedelta(days=730)).strftime("%Y%m%d"))
        end_date   = request.args.get("end_date", datetime.now().strftime("%Y%m%d"))
        adjust     = request.args.get("adjust", "qfq")

        normalized = code_normalize(code)          # 如 "SH600519"
        ts_code    = ts_code_from_code(normalized)  # 如 "600519.SH" (Tushare格式)
        import logging as _log
        _log.warning(f"[klines] raw_code={code} normalized={normalized} ts_code={ts_code}")

        # 判断指数 vs 股票（使用标准化后纯数字代码）
        is_index = (normalized in INDEX_NUMBERS) or (
            normalized.isdigit() and
            len(normalized) <= 8 and
            normalized.startswith(("000", "399"))
        )

        # 获取数据
        df = None
        if period == "daily":
            df = fetch_daily(ts_code, start_date, end_date, adjust)
        elif period == "weekly":
            df = fetch_weekly(ts_code, start_date, end_date)
        elif period == "monthly":
            df = fetch_monthly(ts_code, start_date, end_date)
        elif period in ("1", "5", "15", "30", "60"):
            df = fetch_minute(ts_code, start_date, end_date, period)

        if df is None or df.empty:
            return jsonify({
                "code": ts_code,
                "error": f"无数据 period={period} start={start_date} end={end_date}",
                "is_index": is_index
            }), 404

        # 提取字段
        date_col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        dates = df[date_col].astype(str).tolist()

        def safe_float(col, default=0.0):
            return df[col].astype(float).round(2).tolist() if col in df.columns else [default] * len(dates)

        closes = safe_float("close")
        opens  = safe_float("open")
        highs  = safe_float("high")
        lows   = safe_float("low")
        vols   = df["vol"].astype(float).round(0).tolist() if "vol" in df.columns else [0] * len(dates)

        # 股票名称（仅股票需要）
        name = get_stock_name(ts_code) if not is_index else ts_code

        # 技术指标计算
        indicators = {}
        try:
            if len(closes) >= 15:
                c = closes
                # RSI
                def calc_rsi(c, n=14):
                    if len(c) < n+1: return None
                    delta = [c[i+1]-c[i] for i in range(len(c)-1)]
                    gain = [max(d,0) for d in delta]; loss = [abs(min(d,0)) for d in delta]
                    ag = sum(gain[-n:])/n; al = sum(loss[-n:])/n
                    return round(100-100/(1+ag/al), 2) if al > 0 else 100
                # MA
                def calc_ma(c, n):
                    return round(sum(c[-n:])/n, 2) if len(c) >= n else None
                indicators = {
                    "rsi14": calc_rsi(closes, 14),
                    "rsi26": calc_rsi(closes, 26),
                    "ma5":   calc_ma(closes, 5),
                    "ma10":  calc_ma(closes, 10),
                    "ma20":  calc_ma(closes, 20),
                    "ma60":  calc_ma(closes, 60) if len(closes) >= 60 else None,
                    "cur":   closes[-1] if closes else None,
                    "chg_pct": round((closes[-1]-closes[-2])/closes[-2]*100, 2) if len(closes)>=2 else 0,
                }
        except Exception:
            pass

        return jsonify({
            "code": ts_code,
            "name": name,
            "period": period,
            "adjust": adjust,
            "is_index": is_index,
            "dates": dates,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "vols": vols,
            "count": len(dates),
            "from": dates[0] if dates else "",
            "to": dates[-1] if dates else "",
            "indicators": indicators,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except Exception as e:
        return jsonify({"error": str(e), "code": code}), 500


# ============================================================
# 批量接口：POST /api/v1/klines/batch
# ============================================================
@klines_bp.route("/klines/batch", methods=["POST"])
def get_klines_batch():
    """
    批量获取K线（最多20只）
    Body: {"codes": ["600519.SH","601985.SH"], "period": "daily", "adjust": "qfq"}
    """
    try:
        body = request.get_json()
        if not body:
            return jsonify({"error": "缺少body"}), 400

        codes     = body.get("codes", [])
        period    = body.get("period", "daily")
        adjust    = body.get("adjust", "qfq")
        start_date = body.get("start_date", (datetime.now() - timedelta(days=365)).strftime("%Y%m%d"))
        end_date   = body.get("end_date", datetime.now().strftime("%Y%m%d"))

        if not codes:
            return jsonify({"error": "缺少codes参数"}), 400
        if len(codes) > 20:
            return jsonify({"error": "最多20只股票"}), 400

        results = {}
        for code in codes:
            try:
                normalized = code_normalize(code)
                ts_code = ts_code_from_code(normalized)
                if period == "daily":
                    df = fetch_daily(ts_code, start_date, end_date, adjust)
                elif period == "weekly":
                    df = fetch_weekly(ts_code, start_date, end_date)
                elif period == "monthly":
                    df = fetch_monthly(ts_code, start_date, end_date)
                elif period in ("1", "5", "15", "30", "60"):
                    df = fetch_minute(ts_code, start_date, end_date, period)
                else:
                    df = fetch_daily(ts_code, start_date, end_date, adjust)

                if df is not None and not df.empty:
                    date_col = "trade_date" if "trade_date" in df.columns else df.columns[0]
                    dates  = df[date_col].astype(str).tolist()
                    closes = df["close"].astype(float).round(2).tolist() if "close" in df.columns else []
                    results[normalized] = {
                        "dates": dates, "closes": closes,
                        "count": len(dates),
                    }
                else:
                    results[normalized] = {"error": "无数据"}
            except Exception as e:
                results[code] = {"error": str(e)}

        return jsonify({
            "count": len(codes),
            "results": results,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 前端画图专用接口：GET /api/v1/klines/OHLCV
# ============================================================
@klines_bp.route("/klines/OHLCV", methods=["GET"])
def get_ohlcv():
    """
    前端K线图画图专用接口
    GET /api/v1/klines/OHLCV?code=600519.SH&period=daily&start=20250101&end=20260603
    """
    code = request.args.get("code", "")
    if not code:
        return jsonify({"error": "缺少code参数"}), 400

    period     = request.args.get("period", "daily")
    start_date = request.args.get("start", (datetime.now() - timedelta(days=730)).strftime("%Y%m%d"))
    end_date   = request.args.get("end", datetime.now().strftime("%Y%m%d"))
    adjust     = request.args.get("adjust", "qfq")

    normalized = code_normalize(code)
    ts_code    = ts_code_from_code(normalized)

    if period == "daily":
        df = fetch_daily(ts_code, start_date, end_date, adjust)
    elif period == "weekly":
        df = fetch_weekly(ts_code, start_date, end_date)
    elif period == "monthly":
        df = fetch_monthly(ts_code, start_date, end_date)
    elif period in ("1", "5", "15", "30", "60"):
        df = fetch_minute(ts_code, start_date, end_date, period)
    else:
        df = fetch_daily(ts_code, start_date, end_date, adjust)

    if df is None or df.empty:
        return jsonify({"error": f"无数据: {ts_code}"}), 404

    date_col = "trade_date" if "trade_date" in df.columns else df.columns[0]
    dates  = df[date_col].astype(str).tolist()
    closes = df["close"].astype(float).round(2).tolist() if "close" in df.columns else []
    highs  = df["high"].astype(float).round(2).tolist() if "high" in df.columns else []
    lows   = df["low"].astype(float).round(2).tolist() if "low" in df.columns else []
    opens  = df["open"].astype(float).round(2).tolist() if "open" in df.columns else []
    vols   = df["vol"].astype(float).round(0).tolist() if "vol" in df.columns else []

    return jsonify({
        "code": normalized,
        "period": period,
        "dates": dates,
        "opens": opens, "highs": highs, "lows": lows, "closes": closes, "vols": vols,
        "count": len(dates),
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })