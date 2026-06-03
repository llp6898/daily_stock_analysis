"""
K线历史数据路由 - 添加到 Render 服务的 main.py 或 routes.py
使用 akshare 获取 A股日线/周线/分钟线历史数据
"""

from flask import Blueprint, request, jsonify
import akshare as ak
import pandas as pd
from datetime import datetime

klines_bp = Blueprint("klines", __name__, url_prefix="/api/v1")


def stock_code_fix(code: str) -> str:
    """统一股票代码格式"""
    code = code.upper().strip()
    if not (code.startswith("SH") or code.startswith("SZ") or code.startswith("BJ")):
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith("0") or code.startswith("3"):
            return f"SZ{code}"
        elif code.startswith("4") or code.startswith("8"):
            return f"BJ{code}"
    return code.replace("SH", "SH").replace("SZ", "SZ").replace("BJ", "BJ")


@klines_bp.route("/klines/<code>", methods=["GET"])
def get_klines(code):
    """
    获取股票/指数K线历史数据
    参数:
      code: 股票代码，如 600519.SH
      period: 日线周期 daily(默认)/weekly/monthly
      start_date: 开始日期 YYYYMMDD
      end_date: 结束日期 YYYYMMDD
      adjust: 复权类型 qfq(前复权默认)/hfq(后复权)/none(不复权)
    返回:
      {code, name, dates, opens, highs, lows, closes, vols, update_time}
    """
    try:
        # 参数解析
        period = request.args.get("period", "daily")
        start_date = request.args.get("start_date", "20200101")
        end_date = request.args.get("end_date", datetime.now().strftime("%Y%m%d"))
        adjust = request.args.get("adjust", "qfq")

        # period 映射
        period_map = {
            "daily": "daily",
            "weekly": "weekly",
            "monthly": "monthly",
            "5": "5",
            "15": "15",
            "30": "30",
            "60": "60",
        }
        period_ak = period_map.get(period, "daily")

        # 指数代码特殊处理
        index_codes = ["000001.SH", "399001.SZ", "000300.SH", "000016.SH", "000905.SH"]
        is_index = code in index_codes or "SH" in code and code.startswith("000")
        is_index = is_index or code in ["399006.SZ"]  # 创业板指

        if is_index:
            # 指数：使用 index_zh_a_hist
            df = ak.index_zh_a_hist(symbol=code, period=period_ak,
                                     start_date=start_date, end_date=end_date)
        else:
            # 股票：使用 stock_zh_a_hist
            df = ak.stock_zh_a_hist(
                symbol=code,
                period=period_ak,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )

        if df is None or df.empty:
            return jsonify({"error": "无数据，可能代码错误或日期范围无效"}), 404

        # 列名标准化（akshare不同接口列名略有差异）
        col_map = {}
        for col in df.columns:
            c = col.lower()
            if "日期" in col:   col_map[col] = "date"
            elif "开盘" in col: col_map[col] = "open"
            elif "最高" in col: col_map[col] = "high"
            elif "最低" in col: col_map[col] = "low"
            elif "收盘" in col: col_map[col] = "close"
            elif "成交量" in col or "量" in col: col_map[col] = "vol"
            elif "成交额" in col: col_map[col] = "amount"

        df = df.rename(columns=col_map)

        # 提取数据
        if "date" not in df.columns:
            return jsonify({"error": f"返回数据列名异常：{df.columns.tolist()}"}), 500

        dates  = df["date"].astype(str).tolist()
        opens  = df["open"].astype(float).tolist()  if "open"  in df.columns else [0]*len(dates)
        highs  = df["high"].astype(float).tolist()  if "high"  in df.columns else [0]*len(dates)
        lows   = df["low"].astype(float).tolist()   if "low"   in df.columns else [0]*len(dates)
        closes = df["close"].astype(float).tolist() if "close" in df.columns else [0]*len(dates)
        vols   = df["vol"].astype(float).tolist()   if "vol"   in df.columns else [0]*len(dates)

        # 股票名称
        try:
            info = ak.stock_individual_info_em(symbol=code)
            name = info[info["item"]=="股票名称"]["value"].values[0] if len(info) > 0 else code
        except Exception:
            name = code

        return jsonify({
            "code": code,
            "name": name,
            "period": period,
            "adjust": adjust,
            "dates": dates,
            "opens": [round(v, 2) for v in opens],
            "highs": [round(v, 2) for v in highs],
            "lows":  [round(v, 2) for v in lows],
            "closes": [round(v, 2) for v in closes],
            "vols": [round(v, 0) for v in vols],
            "count": len(dates),
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@klines_bp.route("/klines/market", methods=["GET"])
def get_market_klines():
    """
    批量获取多只股票的K线数据（支持最多20只）
    参数: codes=600519.SH,601985.SH,...
    """
    codes_param = request.args.get("codes", "")
    if not codes_param:
        return jsonify({"error": "缺少codes参数"}), 400

    codes = [c.strip() for c in codes_param.split(",") if c.strip()]
    if len(codes) > 20:
        return jsonify({"error": "最多支持20只股票"}), 400

    results = {}
    for code in codes:
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date="20250101", end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq"
            )
            if df is not None and not df.empty:
                cols = df.columns.tolist()
                col_map = {}
                for c in cols:
                    if "日期" in c: col_map[c] = "date"
                    elif "开盘" in c: col_map[c] = "open"
                    elif "最高" in c: col_map[c] = "high"
                    elif "最低" in c: col_map[c] = "low"
                    elif "收盘" in c: col_map[c] = "close"
                    elif "成交量" in c or "量" in c: col_map[c] = "vol"
                df = df.rename(columns=col_map)
                closes = df["close"].astype(float).tolist() if "close" in df.columns else []
                results[code] = {
                    "dates":  df["date"].astype(str).tolist() if "date" in df.columns else [],
                    "closes": [round(v, 2) for v in closes],
                    "count": len(closes),
                }
        except Exception:
            results[code] = {"error": "获取失败"}

    return jsonify({"count": len(results), "results": results})