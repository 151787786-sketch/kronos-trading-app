"""Round 3: data correctness - independently recompute indicators, fees,
account balances, and backtest math, then compare with API outputs."""
import json
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, r"D:\a\kronos\trading-app")
sys.path.insert(0, r"D:\a\kronos")

BASE = "http://127.0.0.1:7071"
PASS = FAIL = 0
FAILURES = []


def req(method, path, body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"raw": raw[:200]}
            return resp.status, {"text": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return -1, {"error": str(e)}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  XX {name} {detail}")


def section(t):
    print(f"\n=== {t} ===")


# ----------------------------------------------------------------

section("1. Indicator math (independent recompute)")
import pandas as pd
import numpy as np
import market as mkt
import indicators as ind

df = mkt.fetch_kline("600585", period="day", bars=600)
api_k = req("GET", "/api/market/kline?symbol=600585&period=day&bars=250")[1]["bars"]
last = api_k[-1]

# recompute MA20 independently
ma20_re = df["close"].rolling(20).mean().iloc[-1]
check("MA20 独立重算一致", abs(ma20_re - last["ma20"]) < 1e-6, f"api={last['ma20']} re={ma20_re}")

# RSI recompute
delta = df["close"].diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)
ag = gain.ewm(alpha=1/14, adjust=False).mean()
al = loss.ewm(alpha=1/14, adjust=False).mean()
rs = ag / al.replace(0, np.nan)
rsi_re = (100 - 100 / (1 + rs)).iloc[-1]
check("RSI 独立重算一致", abs(rsi_re - last["rsi"]) < 0.01, f"api={last['rsi']} re={rsi_re}")

# MACD recompute
ema_f = df["close"].ewm(span=12, adjust=False).mean()
ema_s = df["close"].ewm(span=26, adjust=False).mean()
dif_re = (ema_f - ema_s).iloc[-1]
check("MACD DIF 独立重算一致", abs(dif_re - last["dif"]) < 1e-6, f"api={last['dif']} re={dif_re}")

# BOLL recompute
mid = df["close"].rolling(20).mean().iloc[-1]
std = df["close"].rolling(20).std().iloc[-1]
upper_re = mid + 2 * std
check("BOLL上轨 独立重算一致", abs(upper_re - last["boll_upper"]) < 1e-6,
      f"api={last['boll_upper']} re={upper_re}")

section("2. Fee math & account balances")
import account

# reset, buy 1000 @ 17.41, sell 400 @ 17.50
req("POST", "/api/account/reset")
r = req("POST", "/api/trade", {"symbol": "600585", "name": "海螺水泥", "side": "BUY", "price": 17.41, "shares": 1000})
check("买入成功", r[0] == 200 and r[1].get("ok"))

acc = req("GET", "/api/account")[1]
# buy: amount=17.41*1000=17410, fee=max(17410*0.0003,5)=5.22, total=17415.22
expected_cash = 1000000 - 17410 - 5.22
check("买入后现金精确", abs(acc["cash"] - expected_cash) < 1e-6,
      f"api={acc['cash']} expected={expected_cash}")

# sell 400 @ 17.50: amount=7000, fee=max(7000*0.0003,5)+7000*0.0005=5+3.5=8.5
req("POST", "/api/trade", {"symbol": "600585", "side": "SELL", "price": 17.50, "shares": 400})
acc = req("GET", "/api/account")[1]
pos = acc["positions"][0]
expected_cash2 = expected_cash + 7000 - 8.5
check("卖出后现金精确", abs(acc["cash"] - expected_cash2) < 1e-6,
      f"api={acc['cash']} expected={expected_cash2}")
# remaining 600 shares, avg_cost = (17410+5.22)/1000 = 17.41522
check("持仓成本精确", abs(pos["avg_cost"] - (17410 + 5.22) / 1000) < 1e-6,
      f"api={pos['avg_cost']} expected={(17410+5.22)/1000}")
check("持仓数量600", abs(pos["shares"] - 600) < 1e-6)

# total asset = cash + 600 * market_price
mv = 600 * pos["market_price"]
check("总资产=现金+市值", abs(acc["total_asset"] - (acc["cash"] + mv)) < 1e-6)

req("POST", "/api/account/reset")

section("3. Backtest math sanity")
bt = req("GET", "/api/backtest?symbol=600585&strategy=combined")[1]
eq = bt["equity"]
check("回测期末资产=序列末值", abs(eq[-1] - bt["final_asset"]) < 1.0,
      f"eq[-1]={eq[-1]} final={bt['final_asset']}")
# total return = final/initial - 1
check("总收益率一致", abs(bt["total_return_pct"] - (bt["final_asset"] / 100000 - 1) * 100) < 0.5,
      f"api={bt['total_return_pct']}")
# benchmark = close/close0 * initial
bm0 = bt["benchmark"][0]
check("基准起点=初始资金", abs(bm0 - 100000) < 1.0, f"bm0={bm0}")
# equity should be non-negative
check("权益序列无负值", min(eq) >= 0)
# max drawdown <= 0
check("最大回撤<=0", bt["max_drawdown_pct"] <= 0)

section("4. Orders records consistency")
ords = req("GET", "/api/orders")[1]["orders"]
req("POST", "/api/account/reset")
# after reset orders cleared
ords2 = req("GET", "/api/orders")[1]["orders"]
check("重置后委托清空", len(ords2) == 0)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("Failures:", FAILURES)
sys.exit(1 if FAIL else 0)
