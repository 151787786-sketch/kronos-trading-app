"""Round 5: performance - response times, forecast cache hits, repeated calls."""
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:7071"
PASS = FAIL = 0
FAILURES = []


def timed(path, timeout=180):
    t0 = time.time()
    try:
        r = urllib.request.urlopen(BASE + path, timeout=timeout)
        raw = r.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw[:100]}
        return r.status, body, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:100]}, time.time() - t0
    except Exception as e:
        return -1, {"error": str(e)}, time.time() - t0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK {name} {detail}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  XX {name} {detail}")


def section(t):
    print(f"\n=== {t} ===")


section("1. Light endpoints < 5s")
for name, path in [
    ("状态", "/api/status"),
    ("首页", "/"),
    ("K线日线250", "/api/market/kline?symbol=600585&period=day&bars=250"),
    ("K线5分钟200", "/api/market/kline?symbol=600585&period=5m&bars=200"),
    ("实时报价", "/api/market/quotes?symbols=600585,002202"),
    ("自选列表", "/api/watchlist"),
    ("账户", "/api/account"),
    ("委托", "/api/orders"),
    ("回测", "/api/backtest?symbol=600585&strategy=combined"),
]:
    st, b, dt = timed(path, timeout=60)
    check(f"{name} {dt*1000:.0f}ms", st == 200 and dt < 5, f"st={st}")

section("2. Forecast: first call vs cached call")
st1, b1, dt1 = timed("/api/forecast?symbol=600585&period=day&pred_len=30&T=1.3&sample_count=1", timeout=180)
st2, b2, dt2 = timed("/api/forecast?symbol=600585&period=day&pred_len=30&T=1.3&sample_count=1", timeout=180)
check(f"首次预测 {dt1:.1f}s", st1 == 200, f"st={st1}")
check(f"缓存命中 {dt2*1000:.0f}ms (<2s)", st2 == 200 and dt2 < 2, f"st={st2} dt={dt2:.2f}")
check("两次结果一致", b1.get("end_close") == b2.get("end_close"))

section("3. Buysell caches forecast")
st1, b1, dt1 = timed("/api/buysell?symbol=600585&period=day", timeout=180)
st2, b2, dt2 = timed("/api/buysell?symbol=600585&period=day", timeout=180)
check(f"买卖点首次 {dt1:.1f}s", st1 == 200)
check(f"买卖点二次 {dt2*1000:.0f}ms", dt2 < 5, f"dt={dt2:.2f}")

section("4. Concurrent light requests")
import threading


def worker(res, i):
    st, b, dt = timed("/api/market/kline?symbol=600585&period=day&bars=200", timeout=60)
    res[i] = (st, dt)


threads, results = [], {}
for i in range(8):
    t = threading.Thread(target=worker, args=(results, i))
    threads.append(t)
    t.start()
for t in threads:
    t.join()
oks = sum(1 for v in results.values() if v[0] == 200)
check(f"8并发K线全部成功 {oks}/8", oks == 8)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("Failures:", FAILURES)
sys.exit(1 if FAIL else 0)
