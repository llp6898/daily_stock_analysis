#!/usr/bin/env python3
"""
同花顺公式选股扫描 v6.0 - 观察名单模式
盘中多次扫描 → 写入 watchlist → 14:55 尾盘执行买入
逻辑：
  09:30-14:30：扫描 → 发现候选股 → 追加到 watchlist（不重复、不买）
  14:55：读取 watchlist → 执行全部买入 → 清空 watchlist
"""
import json, urllib.request, urllib.parse, re, time, signal, sys, os

WATCHLIST = '/workspace/stock_watchlist.json'
SCAN_RESULT = '/workspace/ths_scan_result.json'
BUY_RESULT = '/workspace/buy_result.json'
ACCOUNT_FILE = '/workspace/projects/workspace/user_accounts/default.json'

TODAY = time.strftime('%Y-%m-%d')

# ---- 工具函数 ----
def load_account():
    for _ in range(3):
        try:
            with open(ACCOUNT_FILE) as f:
                d = json.load(f)
            return d.get('capital_account', '116723406'), d.get('department_id', '997376')
        except:
            time.sleep(1)
    return '116723406', '997376'

def get_stk(code):
    return '2' if code.startswith('6') else '1'

def is_market_time():
    """检查是否为盘中（09:30-14:55）"""
    now = time.localtime()
    h, m = now.tm_hour, now.tm_min
    w = now.tm_wday  # 0=周一
    if w >= 5:
        return False  # 周末
    total_min = h * 60 + m
    return 9 * 60 + 30 <= total_min < 14 * 60 + 55

def is_final_scan_time():
    """14:55-14:59 尾盘窗口"""
    now = time.localtime()
    h, m = now.tm_hour, now.tm_min
    w = now.tm_wday
    if w >= 5:
        return False
    total_min = h * 60 + m
    return 14 * 60 + 55 <= total_min <= 14 * 60 + 59

# ---- watchlist 操作 ----
def load_watchlist():
    """读取今日观察名单"""
    if not os.path.exists(WATCHLIST):
        return []
    try:
        with open(WATCHLIST) as f:
            d = json.load(f)
        # 只保留今日记录
        if d.get('date') != TODAY:
            return []
        return d.get('stocks', [])
    except:
        return []

def save_watchlist(stocks):
    """保存观察名单（去重 + 今日标记）"""
    # 按 code 去重，保留最新一条
    seen = {}
    for s in stocks:
        seen[s['code']] = s
    unique = list(seen.values())
    with open(WATCHLIST, 'w') as f:
        json.dump({'date': TODAY, 'stocks': unique, 'updated': time.strftime('%H:%M')}, f, ensure_ascii=False, indent=2)
    return unique

def add_to_watchlist(candidates):
    """追加候选股到观察名单"""
    existing = {s['code']: s for s in load_watchlist()}
    added = 0
    for c in candidates:
        code = c['code']
        # 如果已存在但评分更高，更新
        if code in existing:
            if c.get('score', 0) > existing[code].get('score', 0):
                existing[code] = c
                added += 1
        else:
            existing[code] = c
            added += 1
    save_watchlist(list(existing.values()))
    return added

# ---- 同花顺数据抓取 ----
def fetch_ths_quote(codes):
    """抓取同花顺实时行情（支持多代码）"""
    if not codes:
        return {}
    ts = str(int(time.time() * 1000))
    # 单代码测试
    url = f'https://qt.gtimg.cn/q=sh{",".join(codes)}&_={ts}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.10jqka.com.cn/'})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode('gbk', 'ignore')
        lines = raw.strip().split('\n')
        result = {}
        for line in lines:
            m = re.search(r'qt_"?(sh\d+|sz\d+)"?\s*=\s*"([^"]+)"', line)
            if m:
                code = m.group(1)
                fields = m.group(2).split('~')
                if len(fields) < 10:
                    continue
                result[code] = {
                    'name': fields[1],
                    'price': float(fields[3]),
                    'chg': float(fields[31]) if fields[31] else 0,
                    'vr': 1.0
                }
        return result
    except Exception as e:
        return {}

def ths_formula_filter(stocks):
    """同花顺公式过滤，返回候选股列表"""
    if not stocks:
        return []
    codes = [s['code'] for s in stocks]
    quotes = fetch_ths_quote(codes)
    result = []
    for s in stocks:
        q = quotes.get(f"sh{s['code']}") or quotes.get(f"sz{s['code']}")
        if not q:
            continue
        price = q['price']
        chg = q['chg']
        vr = q.get('vr', 1.0)
        if abs(chg) > 9.5:
            continue  # 涨停股不买入（等二买）
        score = s.get('score', 0)
        rsi = s.get('rsi', 60)
        if score >= 28 and rsi < 85:
            s['price'] = price
            s['chg'] = chg
            s['vr'] = vr
            s['name'] = q.get('name', s.get('name', s['code']))
            result.append(s)
    return result

# ---- 主扫描逻辑 ----
def do_scan():
    """盘中扫描：读取 ths_scan_result.json，追加候选股到 watchlist"""
    # 读取今日扫描结果
    if not os.path.exists(SCAN_RESULT):
        return 0
    try:
        with open(SCAN_RESULT) as f:
            data = json.load(f)
    except:
        return 0

    if data.get('partial'):
        return 0  # 扫描被中断，不处理

    candidates = []
    for key in ['黑马启动_list', '强势突破_list', '量价异动_list', '超跌反弹_list']:
        for s in data.get(key, []):
            if s.get('score', 0) >= 28:
                s['formula'] = key
                candidates.append(s)

    if not candidates:
        return 0

    # 同花顺过滤
    filtered = ths_formula_filter(candidates)
    if not filtered:
        return 0

    added = add_to_watchlist(filtered)
    return added

# ---- 执行买入 ----
def do_final_buy():
    """14:55 尾盘买入：读取 watchlist，执行全部买入，清空"""
    stocks = load_watchlist()
    if not stocks:
        return [], []

    BUY_RESULT_TMP = '/tmp/buy_tmp.json'
    with open(BUY_RESULT_TMP, 'w') as f:
        json.dump({'status': 'running', 'time': TODAY, 'stocks': stocks}, f)

    success, fail = [], []
    USRID, YYBID = load_account()

    for s in stocks:
        code = s['code']
        price = float(s['price'])
        qty = 1000
        label = s.get('formula', '观察名单')
        score = s.get('score', 0)
        name = s.get('name', code)

        params = urllib.parse.urlencode({
            'usrid': USRID, 'zqdm': code, 'gddh': '',
            'scdm': get_stk(code), 'yybd': YYBID,
            'wtjg': str(price), 'wtsl': str(qty),
            'mmlb': 'B', 'datatype': 'json'
        })
        url = f'http://trade.10jqka.com.cn:8088/pt_stk_weituo_dklc?{params}'
        last_err = ''

        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=12) as r:
                    raw = r.read().decode('utf-8', 'ignore')
            except Exception as e:
                last_err = str(e)
                time.sleep(3)
                continue

            m = re.search(r'errorcode.*?"(\d+)".*?entrust_no.*?"([^"]+)"', raw)
            if not m:
                m = re.search(r'code="(\d+)".*?entrust_no="([^"]+)"', raw)
            if m and m.group(1) == '0':
                entrust = m.group(2)
                r = {'ok': True, 'entrust_no': entrust, 'stock': code,
                     'price': price, 'qty': qty, 'name': name, 'label': label, 'score': score}
                success.append(r)
                print(f'  ✅ 买入 {name}({code}) @{price} 委托号:{entrust}')
                break
            elif m:
                err = re.search(r'errormsg["\s:]+([^"<>]+)', raw)
                last_err = err.group(1).strip() if err else m.group(1)
                break
            else:
                last_err = raw[:80]

        if not any(r.get('stock') == code and r.get('ok') for r in success):
            fail.append({'stock': code, 'price': price, 'name': name,
                         'error': last_err, 'label': label, 'score': score})
            print(f'  🔴 买入失败 {name}({code}) {last_err}')

        time.sleep(2)

    # 写入结果
    with open(BUY_RESULT_TMP, 'w') as f:
        json.dump({'status': 'done', 'success': success, 'fail': fail,
                  'time': TODAY}, f, ensure_ascii=False, indent=2)

    # 清空 watchlist
    save_watchlist([])

    return success, fail

# ---- 信号处理 ----
def signal_handler(signum, frame):
    print('[SIGNAL] 中断，写入中间状态')
    sys.exit(0)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ---- 入口 ----
if __name__ == '__main__':
    print(f'同花顺公式扫描 [{time.strftime("%H:%M")}]')

    if is_final_scan_time():
        print('>>> 14:55 尾盘窗口，执行买入')
        success, fail = do_final_buy()
        print(f'完成: {len(success)}成功/{len(fail)}失败')
        if fail:
            print('失败列表:')
            for r in fail:
                print(f'  {r["name"]} {r["error"]}')

    elif is_market_time():
        print('>>> 盘中扫描，追加到观察名单')
        added = do_scan()
        if added > 0:
            wl = load_watchlist()
            print(f'观察名单新增{added}只，共{len(wl)}只')
        else:
            print('无新增候选')

    else:
        print('非交易时段，跳过')