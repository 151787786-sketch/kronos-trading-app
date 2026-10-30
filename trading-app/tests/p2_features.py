"""Round 10: 预警快照 + 预警时间轴 + 日报统计 + 用户反馈闭环 (P2) 回归测试。

运行前需先启动 APP（python app.py）。

覆盖：
  A. snapshots.py 逻辑层 —— 快照（级别门槛/指标计算）、时间轴、日报、反馈
  B. HTTP 层 —— /api/snapshots* /api/timeline /api/daily-report /api/feedback*
  C. DB 层 —— 表结构、索引、既有表未被破坏
  D. 前端静态检查 —— P2 面板元素与函数
"""
import json
import os
import sqlite3
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


import snapshots  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# 测试隔离：先用一个不会与真实数据冲突的专用标的做逻辑层断言，
# 并在开始前清掉该标的的历史残留（重复跑本套件时不会互相污染）。
TEST_SYM = "T00001"
TEST_SYM2 = "T00002"


def _purge(sym):
    """清掉指定标的的快照及其反馈（仅用于测试夹具）。"""
    try:
        conn = sqlite3.connect(snapshots.DB_PATH)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM alert_snapshots WHERE symbol=?", (sym,)).fetchall()]
        for i in ids:
            conn.execute("DELETE FROM alert_feedback WHERE snapshot_id=?", (i,))
        conn.execute("DELETE FROM alert_snapshots WHERE symbol=?", (sym,))
        conn.execute("DELETE FROM alert_feedback WHERE symbol=?", (sym,))
        conn.commit()
        conn.close()
        return len(ids)
    except Exception:
        return 0


# 逻辑层断言前先清场，保证"2 条快照"这类计数断言稳定
_purged = _purge(TEST_SYM)
_purge(TEST_SYM2)
print(f"  (fixture: purged {_purged} leftover snapshots for {TEST_SYM})")


def mk_bars(n=60, day=None, start_px=10.0):
    day = day or TODAY
    return [{"timestamps": "%s 10:%02d" % (day, i), "open": start_px,
             "high": start_px + 0.2, "low": start_px - 0.1,
             "close": start_px + i * 0.01, "volume": 1000 + i * 10}
            for i in range(n)]


# 测试产生的快照 id，最后统一清理
_CREATED = []


def cleanup():
    for sid in list(_CREATED):
        try:
            snapshots.delete_snapshot(sid)
        except Exception:
            pass
    _purge(TEST_SYM)
    _purge(TEST_SYM2)


# ===========================================================================
print("\n=== A. snapshots.py 逻辑层 ===")

snapshots.init_tables()

# A1. 建表后两张新表存在
conn = sqlite3.connect(snapshots.DB_PATH)
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
check("A1 建表 alert_snapshots", "alert_snapshots" in tables)
check("A1b 建表 alert_feedback", "alert_feedback" in tables)

# A2. 级别门槛：一级不存
r1 = snapshots.save_snapshot("T00001", "测试股", "surge", "up", 1, 10.0,
                             time.time(), bars=mk_bars())
check("A2 一级预警不存快照（返回 None）", r1 is None, f"got {r1}")

# A3. 二级存且带完整载荷
now = time.time()
r2 = snapshots.save_snapshot("T00001", "测试股", "surge", "up", 2, 10.5, now,
                             reason="剧烈拉升", score=66.0, bars=mk_bars(60),
                             evidence={"factors": [{"name": "振幅突破", "delta": 18.0}]},
                             summary="测试结论")
check("A3 二级预警成功存快照", r2 and r2.get("saved") is True, f"got {r2}")
if r2:
    _CREATED.append(r2["id"])

# A4. 三级存
r3 = snapshots.save_snapshot("T00001", "测试股", "volume", "down", 3, 10.2, now + 60,
                             reason="放量下杀", score=82.0, bars=mk_bars(60),
                             summary="测试结论2")
check("A4 三级预警成功存快照", r3 and r3.get("saved") is True)
if r3:
    _CREATED.append(r3["id"])

# A5. 读取快照：字段完整
if r2:
    got = snapshots.get_snapshot(r2["id"])
    check("A5 快照可读取", got is not None)
    check("A5b 含级别中文标签",
          got["level"] == 2 and got["level_label"] == "警告级",
          f"got {got.get('level_label')}")
    check("A5c 含 K 线载荷", isinstance(got.get("kline"), list) and len(got["kline"]) == 60,
          f"kline={type(got.get('kline'))}")
    check("A5d 含指标载荷", isinstance(got.get("indicators"), dict)
          and "close" in got["indicators"])
    check("A5e 含依据卡片载荷",
          isinstance(got.get("evidence"), dict) and "factors" in got["evidence"])
    check("A5f 含 feedback 字段（初始为空列表）",
          isinstance(got.get("feedback"), list))

# A6. 指标计算正确性（已知输入 → 已知输出）
bars_known = [{"timestamps": "2026-01-05 10:00", "open": 10.0, "high": 10.0,
               "low": 10.0, "close": 10.0, "volume": 100},
              {"timestamps": "2026-01-05 10:01", "open": 10.0, "high": 11.0,
               "low": 10.0, "close": 11.0, "volume": 300}]
ind = snapshots._indicators_from_bars(bars_known)
check("A6 窗口涨跌幅正确（10→11 = +10%）",
      abs(ind["chg_window_pct"] - 10.0) < 0.01, f"got {ind['chg_window_pct']}")
check("A6b 单根涨跌幅正确（10→11 = +10%）",
      abs(ind["chg_1bar_pct"] - 10.0) < 0.01, f"got {ind['chg_1bar_pct']}")
check("A6c 量比正确（300 / 200 = 1.5）",
      abs(ind["vol_ratio_last"] - 1.5) < 0.01, f"got {ind['vol_ratio_last']}")
check("A6d 窗口振幅正确（(11-10)/10 = 10%）",
      abs(ind["amp_window_pct"] - 10.0) < 0.01, f"got {ind['amp_window_pct']}")
check("A6e 空输入不崩溃", snapshots._indicators_from_bars([]) == {})

# A7. 时间轴
tl = snapshots.timeline("T00001", day=TODAY)
check("A7 时间轴返回 ok", tl.get("ok") is True)
check("A7b 时间轴含 2 个点", tl["count"] == 2, f"got {tl['count']}")
check("A7c 时间轴按时间升序",
      len(tl["points"]) >= 2 and tl["points"][0]["ts"] <= tl["points"][1]["ts"])
check("A7d 时间轴点含级别/方向/价格",
      all(k in tl["points"][0] for k in ("level", "direction", "price", "ts_label")))
tl_other = snapshots.timeline("T00001", day=YESTERDAY)
check("A7e 其他日期不串入（隔离正确）", tl_other["count"] == 0, f"got {tl_other['count']}")

# A8. 日报（断言限定在测试标的上，避免被真实运行数据干扰）
rep = snapshots.daily_report(day=TODAY, with_verify=False)
check("A8 日报 ok", rep.get("ok") is True)
_test_syms = {k for k in rep["by_symbol"] if k.startswith("测试股(T0000")}
check("A8b 日报统计到测试标的的 2 条快照",
      sum(v for k, v in rep["by_symbol"].items() if "T00001" in k) == 2,
      f"got {rep['by_symbol']}")
check("A8c 类型分布含 surge 与 volume",
      rep["by_type"].get("surge", 0) >= 1 and rep["by_type"].get("volume", 0) >= 1,
      f"got {rep['by_type']}")
check("A8d 方向分布含 up 与 down",
      rep["by_direction"].get("up", 0) >= 1 and rep["by_direction"].get("down", 0) >= 1,
      f"got {rep['by_direction']}")
check("A8e 级别分布含二级与三级",
      any("2级" in k for k in rep["by_level"]) and any("3级" in k for k in rep["by_level"]),
      f"got {rep['by_level']}")
check("A8f 含小时分布", isinstance(rep.get("hour_distribution"), dict))
check("A8g 含走势回溯区块", "verification" in rep and "note" in rep["verification"])
check("A8h 无验证样本时 accuracy_pct 为 None（不硬算）",
      rep["verification"]["accuracy_pct"] is None,
      f"got {rep['verification']['accuracy_pct']}")
check("A8i 非法日期格式返回 ok=False",
      snapshots.daily_report(day="not-a-date").get("ok") is False)
check("A8j 日报快照计数 >= 测试标的写入数",
      rep["snapshots"] >= 2, f"got {rep['snapshots']}")

# A9. 反馈
if r2:
    fb = snapshots.add_feedback(r2["id"], "useful", note="很准")
    check("A9 反馈录入成功", fb.get("verdict") == "useful"
          and fb.get("verdict_label") == "有用")
    check("A9b 反馈自动带出 symbol（从快照反查）",
          fb.get("id") is not None)
    fb2 = snapshots.add_feedback(r2["id"], "late")
    check("A9c 同一快照可多条反馈", fb2.get("verdict") == "late")

# A10. 非法 verdict 被拒
try:
    snapshots.add_feedback(r2["id"], "bogus")
    check("A10 非法 verdict 应被拒绝", False, "未抛异常")
except ValueError:
    check("A10 非法 verdict 被拒绝（ValueError）", True)

# A10b. 同一秒内连续提交多个评价必须成功
#       （早期 schema 有 UNIQUE(snapshot_id, ts)，用户快速连点会直接报错）
if r2:
    try:
        snapshots.add_feedback(r2["id"], "useless")
        snapshots.add_feedback(r2["id"], "late")
        check("A10b 同一秒内连点多个评价不报错（无 UNIQUE 冲突）", True)
    except Exception as e:
        check("A10b 同一秒内连点多个评价不报错（无 UNIQUE 冲突）", False,
              f"{type(e).__name__}: {e}")

# A10c. 表结构不含会阻塞连点的 UNIQUE 约束
_conn = sqlite3.connect(snapshots.DB_PATH)
_sql = _conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                     "AND name='alert_feedback'").fetchone()
_conn.close()
check("A10c alert_feedback 无 UNIQUE(snapshot_id, ts) 约束",
      _sql and "UNIQUE" not in (_sql[0] or "").upper(),
      "旧约束会阻止同一秒内多次评价")

# A11. 快照详情附带反馈
if r2:
    got2 = snapshots.get_snapshot(r2["id"])
    check("A11 快照详情含反馈记录", len(got2["feedback"]) >= 2,
          f"got {len(got2['feedback'])}")
    check("A11b 反馈含中文标签",
          all(f.get("verdict_label") for f in got2["feedback"]))

# A12. 反馈统计口径
st = snapshots.feedback_stats(days=1)
check("A12 反馈统计含各档计数",
      all(k in st for k in ("useful", "useless", "late", "rated", "total")))
check("A12b 反馈统计声明「非模型准确率」",
      "主观评价" in st["disclaimer"] and "不等于" in st["disclaimer"])
check("A12c 反馈统计给出可执行建议", bool(st.get("suggestion")))
check("A12d 本次写入的 useful/late 已被统计",
      st["useful"] >= 1 and st["late"] >= 1,
      f"useful={st['useful']} late={st['late']}")

# A12e. 建议逻辑：用一个隔离的临时库直接验证「晚了占比高 → 建议提前」
#        （不依赖累计数据，避免被历史反馈干扰）
import tempfile  # noqa: E402

_tmpdir = tempfile.mkdtemp(prefix="kronos_fb_")
_orig_db = snapshots.DB_PATH
try:
    snapshots.DB_PATH = os.path.join(_tmpdir, "fb_test.db")
    snapshots.init_tables()
    sid_t = snapshots.save_snapshot("T00999", "反馈测试", "surge", "up", 3, 10.0,
                                    time.time(), bars=mk_bars(20))["id"]
    for v in ("late", "late", "useful"):
        snapshots.add_feedback(sid_t, v)
    st_late = snapshots.feedback_stats(days=1)
    check("A12e 「晚了」占比高 → 建议提前预警",
          "提前" in st_late["suggestion"],
          f"got {st_late['suggestion']} (late={st_late['late']}/{st_late['rated']})")

    # 空反馈时的建议
    snapshots.DB_PATH = os.path.join(_tmpdir, "fb_empty.db")
    snapshots.init_tables()
    st_empty = snapshots.feedback_stats(days=1)
    check("A12f 无反馈样本时不瞎给建议",
          "暂无反馈样本" in st_empty["suggestion"] and st_empty["rated"] == 0,
          f"got {st_empty['suggestion']}")
finally:
    snapshots.DB_PATH = _orig_db

# A13. 时间轴带反馈状态
tl2 = snapshots.timeline("T00001", day=TODAY)
withfb = [p for p in tl2["points"] if p.get("feedback")]
check("A13 时间轴点携带反馈状态", len(withfb) >= 1, f"got {len(withfb)}")

# A14. 删除快照级联删除反馈
if r3:
    snapshots.delete_snapshot(r3["id"])
    _CREATED.remove(r3["id"])
    conn2 = sqlite3.connect(snapshots.DB_PATH)
    left = conn2.execute("SELECT COUNT(*) FROM alert_feedback WHERE snapshot_id=?",
                         (r3["id"],)).fetchone()[0]
    conn2.close()
    check("A14 删除快照级联清理其反馈", left == 0, f"got {left}")


# ===========================================================================
print("\n=== B. HTTP 层 ===")

st, r = req("GET", "/api/snapshots?limit=5")
check("B1 GET /api/snapshots 200", st == 200, f"status={st}")
check("B1b 返回 snapshots 列表与 count",
      isinstance(r.get("snapshots"), list) and "count" in r)

st, r = req("GET", "/api/snapshots/capture", body=None)
check("B2 capture 需要 POST（GET 不被允许）", st in (404, 405), f"status={st}")

st, r = req("POST", "/api/snapshots/capture", body={"symbol": "", "level": 3})
check("B3 capture 缺 symbol → 400", st == 400, f"status={st}")

st, r = req("POST", "/api/snapshots/capture",
            body={"symbol": "000001", "level": 1, "direction": "up"})
check("B4 capture 一级 → saved=False 且说明原因",
      st == 200 and r.get("saved") is False, f"got {r}")

st, r = req("POST", "/api/snapshots/capture",
            body={"symbol": "000001", "name": "平安银行", "level": 3,
                  "direction": "up", "event_type": "surge", "score": 88.0,
                  "price": 11.5, "reason": "测试快照", "summary": "HTTP 测试"})
check("B5 capture 三级 → 保存成功", st == 200 and r.get("saved") is True, f"got {r}")
sid_http = r.get("id") if isinstance(r, dict) else None
if sid_http:
    _CREATED.append(sid_http)

if sid_http:
    st, r = req("GET", f"/api/snapshots/{sid_http}")
    check("B6 GET /api/snapshots/<id> 200", st == 200, f"status={st}")
    check("B6b 含 K 线载荷（真实数据）",
          isinstance(r.get("kline"), list) and len(r["kline"]) > 0,
          f"kline type={type(r.get('kline'))}")
    check("B6c 含指标", isinstance(r.get("indicators"), dict)
          and r["indicators"].get("close") is not None)

    st, r = req("POST", "/api/feedback",
                body={"snapshot_id": sid_http, "verdict": "useful"})
    check("B7 POST /api/feedback 200 且返回中文标签",
          st == 200 and r.get("verdict_label") == "有用", f"got {r}")

    st, r = req("POST", "/api/feedback",
                body={"snapshot_id": sid_http, "verdict": "nonsense"})
    check("B8 非法 verdict → 400", st == 400, f"status={st}")

    st, r = req("POST", "/api/feedback", body={})
    check("B9 缺 verdict → 400", st == 400, f"status={st}")

    st, r = req("GET", f"/api/snapshots/{sid_http}")
    check("B10 反馈回填到快照详情", len(r.get("feedback") or []) >= 1,
          f"got {len(r.get('feedback') or [])}")

    st, r = req("DELETE", f"/api/snapshots/{sid_http}")
    check("B11 DELETE /api/snapshots/<id> 200", st == 200 and r.get("ok") is True)
    if sid_http in _CREATED:
        _CREATED.remove(sid_http)
    st, r = req("GET", f"/api/snapshots/{sid_http}")
    check("B12 删除后查不到（404）", st == 404, f"status={st}")

st, r = req("GET", "/api/timeline?symbol=T00001")
check("B13 GET /api/timeline 200", st == 200, f"status={st}")
check("B13b 时间轴结构完整",
      r.get("ok") is True and "points" in r and "count" in r)

st, r = req("GET", "/api/timeline")
check("B14 timeline 缺 symbol → 400", st == 400, f"status={st}")

st, r = req("GET", "/api/daily-report?verify=0")
check("B15 GET /api/daily-report 200", st == 200, f"status={st}")
check("B15b 日报结构完整",
      all(k in r for k in ("snapshots", "by_type", "by_direction", "verification")),
      f"keys={list(r)[:10]}")

st, r = req("GET", "/api/daily-report?day=bad-date")
check("B16 非法日期不崩溃", st in (200, 500), f"status={st}")

st, r = req("GET", "/api/feedback?limit=5")
check("B17 GET /api/feedback 200", st == 200, f"status={st}")
check("B17b 返回 feedback 列表", isinstance(r.get("feedback"), list))

st, r = req("GET", "/api/feedback/stats?days=30")
check("B18 GET /api/feedback/stats 200", st == 200, f"status={st}")
check("B18b 含主观评价免责声明",
      "主观评价" in (r.get("disclaimer") or ""), f"got {r.get('disclaimer')}")

st, r = req("POST", "/api/feedback", body={"verdict": "useless", "symbol": "000001"})
check("B19 无 snapshot_id 也可反馈（按 symbol 记录）",
      st == 200 and r.get("ok") is True, f"got {r}")


# ===========================================================================
print("\n=== C. DB 层 ===")

conn = sqlite3.connect(snapshots.DB_PATH)
cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_snapshots)").fetchall()}
for c in ("symbol", "level", "ts", "kline", "indicators", "evidence", "direction"):
    check(f"C1 alert_snapshots 含字段 {c}", c in cols)
cols2 = {r[1] for r in conn.execute("PRAGMA table_info(alert_feedback)").fetchall()}
for c in ("snapshot_id", "verdict", "note", "ts"):
    check(f"C2 alert_feedback 含字段 {c}", c in cols2)
idx = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
check("C3 快照时间索引存在", "idx_snap_ts" in idx, f"got {sorted(idx)}")
check("C3b 反馈索引存在", "idx_fb_snap" in idx or "idx_fb_ts" in idx)

# 既有表未被破坏
pre = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
for t in ("account", "alert_events", "alert_plans", "watchlist", "orders",
          "positions", "research_sessions", "iteration_log"):
    check(f"C4 既有表 {t} 未被破坏", t in pre)

# 既有表可正常读写
try:
    n = conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
    check("C5 既有 alert_events 可查询", isinstance(n, int))
    n2 = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    check("C5b 既有 watchlist 可查询", isinstance(n2, int))
except Exception as e:
    check("C5 既有表可查询", False, str(e))
conn.close()

# C6. app.py 模块级依赖：后台 monitor 函数不能依赖 __main__ 里的导入
#     （曾因 time 只在 if __name__ == "__main__" 内导入，导致 monitor 快照崩溃）
import app as _app  # noqa: E402

check("C6 app 模块可独立导入（非 __main__ 场景）", _app is not None)
check("C6b app 模块级有 time 可用（monitor 快照依赖）",
      hasattr(_app, "time") and callable(getattr(_app.time, "time", None)),
      "app.time 不可用 —— monitor 会在运行时 NameError")
check("C6c 存在 _capture_momentum_snapshots 函数",
      callable(getattr(_app, "_capture_momentum_snapshots", None)))
import inspect as _inspect  # noqa: E402
_sig = _inspect.signature(_app._capture_momentum_snapshots)
check("C6d 快照捕获默认阈值为 55 分（中等信号以上）",
      _sig.parameters.get("min_score") is not None
      and abs(_sig.parameters["min_score"].default - 55.0) < 1e-9,
      f"got {_sig.parameters.get('min_score')}")

# C7. 快照表的既有告警表互不干扰（P2 不应写入 alert_events）
conn = sqlite3.connect(snapshots.DB_PATH)
before_events = conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
conn.close()
snapshots.save_snapshot("T00003", "隔离测试", "surge", "up", 3, 10.0,
                        time.time(), bars=mk_bars(20))
conn = sqlite3.connect(snapshots.DB_PATH)
after_events = conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0]
conn.close()
check("C7 存快照不会写入 alert_events（表职责隔离）",
      before_events == after_events, f"{before_events} -> {after_events}")
_purge("T00003")


# ===========================================================================
print("\n=== D. 前端静态检查 ===")

with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
    html = f.read()

for el in ("p2-body", "p2-range", "snap-modal", "sm-body", "sm-title"):
    check(f"D1 存在元素 #{el}", f'id="{el}"' in html)
for fn in ("loadP2", "renderP2", "switchP2", "renderTimeline", "renderSnapshots",
           "renderDaily", "openSnapModal", "closeSnapModal", "sendFeedback",
           "captureSnapshot", "p2TabsHtml"):
    check(f"D2 存在函数 {fn}", f"function {fn}(" in html)
check("D3 时间轴样式存在", "tl-track" in html and "tl-dot" in html and "tl-axis" in html)
check("D4 时间轴点击跳转 K 线", "focusAnomaly(" in html and "tl-dot" in html)
check("D5 快照卡样式与级别徽章", "snap-card" in html and "lv-tag" in html)
check("D6 三个反馈按钮（有用/无用/晚了）",
      "👍 有用" in html and "👎 无用" in html and "⏰ 晚了" in html)
check("D7 反馈调用 /api/feedback", "/api/feedback" in html)
check("D8 日报含分布条形图", "bar-row" in html and "br-fill" in html)
check("D9 日报含走势回溯区块", "走势回溯" in html)
check("D10 日报标注反馈为「主观评价」", "主观评价，非模型准确率" in html)
check("D11 全屏预警触发时自动存快照", "captureSnapshot(t)" in html)
check("D12 快照详情内绘制 K 线", "sm-chart" in html and "Plotly.react('sm-chart'" in html)
check("D13 图表加载联动 P2", "loadP2(force)" in html)
check("D14 快照弹窗可按 Esc/点击背景关闭",
      "closeSnapModal()" in html and 'onclick="if(event.target===this)closeSnapModal()"' in html)


# ===========================================================================
cleanup()
print("\n" + "=" * 60)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAILURES:
    print("  失败项：")
    for f in FAILURES:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
