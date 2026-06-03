"""
K线历史数据路由 - 完整升级版 v2.0
支持所有K线级别：1/5/15/30/60分钟 + 日/周/月线
使用 akshare 多接口自动适配
"""

from flask import Blueprint, request, jsonify
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

klines_bp = Blueprint("klines", __name__, url_prefix="/api/v1")


def get_stock_name(code: str) -> str:
    """获取股票名称"""
    try:
        info = ak.stock_individual_info_em(symbol=code)
        name_rows = info[info["item"].str.contains("股票名称", na=False)]
        if not name_rows.empty:
            return name_rows.iloc[0]["value"]
    except Exception:
        pass
    return code


def col_normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名映射"""
    col_map = {}
    cols_lower = {c: c.lower() for c in df.columns}
    for c in df.columns:
        cl = cols_lower[c]
        if "日期" in c or "date" in cl or "时间" in c: col_map[c] = "date"
        elif "开盘" in c or "open" in cl: col_map[c] = "open"
        elif "最高" in c or "high" in cl: col_map[c] = "high"
        elif "最低" in c or "low" in cl: col_map[c] = "low"
        elif "收盘" in c or "close" in cl: col_map[c] = "close"
        elif "成交量" in c or "vol" in cl or ("成交" in c and "额" not in c): col_map[c] = "vol"
        elif "成交额" in c or "amount" in cl: col_map[c] = "amount"
        elif "复权" in c or "adjust" in cl: col_map[c] = "adjust"
    return df.rename(columns=col_map)


def get_daily_ohlcv(df: pd.DataFrame):
    """从任意df提取OHLCV"""
    date_col = "date" if "date" in df.columns else ([c for c in df.columns if "date" in c.lower()] or [df.columns[0]])[0]
    data = {
        "dates":  df[date_col].astype(str).tolist(),
        "opens":  [],
        "highs":  [],
        "lows":   [],
        "closes": [],
        "vols":   [],
    }
    for col, key in [("open","opens"),("high","highs"),("low","lows"),("close","closes"),("vol","vols")]:
        if col in df.columns:
            data[key] = df[col].astype(float).round(2).tolist()
        else:
            data[key] = [0] * len(data["dates"])
    return data


def fetch_stock_daily(code: str, start_date: str, end_date: str, adjust: str = "qfq"):
    """股票日线（含复权）"""
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start_date, end_date=end_date, adjust=adjust
    )
    return col_normalize(df) if df is not None and not df.empty else None


def fetch_stock_minute(code: str, period: str, start_date: str, end_date: str):
    """股票分钟K线"""
    # period: 5/15/30/60
    period_map = {"5": "5", "15": "15", "30": "30", "60": "60", "1": "1"}
    p = period_map.get(period, "5")
    df = ak.stock_zh_a_hist(
        symbol=code, period=p,
        start_date=start_date, end_date=end_date, adjust="qfq"
    )
    return col_normalize(df) if df is not None and not df.empty else None


def fetch_index_daily(code: str, start_date: str, end_date: str, period: str = "daily"):
    """指数日线/周线/月线"""
    period_map = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
    p = period_map.get(period, "daily")
    # 指数代码统一处理
    symbol = code.replace(".SH", "").replace(".SZ", "")
    try:
        df = ak.index_zh_a_hist(symbol=symbol, period=p, start_date=start_date, end_date=end_date)
        return col_normalize(df) if df is not None and not df.empty else None
    except Exception:
        return None


def fetch_index_minute(code: str, period: str):
    """指数分钟K线"""
    period_map = {"5": "5", "15": "15", "30": "30", "60": "60"}
    p = period_map.get(period, "5")
    symbol = code.replace(".SH", "").replace(".SZ", "")
    try:
        # 尝试分钟接口
        df = ak.index_zh_a_hist(symbol=symbol, period=p, start_date="20250603", end_date="20260603")
        return col_normalize(df) if df is not None and not df.empty else None
    except Exception:
        return None


# ===== 指数列表 =====
INDEX_CODES = {
    "000001.SH", "399001.SZ", "000300.SH", "000016.SH", "000905.SH",
    "399006.SZ", "000688.SH", "399005.SZ",
}


@klines_bp.route("/klines/<code>", methods=["GET"])
def get_klines(code: str):
    """
    全功能K线接口
    参数:
      code      股票/指数代码，如 600519.SH / 000001.SH
      period    K线周期: 1/5/15/30/60/daily/weekly/monthly
      start_date 开始日期 YYYYMMDD（默认2年前）
      end_date   结束日期 YYYYMMDD（默认今天）
      adjust     复权类型: qfq(前复权)/hfq(后复权)/none(不复权)
    """
    try:
        period    = request.args.get("period", "daily")
        start_date = request.args.get("start_date", (datetime.now() - timedelta(days=730)).strftime("%Y%m%d"))
        end_date   = request.args.get("end_date", datetime.now().strftime("%Y%m%d"))
        adjust     = request.args.get("adjust", "qfq")

        # 自动判断是否为指数
        is_index = code in INDEX_CODES or (code.startswith("000") and ".SH" in code)

        df = None
        source = ""

        if is_index:
            # 指数
            if period in ["daily", "weekly", "monthly"]:
                df = fetch_index_daily(code, start_date, end_date, period)
                source = f"指数{period}"
            else:
                df = fetch_index_minute(code, period)
                source = f"指数{minute}分钟"
        else:
            # 股票
            if period in ["daily", "weekly", "monthly"]:
                df = fetch_stock_daily(code, start_date, end_date, adjust)
                source = f"股票日线({adjust}复权)"
            else:
                df = fetch_stock_minute(code, period, start_date, end_date)
                source = f"股票{period}分钟"

        if df is None or df.empty:
            return jsonify({"error": f"无数据 | code={code} period={period} is_index={is_index}"}), 404

        # 提取数据
        if "date" not in df.columns and len(df.columns) > 0:
            # 尝试第一列作为日期
            df.columns = [c for c in df.columns]  # 保持原样
            date_col = df.columns[0]
            df.rename(columns={date_col: "date"}, inplace=True)

        date_col = "date" if "date" in df.columns else df.columns[0]
        dates = df[date_col].astype(str).tolist()
        opens  = df["open"].astype(float).round(2).tolist()  if "open"  in df.columns else [0]*len(dates)
        highs  = df["high"].astype(float).round(2).tolist()  if "high"  in df.columns else [0]*len(dates)
        lows   = df["low"].astype(float).round(2).tolist()   if "low"   in df.columns else [0]*len(dates)
        closes = df["close"].astype(float).round(2).tolist() if "close" in df.columns else [0]*len(dates)
        vols   = df["vol"].astype(float).round(0).tolist()   if "vol"   in df.columns else [0]*len(dates)

        name = get_stock_name(code)

        return jsonify({
            "code": code,
            "name": name,
            "period": period,
            "adjust": adjust if period in ["daily", "weekly", "monthly"] else "qfq",
            "source": source,
            "dates": dates,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "vols": vols,
            "count": len(dates),
            "from": dates[0] if dates else "",
            "to": dates[-1] if dates else "",
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except Exception as e:
        return jsonify({"error": str(e), "code": code}), 500


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

        codes   = body.get("codes", [])
        period  = body.get("period", "daily")
        adjust  = body.get("adjust", "qfq")
        start_date = body.get("start_date", (datetime.now() - timedelta(days=365)).strftime("%Y%m%d"))
        end_date   = body.get("end_date", datetime.now().strftime("%Y%m%d"))

        if not codes:
            return jsonify({"error": "缺少codes参数"}), 400
        if len(codes) > 20:
            return jsonify({"error": "最多20只股票"}), 400

        results = {}
        for code in codes:
            try:
                if code in INDEX_CODES or (code.startswith("000") and ".SH" in code):
                    df = fetch_index_daily(code, start_date, end_date, period)
                else:
                    df = fetch_stock_daily(code, start_date, end_date, adjust) if period in ["daily", "weekly", "monthly"] else fetch_stock_minute(code, period, start_date, end_date)
                if df is not None and not df.empty:
                    date_col = "date" if "date" in df.columns else df.columns[0]
                    dates  = df[date_col].astype(str).tolist()
                    closes = df["close"].astype(float).round(2).tolist() if "close" in df.columns else []
                    highs  = df["high"].astype(float).round(2).tolist() if "high" in df.columns else []
                    vols   = df["vol"].astype(float).round(0).tolist() if "vol" in df.columns else []
                    results[code] = {
                        "dates": dates, "closes": closes,
                        "highs": highs, "vols": vols,
                        "count": len(dates),
                    }
                else:
                    results[code] = {"error": "无数据"}
            except Exception as e:
                results[code] = {"error": str(e)}

        return jsonify({
            "count": len(codes),
            "results": results,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@klines_bp.route("/klines/OHLCV", methods=["GET"])
def get_ohlcv():
    """
    单只股票完整OHLCV（方便前端画图）
    GET /api/v1/klines/OHLCV?code=600519.SH&period=daily&start=20250101&end=20260603
    """
    code = request.args.get("code", "")
    if not code:
        return jsonify({"error": "缺少code参数"}), 400
    period = request.args.get("period", "daily")
    start_date = request.args.get("start", (datetime.now() - timedelta(days=730)).strftime("%Y%m%d"))
    end_date   = request.args.get("end", datetime.now().strftime("%Y%m%d"))
    adjust     = request.args.get("adjust", "qfq")

    is_index = code in INDEX_CODES or (code.startswith("000") and ".SH" in code)

    if is_index:
        df = fetch_index_daily(code, start_date, end_date, period)
    else:
        df = fetch_stock_daily(code, start_date, end_date, adjust) if period in ["daily", "weekly", "monthly"] else fetch_stock_minute(code, period, start_date, end_date)

    if df is None or df.empty:
        return jsonify({"error": f"无数据: {code}"}), 404

    # 简单数组格式，方便前端直接用
    closes = df["close"].astype(float).round(2).tolist() if "close" in df.columns else []
    highs  = df["high"].astype(float).round(2).tolist() if "high" in df.columns else []
    lows   = df["low"].astype(float).round(2).tolist() if "low" in df.columns else []
    opens  = df["open"].astype(float).round(2).tolist() if "open" in df.columns else []
    vols   = df["vol"].astype(float).round(0).tolist() if "vol" in df.columns else []
    date_col = "date" if "date" in df.columns else df.columns[0]
    dates = df[date_col].astype(str).tolist()

    # 计算技术指标
    def calc_rsi(c, n=14):
        if len(c) < n+1: return None
        delta = [c[i+1]-c[i] for i in range(len(c)-1)]
        gain = [max(d,0) for d in delta]; loss = [abs(min(d,0)) for d in delta]
        ag = sum(gain[-n:])/n; al = sum(loss[-n:])/n
        return round(100-100/(1+ag/al), 2) if al > 0 else 100

    def calc_ma(c, n):
        return round(sum(c[-n:])/n, 2) if len(c) >= n else None

    if closes:
        rsi14 = calc_rsi(closes, 14)
        rsi26 = calc_rsi(closes, 26)
        ma5  = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        ma60 = calc_ma(closes, 60) if len(closes) >= 60 else None
    else:
        rsi14 = rsi26 = ma5 = ma10 = ma20 = ma60 = None

    return jsonify({
        "code": code,
        "period": period,
        "dates": dates,
        "opens": opens, "highs": highs, "lows": lows, "closes": closes, "vols": vols,
        "count": len(dates),
        "indicators": {
            "rsi14": rsi14, "rsi26": rsi26,
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "cur": closes[-1] if closes else None,
            "chg_pct": round((closes[-1]-closes[-2])/closes[-2]*100, 2) if len(closes)>=2 else 0,
        },
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })