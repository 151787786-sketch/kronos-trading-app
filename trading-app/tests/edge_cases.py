"""Round 2: edge cases - invalid inputs, insufficient data, extreme values."""
import json
import sys
import urllib.request
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:7071"
PASS = FAIL = 0
FAILURES = []


def req(method, path, body=None, timeout=30):
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
            return resp.status, {"text": raw, "content_type": ctype}
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


# --- edge cases ---

section("1. Invalid inputs")
st, b = req("GET", "/api/market/kline?symbol=" + urllib.parse.quote("abcxyz") + "&period=day")
check("乱码代码K线 -> 5xx或错误信息", st >= 400, f"st={st}")

st, b = req("GET", "/api/market/kline?symbol=600585&period=" + urllib.parse.quote("999m"))
check("非法周期 -> 400/500", st >= 400, f"st={st}")

st, b = req("GET", "/api/market/kline?symbol=600585&period=day&bars=-5")
check("负数bars不崩溃", st in (200, 400, 500), f"st={st}")

st, b = req("GET", "/api/market/kline?symbol=600585&period=day&bars=999999")
check("超大bars不崩溃", st in (200, 400, 500), f"st={st}")

st, b = req("POST", "/api/watchlist", {"symbol": "  "})
check("空白symbol被拒", st == 400, f"st={st}")

st, b = req("POST", "/api/watchlist", {"symbol": "1234567890"})
check("超长数字被拒/报错", st in (400, 500), f"st={st}")

st, b = req("POST", "/api/trade", {"symbol": "600585", "side": "BUY", "price": -5, "shares": 100})
check("负价格交易被拒", st == 400, f"st={st}")

st, b = req("POST", "/api/trade", {"symbol": "600585", "side": "BUY", "price": 17, "shares": 0})
check("零股数被拒", st == 400, f"st={st}")

st, b = req("POST", "/api/trade", {"symbol": "600585", "side": "HODL", "price": 17, "shares": 100})
check("非法方向被拒", st == 400, f"st={st}")

st, b = req("GET", "/api/forecast?symbol=600585&pred_len=9999")
check("超大预测长度报错", st == 500 or st == 200, f"st={st}")

section("2. Insufficient data (newly listed stock)")
st, b = req("GET", "/api/market/kline?symbol=688836&period=day&bars=250")
check("次新股(3根)K线不崩溃", st in (200, 500), f"st={st}")
st, b = req("GET", "/api/forecast?symbol=688836&period=day&pred_len=30", timeout=60)
check("次新股预测返回错误而非崩溃", st >= 400 or "error" in b, f"st={st} b={str(b)[:80]}")
st, b = req("GET", "/api/buysell?symbol=688836&period=day", timeout=60)
check("次新股买卖点不崩溃", st in (200, 500), f"st={st}")

section("3. Accuracy edge")
st, b = req("GET", "/api/accuracy?symbol=600585&period=day&pred_len=30&rounds=0")
check("rounds=0 不崩溃", st in (200, 500), f"st={st}")
st, b = req("GET", "/api/accuracy?symbol=688836&period=day&pred_len=30&rounds=2", timeout=60)
check("次新股准确率优雅报错", st >= 400 or "error" in b, f"st={st}")

section("4. Report edge")
st, b = req("GET", "/api/report?symbol=600585&format=" + urllib.parse.quote("docx"))
check("非法格式 -> json兜底", st == 200 and "forecast" in b, f"st={st} b={str(b)[:60]}")
st, b = req("GET", "/api/report?symbol=600585&format=csv")
check("CSV含表头", st == 200 and "date,open,high" in b.get("text", ""), f"st={st}")

section("5. Empty/edge watchlist ops")
st, b = req("DELETE", "/api/watchlist/999999")
check("删除不存在OK", st == 200 and b.get("ok"), f"st={st}")

section("6. Forecast consistency")
st1, b1 = req("GET", "/api/forecast?symbol=600585&pred_len=30&T=1.3")
st2, b2 = req("GET", "/api/forecast?symbol=600585&pred_len=30&T=1.3")
check("同参数两次预测结果一致(缓存)", st1 == 200 and st2 == 200 and b1.get("end_close") == b2.get("end_close"))
check("预测高低价关系", st1 == 200 and all(b1["high"][i] >= b1["low"][i] for i in range(len(b1["low"]))))

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("Failures:", FAILURES)
sys.exit(1 if FAIL else 0)
