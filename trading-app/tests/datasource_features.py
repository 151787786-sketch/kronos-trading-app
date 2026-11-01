"""Round 13: 数据源适配层（腾讯 / Wind 可切换）回归测试。

运行前需先启动 APP（python app.py）。

覆盖：
  A. market_backend 契约层 —— 后端注册、可用性探测、能力声明、偏好读写
  B. market.py 分发    —— 68 个调用点无感切换、返回契约不变
  C. HTTP 层           —— /api/datasource*
  D. 数据诚实性        —— 成交额估算被正确标注（腾讯接口不返回该字段）
"""
import json
import os
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:7071"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILURES = []

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=120):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:300]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}
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


import pandas as pd  # noqa: E402

import market  # noqa: E402
import market_backend as mb  # noqa: E402

# ===========================================================================
print("\n=== A. market_backend 契约层 ===")

# A1. 基础契约常量
check("A1 KLINE_COLUMNS 含 7 列", len(mb.KLINE_COLUMNS) == 7)
check("A1b 列名正确",
      mb.KLINE_COLUMNS == ["timestamps", "open", "high", "low", "close",
                           "volume", "amount"], str(mb.KLINE_COLUMNS))
check("A1c 支持 6 个周期", len(mb.PERIODS) == 6, str(mb.PERIODS))
check("A1d QUOTE_FIELDS 含 13 个字段", len(mb.QUOTE_FIELDS) == 13,
      str(len(mb.QUOTE_FIELDS)))

# A2. 后端已注册
market._ensure_backends()
names = sorted(mb._BACKENDS.keys())
check("A2 注册了 tencent", "tencent" in names, str(names))
check("A2b 注册了 wind", "wind" in names, str(names))

# A3. 可用性探测
tencent_b = mb._BACKENDS["tencent"]
wind_b = mb._BACKENDS["wind"]
check("A3 腾讯后端可用", tencent_b.available()[0] is True)
check("A3b 腾讯可用时无原因文字", tencent_b.available()[1] == "")

wind_ok, wind_reason = wind_b.available()
check("A3c Wind 可用性可探测", isinstance(wind_ok, bool))
check("A3d Wind 不可用时给出具体原因", wind_ok or bool(wind_reason),
      "不可用但没说原因")

# A4. 能力声明
caps_t = tencent_b.capabilities()
caps_w = wind_b.capabilities()
check("A4 腾讯声明 real_amount", "real_amount" in caps_t)
check("A4b 腾讯 K 线成交额标注为估算（False）", caps_t["real_amount"] is False,
      "接口不返回成交额字段，已实测")
check("A4c 腾讯实时成交额标注为真实",
      caps_t.get("real_amount_realtime") is True)
check("A4d 腾讯不宣称 Level-2", caps_t["level2"] is False)
check("A4e Wind 声明的能力不同", caps_w != caps_t)
check("A4f 每个后端都声明 periods", all("periods" in c for c in (caps_t, caps_w)))

# A5. 优先级
check("A5 Wind 优先级高于腾讯", wind_b.priority > tencent_b.priority,
      f"{wind_b.priority} vs {tencent_b.priority}")

# A6. list_backends 结构
lst = mb.list_backends()
check("A6 list_backends 返回列表", isinstance(lst, list) and len(lst) >= 2)
check("A6b 每项含 name/available/capabilities",
      all(all(k in x for k in ("name", "available", "capabilities", "priority"))
          for x in lst))
check("A6c 按优先级降序", lst[0]["priority"] >= lst[-1]["priority"])

# A7. 偏好读写
orig = mb.get_preference()
try:
    for choice in ("auto", "tencent", "wind"):
        mb.set_preference(choice)
        check(f"A7 偏好可设为 {choice}", mb.get_preference() == choice,
              mb.get_preference())
    try:
        mb.set_preference("bloomberg")
        check("A7b 非法偏好被拒绝", False)
    except ValueError as e:
        check("A7b 非法偏好被拒绝", "bloomberg" in str(e) or "必须" in str(e))
finally:
    mb.set_preference(orig)

# A8. 契约校验拦截坏数据
try:
    mb.DataBackend._validate_kline(pd.DataFrame(), "X", "day")
    check("A8 空 DataFrame 被拒绝", False)
except mb.BackendError:
    check("A8 空 DataFrame 被拒绝", True)

try:
    mb.DataBackend._validate_kline(pd.DataFrame({"timestamps": ["2026-01-01"]}), "X", "day")
    check("A8b 缺必需列被拒绝", False)
except mb.BackendError as e:
    check("A8b 缺必需列被拒绝", "缺少必需列" in str(e))

good = pd.DataFrame({
    "timestamps": ["2026-01-02", "2026-01-01"],
    "open": [1, 1], "high": [2, 2], "low": [0.5, 0.5],
    "close": [1.5, 1.5], "volume": [100, 100],
})
validated = mb.DataBackend._validate_kline(good, "X", "day")
check("A8c 合法数据通过校验", len(validated) == 2)
check("A8d 校验后按时间升序", validated["timestamps"].is_monotonic_increasing)
check("A8e 缺 amount 时自动补列", "amount" in validated.columns)

# A9. Wind 代码转换
wb = mb.WindBackend()
for raw, expect in (("600519", "600519.SH"), ("000001", "000001.SZ"),
                    ("430047", "430047.BJ"), ("sh600519", "600519.SH"),
                    ("sz000001", "000001.SZ"), ("300750", "300750.SZ")):
    check(f"A9 Wind 代码 {raw} → {expect}", wb.to_wind_code(raw) == expect,
          wb.to_wind_code(raw))
check("A9b 反解 Wind 代码", wb.from_wind_code("600519.SH") == "600519")

# A10. Wind 不可用时拒绝取数（不假装成功）
if not wind_ok:
    try:
        wb.fetch_kline("600519", "day", 10)
        check("A10 Wind 不可用时拒绝取数", False, "竟然返回了数据")
    except mb.BackendUnavailable as e:
        check("A10 Wind 不可用时拒绝取数", True)
        check("A10b 错误信息说明原因", "Wind" in str(e), str(e)[:60])


# ===========================================================================
print("\n=== B. market.py 分发层 ===")

# B1. 旧函数仍存在（68 个调用点依赖）
for fn in ("fetch_kline", "fetch_realtime", "fetch_realtime_one",
           "fetch_daily", "resolve_symbol", "search_stock"):
    check(f"B1 market.{fn} 仍可调用", callable(getattr(market, fn, None)))

# B2. 内部实现被改名（不再是公开名）
check("B2 腾讯实现已改名 _tencent_fetch_kline",
      callable(getattr(market, "_tencent_fetch_kline", None)))
check("B2b 腾讯实时实现已改名 _tencent_fetch_realtime",
      callable(getattr(market, "_tencent_fetch_realtime", None)))

# B3. 真实取数（经分发层）
df = market.fetch_kline("600126", period="day", bars=20)
check("B3 fetch_kline 返回 DataFrame", isinstance(df, pd.DataFrame))
check("B3b 含全部 7 列",
      set(mb.KLINE_COLUMNS).issubset(df.columns),
      f"missing={set(mb.KLINE_COLUMNS) - set(df.columns)}")
check("B3c 行数不超请求数", len(df) <= 20, len(df))
check("B3d 时间升序", df["timestamps"].is_monotonic_increasing)
check("B3e 价格为正", (df["close"] > 0).all())

# B4. 成交额估算被标注
check("B4 K 线 DataFrame 标记 amount_estimated", df.attrs.get("amount_estimated") is True,
      "腾讯接口不返回成交额，必须标注为估算")

# B5. 实时行情契约
quotes = market.fetch_realtime(["600126", "000657"])
check("B5 fetch_realtime 返回列表", isinstance(quotes, list))
if quotes:
    missing = [f for f in mb.QUOTE_FIELDS if f not in quotes[0]]
    check("B5b 含全部 13 个约定字段", not missing, str(missing))
    check("B5c 价格为正", quotes[0]["price"] > 0)

    # B6. fetch_realtime_one 包装正常
    one = market.fetch_realtime_one("600126")
    check("B6 fetch_realtime_one 返回单条", isinstance(one, dict) and "symbol" in one)

# B7. 非法周期报错
try:
    market.fetch_kline("600126", period="3m", bars=10)
    check("B7 非法周期被拒绝", False)
except (ValueError, RuntimeError) as e:
    check("B7 非法周期被拒绝", True)

# B8. datasource_info 结构
info = market.datasource_info()
check("B8 datasource_info 含 state/backends/preferred",
      all(k in info for k in ("state", "backends", "preferred")))
check("B8b backends 是列表", isinstance(info["backends"], list))


# ===========================================================================
print("\n=== C. HTTP 层 ===")

st, r = req("GET", "/api/datasource")
check("C1 GET /api/datasource 200", st == 200, f"status={st}")
check("C1b 含 ok/backends/preference/effective",
      all(k in r for k in ("ok", "backends", "preference", "effective")),
      str(list(r))[:120])
check("C1c 生效后端为 tencent", r.get("effective") == "tencent",
      str(r.get("effective")))
check("C1d 含合法选择列表", r.get("choices") == ["auto", "tencent", "wind"],
      str(r.get("choices")))

st, r = req("POST", "/api/datasource/select", body={"backend": "auto"})
check("C2 切到 auto 200", st == 200 and r.get("ok") is True, str(r)[:120])

st, r = req("POST", "/api/datasource/select", body={"backend": "wind"})
check("C3 切到 wind 时给出回退警告",
      st == 200 and r.get("effective") == "tencent" and bool(r.get("warning")),
      str(r)[:150])
req("POST", "/api/datasource/select", body={"backend": "auto"})

st, r = req("POST", "/api/datasource/select", body={"backend": "nope"})
check("C4 非法后端 → 400", st == 400, f"status={st}")

st, r = req("POST", "/api/datasource/select", body={})
check("C5 缺参数 → 400", st == 400, f"status={st}")

st, r = req("POST", "/api/datasource/probe")
check("C6 probe 200", st == 200 and r.get("ok") is True, str(r)[:120])
check("C6b probe 返回 backends", isinstance(r.get("backends"), list))


# ===========================================================================
print("\n=== D. 数据诚实性 ===")

# D1. 能力声明与实际行为一致
check("D1 腾讯 K 线成交额为估算值（能力申报一致）",
      caps_t["real_amount"] is False)
# 实测：amount == volume * close
calc = (df["volume"] * df["close"]).round(2)
diff = (df["amount"] - calc).abs().max()
check("D1b K 线 amount 确为 volume×close 推算",
      float(diff) < 0.02, f"最大偏差={diff}")

# D2. VWAP 源码里说明了精度限制
import inspect  # noqa: E402
import dip  # noqa: E402
vwap_doc = inspect.getdoc(dip.vwap) or ""
check("D2 VWAP 文档说明成交额是估算", "估算" in vwap_doc,
      "应说明 amount 为估算值")
check("D2b VWAP 文档指出真实成交额需 Wind", "Wind" in vwap_doc)

# D3. market.py 源码标注了估算
market_doc = (market._tencent_fetch_kline.__doc__ or "")
try:
    src = inspect.getsource(market._tencent_fetch_kline)
except Exception:
    src = ""
check("D3 fetch_kline 源码标注成交额为估算",
      "估算" in src or "volume × close" in src or "volume * close" in src)

# D4. Wind 不可用原因对用户可读
check("D4 Wind 不可用原因提到 WindPy",
      not wind_ok and "WindPy" in wind_reason, str(wind_reason)[:80])


# ===========================================================================
print("\n" + "=" * 60)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAILURES:
    print("  失败项：")
    for f in FAILURES:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
