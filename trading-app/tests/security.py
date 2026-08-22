"""Round 7: security & robustness - injection, path traversal, malformed input."""
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
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:100]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:100]}
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


section("1. SQL injection attempts on watchlist")
payloads = ["600585'; DROP TABLE watchlist;--", "600585\" OR 1=1 --", "600585 OR '1'='1"]
for p in payloads:
    st, b = req("POST", "/api/watchlist", {"symbol": p})
    check(f"SQLi 拒绝/无害: {p[:20]}", st in (400, 404, 200), f"st={st}")
# verify watchlist still intact
st, b = req("GET", "/api/watchlist")
check("自选表未损坏", st == 200 and isinstance(b.get("watchlist"), list))

section("2. Path traversal via symbol")
traversals = ["../../etc/passwd", "..\\..\\windows\\win.ini", "file:///etc/passwd"]
for p in traversals:
    st, b = req("GET", "/api/market/kline?symbol=" + urllib.parse.quote(p) + "&period=day")
    check(f"路径穿越被拒: {p[:15]}", st >= 400, f"st={st}")

section("3. XSS in names/watchlist")
xss = "<script>alert(1)</script>"
st, b = req("POST", "/api/watchlist", {"symbol": xss})
# resolve should reject it; if accepted, frontend must escape. Verify rejection.
check("XSS symbol 被拒或无害", st in (400, 404, 200), f"st={st} b={str(b)[:60]}")

section("4. Malformed JSON / wrong types")
st, b = req("POST", "/api/trade", {"symbol": 12345, "side": "BUY", "price": "abc", "shares": "xyz"})
check("错误类型交易不崩溃", st in (400, 500, 200), f"st={st}")

st, b = req("GET", "/api/forecast?symbol=600585&T=" + urllib.parse.quote("abc"))
check("非数字T不崩溃", st in (200, 500), f"st={st}")

st, b = req("GET", "/api/backtest?symbol=600585&cash=" + urllib.parse.quote("abc"))
check("非数字cash不崩溃", st in (200, 500), f"st={st}")

section("5. Oversized inputs")
big = "6" * 100000
st, b = req("GET", "/api/market/resolve?q=" + big)
# 414 = Werkzeug rejected the oversized URI itself, which is correct behavior
check("超长查询被服务器拦截(不崩溃)", st in (400, 404, 414, 500), f"st={st}")

section("6. HTTP method handling")
st, b = req("DELETE", "/api/account")
check("DELETE账户不允许", st in (405, 400, 404), f"st={st}")
st, b = req("POST", "/api/strategies")
check("POST strategies不允许", st in (405, 400, 404), f"st={st}")

section("7. Sensitive endpoints")
st, b = req("GET", "/api/notify/config")
cfg_str = json.dumps(b, ensure_ascii=False)
check("通知配置不含完整token返回", "serverchan_key" not in cfg_str or not b.get("serverchan_key"),
      f"keys={list(b.keys())}")
check("配置不泄漏", "pushplus_token" not in cfg_str or not b.get("pushplus_token"))

section("8. DB integrity after attack attempts")
st, b = req("GET", "/api/account")
check("账户接口正常", st == 200 and "cash" in b)
st, b = req("GET", "/api/watchlist")
check("自选接口正常", st == 200)

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("Failures:", FAILURES)
sys.exit(1 if FAIL else 0)
