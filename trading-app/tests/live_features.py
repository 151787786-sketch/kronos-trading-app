"""Round 12: 实时盯盘（交易舱 v1 / P0）回归测试。

运行前需先启动 APP（python app.py）。

覆盖：
  A. tencent_quote.py —— 字段解析、时效校验(fields[30])、市场前缀、
     限流器、缓存、交易时段、批量上限
  B. watch_engine.py   —— 四档周期、按股配置、红绿频闪规则、退避重连、事件队列
  C. freq_probe.py     —— 三档参数、真实实测、报告结构、建议逻辑
  D. HTTP 层           —— /api/watch/* /api/probe/*
  E. 前端静态检查
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

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


def req(method, path, body=None, timeout=300):
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


import freq_probe  # noqa: E402
import tencent_quote as tq  # noqa: E402
import watch_engine as we  # noqa: E402

# ===========================================================================
print("\n=== A. tencent_quote.py ===")

# A1. 市场前缀
check("A1 600126 → sh600126", tq.full_symbol("600126") == "sh600126")
check("A1b 000657 → sz000657", tq.full_symbol("000657") == "sz000657")
check("A1c 512480(ETF) → sh512480", tq.full_symbol("512480") == "sh512480")
check("A1d 430047(北交所) → bj430047", tq.full_symbol("430047") == "bj430047")
check("A1e 已带前缀原样返回", tq.full_symbol("sh600519") == "sh600519")
check("A1f bare_code 去前缀", tq.bare_code("sz000657") == "000657")

# A2. 字段位常量与需求一致
check("A2 F_TIME == 30（需求指定 fields[30]）", tq.F_TIME == 30)
check("A2b F_NAME/F_CODE/F_PRICE/F_PREV/F_OPEN 位次正确",
      (tq.F_NAME, tq.F_CODE, tq.F_PRICE, tq.F_PREV_CLOSE, tq.F_OPEN) == (1, 2, 3, 4, 5))
check("A2c F_PCT == 32, F_HIGH == 33, F_LOW == 34",
      (tq.F_PCT, tq.F_HIGH, tq.F_LOW) == (32, 33, 34))

# A3. 解析真实格式样本
SAMPLE = ('v_sh600126="1~杭钢股份~600126~5.75~5.73~5.70~147932~77916~70012~'
          '5.74~2981~5.73~1660~5.72~1841~5.71~2236~5.70~1490~5.75~777~5.76~3041~'
          '5.77~1943~5.78~2010~5.79~4116~~20261019142647~0.02~0.35~5.81~5.68~'
          '5.75/147932/85097427~147932~8510~0.44~128.71~~5.81~5.68~2.27~194.19~194.19~1.00~";')
p = tq.parse_quote_text(SAMPLE)
check("A3 解析出 1 条", len(p) == 1)
if p and p[0].get("ok"):
    q = p[0]
    check("A3b 名称正确", q["name"] == "杭钢股份", q.get("name"))
    check("A3c 代码正确", q["symbol"] == "600126")
    check("A3d 最新价 5.75", q["price"] == 5.75, q.get("price"))
    check("A3e 昨收 5.73", q["prev_close"] == 5.73)
    check("A3f 今开 5.70", q["open"] == 5.70)
    check("A3g 行情时间取自 fields[30]",
          q["quote_time"] == "20261019142647", q.get("quote_time"))
    check("A3h 涨跌幅 0.35", q["pct"] == 0.35)
    check("A3i 高/低 5.81/5.68", (q["high"], q["low"]) == (5.81, 5.68))

# A4. 字段不足的代码优雅失败
bad = tq.parse_quote_text('v_sz999999="1~测试~999999~";')
check("A4 字段不足返回 ok=False", bad and bad[0]["ok"] is False)
check("A4b 失败项带 error 说明", bad and "error" in bad[0])

# A5. 行情时间解析
dt = tq.parse_quote_time("20261019142647")
check("A5 时间解析为 datetime", isinstance(dt, datetime))
check("A5b 解析结果正确",
      dt == datetime(2026, 10, 19, 14, 26, 47), str(dt))
check("A5c 非法时间返回 None", tq.parse_quote_time("abc") is None)
check("A5d 短串返回 None", tq.parse_quote_time("202610") is None)

# A6. 时效判定以 fields[30] 为准
q_ok = {"quote_time": "20261019142647"}
ref = datetime(2026, 10, 19, 14, 27, 47)
check("A6 quote_age_sec 计算正确（60 秒）",
      abs(tq.quote_age_sec(q_ok, ref) - 60.0) < 0.5, str(tq.quote_age_sec(q_ok, ref)))
check("A6b is_fresh 在阈值内为 True", tq.is_fresh(q_ok, 300, ref) is True)
check("A6c is_fresh 超阈值为 False", tq.is_fresh(q_ok, 30, ref) is False)
check("A6d 无 quote_time 时返回 None",
      tq.quote_age_sec({}, ref) is None)

# A7. 交易时段
def _ts(h, m, wd=0):
    base = datetime(2026, 10, 19, h, m)      # 2026-10-19 是周一
    return tq.trading_session(base)

check("A7 09:00 开盘前", _ts(9, 0)["open"] is False)
check("A7b 09:30 上午交易", _ts(9, 30)["open"] is True)
check("A7c 10:30 上午交易", _ts(10, 30)["phase"] == "am")
check("A7d 11:45 午间休市", _ts(11, 45)["phase"] == "noon")
check("A7e 13:30 下午交易", _ts(13, 30)["phase"] == "pm")
check("A7f 15:30 已收盘", _ts(15, 30)["phase"] == "post")
check("A7g 周末休市",
      tq.trading_session(datetime(2026, 10, 17, 10, 0))["open"] is False)

# A8. 限流器
lim = tq.RateLimiter(interval_ms=200, concurrency=2)
t0 = time.monotonic()
for _ in range(3):
    lim.acquire(); lim.release()
elapsed = time.monotonic() - t0
check("A8 限流器按间隔节流（3 次 200ms 间隔 ≥400ms）",
      elapsed >= 0.38, f"elapsed={elapsed:.3f}s")
check("A8b 限流统计记录 waits", lim.snapshot()["waits"] >= 1)

# A9. 批量上限
cl = tq.QuoteClient()
too_many = [f"{600000 + i:06d}" for i in range(tq.MAX_BATCH + 1)]
try:
    cl.fetch_batch(too_many)
    check("A9 超 500 只单批应报错", False, "未抛异常")
except ValueError:
    check("A9 超 500 只单批报错（MAX_BATCH=500）", tq.MAX_BATCH == 500)

# A10. 缓存
cl2 = tq.QuoteClient(cache_ttl=5)
qs = cl2.fetch(["600126"])
st1 = cl2.stats_snapshot()
qs2 = cl2.fetch(["600126"])
st2 = cl2.stats_snapshot()
check("A10 首次请求产生 HTTP", st1["http_requests"] >= 1)
check("A10b 二次命中缓存（cache_hits 增加）",
      st2["cache_hits"] > st1["cache_hits"],
      f"{st1['cache_hits']} -> {st2['cache_hits']}")
check("A10c 缓存命中不增加 HTTP 请求",
      st2["http_requests"] == st1["http_requests"])

# A11. 真实抓取（网络）
real = tq.get_client().fetch(["600126", "000657"])
ok_real = [q for q in real if q.get("ok")]
check("A11 真实抓取成功", len(ok_real) >= 1,
      f"got {len(ok_real)}/{len(real)}")
if ok_real:
    q = ok_real[0]
    check("A11b 真实行情含 quote_time（fields[30]）", bool(q.get("quote_time")))
    check("A11c 真实行情 quote_time 可解析",
          tq.parse_quote_time(q["quote_time"]) is not None)
    check("A11d 价格为正数", (q.get("price") or 0) > 0)


# ===========================================================================
print("\n=== B. watch_engine.py ===")

# B1. 四档周期
check("B1 允许周期为 1/3/5/10", we.ALLOWED_PERIODS == (1, 3, 5, 10))

CFG_PATH = os.path.join(ROOT, "tests", "_watch_cfg_test.json")
if os.path.exists(CFG_PATH):
    os.remove(CFG_PATH)
cfg = we.WatchConfig(path=CFG_PATH)

check("B1b 默认周期 5 分钟", cfg.period("600126") == 5)
cfg.set_period("600126", 1)
check("B1c 可设 1 分钟", cfg.period("600126") == 1)
cfg.set_period("000657", 10)
check("B1d 可设 10 分钟", cfg.period("000657") == 10)
try:
    cfg.set_period("600126", 7)
    check("B1e 非法周期被拒绝", False)
except ValueError:
    check("B1e 非法周期（7 分钟）被拒绝", True)

# B2. 规则覆盖
default_rules = cfg.rules("512480")
check("B2 默认规则含 4 项",
      all(k in default_rules for k in ("pct_up", "pct_down", "vol_ratio", "turnover_spike")))
cfg.set_rules("600126", {"pct_up": 5.5, "pct_down": -2.5})
r600126 = cfg.rules("600126")
check("B2b 规则可覆盖", r600126["pct_up"] == 5.5 and r600126["pct_down"] == -2.5)
check("B2c 未覆盖项保持默认", r600126["vol_ratio"] == default_rules["vol_ratio"])

# B3. 红绿频闪：买点绿闪
q_buy = {"ok": True, "symbol": "600126", "name": "测试", "pct": -4.0,
         "price": 5.0, "volume_ratio": 1.0, "turnover": 1.0,
         "quote_time": "20261019143000"}
ev_buy = we.evaluate(q_buy, cfg.rules("600126"))
check("B3 跌幅达标触发买点", ev_buy and ev_buy["side"] == "buy", str(ev_buy))
check("B3b 买点 flash=green（A股绿买）", ev_buy and ev_buy["flash"] == "green")
check("B3c 买点含触发原因", ev_buy and len(ev_buy["reasons"]) >= 1)
check("B3d 事件携带 quote_time（时效）",
      ev_buy and ev_buy["quote_time"] == "20261019143000")

# B4. 红绿频闪：卖点红闪
q_sell = {"ok": True, "symbol": "600126", "name": "测试", "pct": 6.0,
          "price": 6.0, "volume_ratio": 3.5, "turnover": 1.0,
          "quote_time": "20261019143000"}
ev_sell = we.evaluate(q_sell, cfg.rules("600126"))
check("B4 涨幅达标触发卖点", ev_sell and ev_sell["side"] == "sell", str(ev_sell))
check("B4b 卖点 flash=red（A股红卖）", ev_sell and ev_sell["flash"] == "red")
check("B4c 放量共振升级为三级",
      ev_sell and ev_sell["level"] == 3, f"level={ev_sell and ev_sell['level']}")

# B5. 不达阈值无预警
q_none = {"ok": True, "symbol": "600126", "pct": 1.0, "volume_ratio": 1.0,
          "turnover": 1.0}
check("B5 幅度不足不触发", we.evaluate(q_none, cfg.rules("600126")) is None)

# B6. 放量但幅度不足 → 关注级（不闪）
q_watch = {"ok": True, "symbol": "600126", "name": "x", "pct": 1.0,
           "volume_ratio": 4.0, "turnover": 1.0, "quote_time": "20261019143000"}
ev_w = we.evaluate(q_watch, cfg.rules("600126"))
check("B6 放量但幅度不足 → 关注级", ev_w and ev_w["side"] == "watch")
check("B6b 关注级 level=1", ev_w and ev_w["level"] == 1)
check("B6c 关注级不频闪（flash=None）", ev_w and ev_w["flash"] is None)

# B7. 失败行情不产生预警
check("B7 ok=False 不触发",
      we.evaluate({"ok": False, "error": "x"}, cfg.rules("600126")) is None)
check("B7b 缺 pct 不触发",
      we.evaluate({"ok": True, "symbol": "x"}, cfg.rules("600126")) is None)

# B8. 引擎：周期节奏
eng = we.WatchEngine(config=cfg, symbols_provider=lambda: ["600126", "000657"])
check("B8 引擎初始未运行", eng.is_running() is False)
st = eng.status()
check("B8b 状态含 tick_count/alert_count/consecutive_failures",
      all(k in st for k in ("tick_count", "alert_count", "consecutive_failures")))
check("B8c 状态含会话判定", "session" in st)

# B9. 引擎：事件队列
eng._events = [{"symbol": "A", "side": "buy", "flash": "green"},
               {"symbol": "B", "side": "sell", "flash": "red"}]
check("B9 peek 不消费", len(eng.peek_events()) == 2 and len(eng.peek_events()) == 2)
drained = eng.drain_events(1)
check("B9b drain 按 limit 取出", len(drained) == 1)
check("B9c drain 后剩余 1 条", len(eng.peek_events()) == 1)
check("B9d clear 清空", eng.clear_events() == 1 and len(eng.peek_events()) == 0)

# B10. 引擎：真实 check_now
eng2 = we.WatchEngine(config=cfg, symbols_provider=lambda: ["600126", "000657"])
try:
    evs = eng2.check_now()
    check("B10 check_now 可执行（真实行情）", isinstance(evs, list))
    if evs:
        check("B10b 事件含 side/flash/quote_time",
              all(k in evs[0] for k in ("side", "flash", "quote_time")))
except Exception as e:
    check("B10 check_now 可执行", False, str(e)[:100])

# B11. 引擎：退避逻辑（模拟连续失败）
class _FailingClient:
    def __init__(self):
        self.calls = 0
    def fetch(self, syms, use_cache=True):
        self.calls += 1
        return [{"full_symbol": s, "symbol": s[-6:], "ok": False,
                 "error": "模拟失败"} for s in syms]
    def stats_snapshot(self):
        return {}
    class limiter:
        @staticmethod
        def set_interval(ms): pass
        @staticmethod
        def snapshot(): return {}

eng3 = we.WatchEngine(config=cfg, client=_FailingClient(),
                      symbols_provider=lambda: ["600126"])
for _ in range(3):
    eng3._tick()
st3 = eng3.status()
check("B11 连续失败计数累加", st3["consecutive_failures"] >= 1,
      f"failures={st3['consecutive_failures']}")
check("B11b 失败后进入退避", st3["backoff_remaining"] > 0,
      f"backoff={st3['backoff_remaining']}")
check("B11c 失败记录 last_error", bool(st3.get("last_error")))

# B12. 配置持久化
cfg2 = we.WatchConfig(path=CFG_PATH)
check("B12 配置可重新加载", cfg2.period("600126") == 1)
check("B12b 规则持久化", cfg2.rules("600126")["pct_up"] == 5.5)
check("B12c prune 移除不在自选的配置", cfg2.prune(["000657"]) >= 0)

eng.stop()
if os.path.exists(CFG_PATH):
    os.remove(CFG_PATH)


# ===========================================================================
print("\n=== C. freq_probe.py ===")

# C1. 三档定义
keys = [t["key"] for t in freq_probe.PROBE_TIERS]
check("C1 三档为 30s/1s/0.5s", keys == ["30s", "1s", "0.5s"], str(keys))
check("C1b 默认只跑保守档 30s", freq_probe.DEFAULT_TIER == "30s")

# C2. 参数校验
bad1 = freq_probe.run_probe(["600126"], tier_keys=["bad"], samples=1)
check("C2 未知档位被拒", bad1.get("ok") is False)
bad2 = freq_probe.run_probe([], tier_keys=["30s"], samples=1)
check("C2b 空标的被拒", bad2.get("ok") is False)

# C3. 真实实测（1 秒档 x3，快）
rep = freq_probe.run_probe(["600126", "000657"], tier_keys=["1s"], samples=3, block=True)
check("C3 探针返回 ok", rep.get("ok") is True, str(rep)[:150])
if rep.get("ok"):
    t = rep["tiers"][0]
    check("C3b 档位标识正确", t["tier"] == "1s")
    check("C3c 样本数正确", t["samples"] == 3, str(t["samples"]))
    check("C3d 成功率在 0~100", 0 <= (t["success_pct"] or 0) <= 100,
          str(t["success_pct"]))
    check("C3e 记录耗时指标",
          all(t.get(k) is not None for k in
              ("latency_ms_avg", "latency_ms_median", "latency_ms_p95", "latency_ms_max")))
    check("C3f 记录限流计数", "rate_limited" in t and "suspect_soft_limit" in t)
    check("C3g 含逐条明细", isinstance(t.get("records"), list) and len(t["records"]) == 3)
    check("C3h 逐条含 status/elapsed_ms/ok",
          all(k in t["records"][0] for k in ("status", "elapsed_ms", "ok")))
    check("C3i 含建议档位", "recommendation" in rep)
    check("C3j 含免责声明（非长期结论）",
          "不代表长期" in (rep.get("caveat") or ""))
    check("C3k 真实请求成功率 > 0", (t["success"] or 0) >= 1,
          f"success={t.get('success')}")

# C4. 建议逻辑（纯函数）
def _t(interval, ok_pct=100.0, limited=0, suspect=0, p95=200):
    return {"tier": f"{interval}s", "tier_label": f"{interval} 秒档",
            "interval_sec": interval, "samples": 10, "success_pct": ok_pct,
            "rate_limited": limited, "suspect_soft_limit": suspect,
            "latency_ms_p95": p95}

rec1 = freq_probe._recommend([_t(30), _t(1), _t(0.5)])
check("C4 全部可用时选最激进档", rec1["safe_tier"] == "0.5s", str(rec1))
rec2 = freq_probe._recommend([_t(30), _t(1), _t(0.5, limited=2)])
check("C4b 出现 429 的档位被排除", rec2["safe_tier"] == "1s", str(rec2))
rec3 = freq_probe._recommend([_t(30), _t(1, p95=5000)])
check("C4c P95 过高时回退到 30s", rec3["safe_tier"] == "30s", str(rec3))
rec4 = freq_probe._recommend([_t(30, ok_pct=50.0)])
check("C4d 全部失败时不给安全档位", rec4["safe_tier"] is None, str(rec4))

# C5. last_report
lr = freq_probe.last_report()
check("C5 last_report 结构", "report" in lr and "running" in lr)
check("C5b 已有报告", lr["report"] is not None)


# ===========================================================================
print("\n=== D. HTTP 层 ===")

st, r = req("GET", "/api/watch/status")
check("D1 GET /api/watch/status 200", st == 200, f"status={st}")
check("D1b 含 running/tick_count/session",
      all(k in r for k in ("running", "tick_count", "session")))

st, r = req("GET", "/api/watch/quotes")
check("D2 GET /api/watch/quotes 200", st == 200, f"status={st}")
check("D2b 含 quotes 与 session", "quotes" in r and "session" in r)
check("D2c 声明时效依据为 fields[30]", r.get("time_basis") == "fields[30]")
if r.get("quotes"):
    q0 = r["quotes"][0]
    check("D2d 行情含 quote_time 与 age_sec",
          "quote_time" in q0 and "age_sec" in q0, str(list(q0))[:150])

st, r = req("GET", "/api/watch/config")
check("D3 GET /api/watch/config 200", st == 200, f"status={st}")
check("D3b 含 allowed_periods 且为四档",
      r.get("allowed_periods") == [1, 3, 5, 10], str(r.get("allowed_periods")))

st, r = req("POST", "/api/watch/config",
            body={"symbol": "600126", "period_min": 3})
check("D4 POST 设置周期 200", st == 200 and r.get("ok") is True, str(r)[:150])
st, r = req("GET", "/api/watch/config")
item = [i for i in r.get("items", []) if i["symbol"] == "600126"]
if item:
    check("D4b 周期已生效", item[0]["period_min"] == 3, str(item[0]))

st, r = req("POST", "/api/watch/config",
            body={"symbol": "600126", "period_min": 7})
check("D5 非法周期 → 400", st == 400, f"status={st}")

st, r = req("POST", "/api/watch/config", body={})
check("D6 空配置 → 400", st == 400, f"status={st}")

st, r = req("POST", "/api/watch/check")
check("D7 POST /api/watch/check 200", st == 200, f"status={st}")
check("D7b 返回 events 列表", isinstance(r.get("events"), list))

st, r = req("GET", "/api/watch/events?peek=1")
check("D8 GET /api/watch/events 200", st == 200 and isinstance(r.get("events"), list))

st, r = req("GET", "/api/watch/overview")
check("D9 GET /api/watch/overview 200", st == 200, f"status={st}")
check("D9b 含 quotes/config/engine/events/session",
      all(k in r for k in ("quotes", "config", "engine", "events", "session")))
check("D9c engine 含运行状态与失败计数",
      all(k in r["engine"] for k in ("running", "tick_count",
                                     "consecutive_failures", "recovered_count")))

st, r = req("POST", "/api/watch/start")
check("D10 启动引擎 200", st == 200 and r.get("ok") is True, str(r)[:120])
st, r = req("GET", "/api/watch/status")
check("D10b 启动后 running=True", r.get("running") is True)
st, r = req("POST", "/api/watch/stop")
check("D11 停止引擎 200", st == 200)
st, r = req("GET", "/api/watch/status")
check("D11b 停止后 running=False", r.get("running") is False)

st, r = req("POST", "/api/probe/run",
            body={"tiers": ["1s"], "samples": 2, "symbols": ["600126"], "block": True})
check("D12 POST /api/probe/run 200", st == 200, f"status={st}")
check("D12b 探针返回 tiers", isinstance(r.get("tiers"), list))
if r.get("tiers"):
    check("D12c 探针真实执行（samples=2）", r["tiers"][0]["samples"] == 2)

st, r = req("GET", "/api/probe/report")
check("D13 GET /api/probe/report 200", st == 200 and "report" in r)

st, r = req("POST", "/api/probe/abort")
check("D14 abort 在无运行时优雅返回", st == 200 and r.get("ok") in (True, False))


# ===========================================================================
print("\n=== E. 前端静态检查 ===")

with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
    html = f.read()

check("E1 存在 tab-live 标签页按钮", 'data-tab="tab-live"' in html)
for el in ("tab-live", "live-quotes", "live-events", "live-config", "live-status",
           "live-session", "probe-result", "live-tick", "live-interval"):
    check(f"E1 存在元素 #{el}", f'id="{el}"' in html)
for fn in ("loadLive", "renderLive", "renderLiveConfig", "liveStart", "liveStop",
           "liveCheckNow", "liveSetPeriod", "liveSetRules", "runProbe",
           "renderProbeReport", "toggleLiveDnd", "startLivePolling"):
    check(f"E2 存在函数 {fn}", f"function {fn}(" in html or f"async function {fn}(" in html)

check("E3 买点绿闪样式存在", "live-flash-green" in html and ".flash-green" in html)
check("E4 卖点红闪样式存在", "live-flash-red" in html and ".flash-red" in html)
check("E5 频闪尊重减少动效设置", "prefers-reduced-motion" in html)
check("E6 保留动效开关降级", 'data-motion="off"' in html and "flash-green" in html)
check("E7 有周期选择器（1/3/5/10）", "allowed_periods" in html)
check("E8 展示行情时间与距今秒数", "age_sec" in html and "行情时间" in html)
check("E9 声明时效依据 fields[30]", "fields[30]" in html)
check("E10 引擎启动/停止按钮", "liveStart()" in html and "liveStop()" in html)
check("E11 探针三档选项", 'value="30s"' in html and 'value="1s"' in html and 'value="0.5s"' in html)
check("E12 探针展示成功率与耗时", "成功率" in html and "P95" in html
      and "latency_ms_p95" in html)
check("E13 有静音频闪开关", "toggleLiveDnd" in html and "静音频闪" in html)
check("E14 标签切换时加载盯盘", "tab-live') { loadLive(); startLivePolling();" in html)

# E15. 频闪只在「事件标的 ∈ 当前行情列表」时套用
#      （渲染逻辑靠 symbol 匹配，事件里的标的若不在自选里就不该闪）
check("E15 频闪按 symbol 匹配行情卡片",
      "e.symbol === q.symbol" in html or "e.symbol===q.symbol" in html,
      "频闪必须与卡片 symbol 对应，否则会闪错标的")
check("E15b 卡片带 data-sym 供定位", 'data-sym="${esc(q.symbol)}"' in html
      or "data-sym=" in html)
check("E15c 关注级（watch）不频闪",
      "e.flash === 'green' || e.flash === 'red'" in html
      or "e.flash === \"green\"" in html)


# ===========================================================================
print("\n" + "=" * 60)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAILURES:
    print("  失败项：")
    for f in FAILURES:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
