#!/usr/bin/env python3
"""
同花顺公式自动买入 - 观察名单模式执行器
由 cron 在 14:55 触发：读取 watchlist → 执行全部买入 → 清空名单
"""
import json, urllib.request, urllib.parse, re, time, sys, os

BUY_RESULT = '/workspace/buy_result.json'
WATCHLIST = '/workspace/stock_watchlist.json'
ACCOUNT_FILE = '/workspace/projects/workspace/user_accounts/default.json'
TODAY = time.strftime('%Y-%m-%d')

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

def notify_success(code, name, price, qty, entrust, label, score):
    print(f'✅ 买入成功 {name}({code}) @{price} × {qty} 委托号:{entrust}')

def notify_fail(code, name, price, err):
    print(f'🔴 买入失败 {name}({code}) @{price} 原因:{err}')

def run():
    stocks = []
    if os.path.exists(WATCHLIST):
        try:
            with open(WATCHLIST) as f:
                d = json.load(f)
            if d.get('date') == TODAY:
                stocks = d.get('stocks', [])
        except:
            pass

    if not stocks:
        print(f'[{time.strftime("%H:%M")}] 观察名单为空，跳过尾盘买入')
        with open(BUY_RESULT, 'w') as f:
            json.dump({'status': 'done', 'success': [], 'fail': [], 'time': TODAY}, f)
        return

    print(f'[{time.strftime("%H:%M")}] 尾盘买入开始，共{len(stocks)}只')
    USRID, YYBID = load_account()
    success, fail = [], []

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
                success.append({'ok': True, 'entrust_no': entrust, 'stock': code,
                                'price': price, 'qty': qty, 'name': name,
                                'label': label, 'score': score})
                notify_success(code, name, price, qty, entrust, label, score)
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
            notify_fail(code, name, price, last_err)

        time.sleep(2)

    # 清空观察名单
    with open(WATCHLIST, 'w') as f:
        json.dump({'date': TODAY, 'stocks': [], 'cleared': time.strftime('%H:%M')}, f)

    # 保存结果
    with open(BUY_RESULT, 'w') as f:
        json.dump({'status': 'done', 'success': success, 'fail': fail,
                  'time': TODAY, 'trigger': '14:55'}, f, ensure_ascii=False, indent=2)

    print(f'尾盘买入完成: {len(success)}成功/{len(fail)}失败')

if __name__ == '__main__':
    run()