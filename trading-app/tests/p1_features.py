"""Round 9: 下探过程实时预警 + 尾盘走势预判 + 午间复盘 + 日内自适应校准 (P1) 回归测试。

运行前需先启动 APP（python app.py）。

覆盖：
  A. dip.py      —— 单日切片、四阶段判定、Dip Quality Score、降级声明
  B. tail.py     —— 六因子权重、代理指标标注、三种结论、14:30 预警线、覆盖率
  C. noon       —— 午间快照与三剧本
  D. calibration —— 缩放边界、样本不足时诚实降级
  E. HTTP 层      —— /api/dip /api/tail /api/noon /api/calibration /api/p1/overview
  F. 前端静态检查 —— P1 面板元素与函数
"""
import json
import os
import re
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


import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import calibration  # noqa: E402
import dip  # noqa: E402
import tail  # noqa: E402


def mk(rows):
    return pd.DataFrame(
        [{"timestamps": r[0], "open": r[1], "high": r[2], "low": r[3],
          "close": r[4], "volume": r[5], "amount": r[4] * r[5]} for r in rows])


def day_rows(date, prices, vols=None, start="09:30", ohlc=True):
    """按分钟生成一天的 K 线。

    prices: list of close 价。
    ohlc=True 时，把上一根的收盘作为本根开盘（真实 K 线的连续关系），
    这样 close<open 才能正确表示「阴线」。
    """
    h, m = map(int, start.split(":"))
    rows = []
    prev = None
    for i, p in enumerate(prices):
        mm = m + i
        hh = h + mm // 60
        mm = mm % 60
        v = (vols[i] if vols else 1000)
        o = prev if (ohlc and prev is not None) else p
        hi, lo = max(o, p) * 1.001, min(o, p) * 0.999
        rows.append((f"{date} {hh:02d}:{mm:02d}", o, hi, lo, p, v))
        prev = p
    return rows


def full_day(date, closes, vols=None):
    """生成覆盖 09:30-11:30 + 13:00-15:00 的完整交易日。

    closes 会被顺序填入上午与下午两个时段（跳过午休）。
    """
    n = len(closes)
    half = n // 2 if n > 120 else n
    am = closes[:half]
    pm = closes[half:]
    rows = day_rows(date, am, vols[:half] if vols else None, start="09:30")
    if pm:
        rows += day_rows(date, pm, vols[half:] if vols else None, start="13:00")
    return rows


# ===========================================================================
print("\n=== A. dip.py 下探过程预警 ===")

# A1. 空/短数据
check("A1 空 DataFrame → ok=False", dip.dip_stage(pd.DataFrame()).get("ok") is False)
check("A1b 超短数据 → ok=False",
      dip.dip_stage(mk(day_rows("2026-01-05", [10.0] * 5))).get("ok") is False)

# A2. 单日切片：跨日数据只取最后一天
two_days = day_rows("2026-01-05", [10.0 - i * 0.05 for i in range(60)]) + \
           day_rows("2026-01-06", [9.0] * 60, start="09:30")
r2 = dip.dip_stage(mk(two_days))
check("A2 跨日数据只取最后交易日", r2.get("session_date") == "2026-01-06",
      f"got {r2.get('session_date')}")

# A3. 正常回撤：小幅波动
mild = day_rows("2026-01-05", [10.0, 10.02, 9.99, 10.01, 10.0] * 12)
r3 = dip.dip_stage(mk(mild))
check("A3 小幅波动判为正常回撤", r3["stage"] == "normal", f"got {r3['stage']}")
check("A3b 阶段含中文标签与系统行为",
      r3["stage_label"] == "正常回撤" and r3["stage_action"] == "不预警")

# A4. 恐慌释放：放量下跌 + 位于均价线下方
# 需要足够的常态量历史（否则放量基准被自身污染），故先给 60 根低量再放量下杀
panic_rows = day_rows("2026-01-05", [10.0] * 60, vols=[500] * 60) + \
             day_rows("2026-01-05", [10.0 - i * 0.05 for i in range(1, 31)],
                      vols=[5000] * 30, start="10:30", ohlc=False)
r4 = dip.dip_stage(mk(panic_rows))
check("A4 放量下跌判为恐慌释放", r4["stage"] in ("panic", "accelerating"),
      f"got {r4['stage']} vol_expand={r4['metrics']['vol_expand']} "
      f"down_ratio={r4['metrics']['down_ratio']}")
check("A4b 恐慌阶段给出出货型倾向（低分）", r4["score"] <= 50,
      f"score={r4['score']}")

# A5. 评分范围与类型标签
for nm, rr in (("A5", r3), ("A5b", r4)):
    check(f"{nm} 评分在 0~100", 0 <= rr["score"] <= 100, f"got {rr['score']}")
check("A5c 类型标签三选一",
      r3["dip_type"] in ("wash", "distribution", "mixed"))
check("A5d summary 形如「xx型下探 NN 分」", "分" in r3["summary"])

# A6. 缩量止跌（抛压衰竭）给高分
# 「抛压衰竭」的定义是**二次下杀**量能 < 首次，因此必须构造两段下杀：
#   第 1 段：放量下杀（4000）→ 第 2 段：缩量下杀（700）且低点不低于第 1 段
exh = day_rows("2026-01-05", [10.0] * 40, vols=[800] * 40) + \
      day_rows("2026-01-05", [10.0 - i * 0.03 for i in range(1, 16)],
               vols=[4000] * 15, start="10:10", ohlc=False) + \
      day_rows("2026-01-05", [9.55 + i * 0.005 for i in range(1, 11)],
               vols=[900] * 10, start="10:25", ohlc=False) + \
      day_rows("2026-01-05", [9.62 - i * 0.003 for i in range(1, 16)],
               vols=[700] * 15, start="10:35", ohlc=False) + \
      day_rows("2026-01-05", [9.58 + i * 0.004 for i in range(1, 11)],
               vols=[600] * 10, start="10:50", ohlc=False)
r6 = dip.dip_stage(mk(exh))
check("A6 二次缩量下杀被识别为抛压衰竭",
      r6["stage"] == "exhausted" or r6["score"] >= 55,
      f"stage={r6['stage']} score={r6['score']} segs={r6['metrics']['down_segments']} "
      f"why={r6['stage_why']}")

# A7. VWAP 计算正确性
vw_rows = [("2026-01-05 09:30", 10.0, 10.0, 10.0, 10.0, 100, 1000.0),
           ("2026-01-05 09:31", 20.0, 20.0, 20.0, 20.0, 100, 2000.0)]
vv = dip.vwap(mk(vw_rows))
check("A7 VWAP 累计均价正确（1000+2000)/(100+100)=15",
      abs(float(vv.iloc[-1]) - 15.0) < 0.01, f"got {vv.iloc[-1]}")

# A8. 降级声明
check("A8 声明 4 项不可得因子", len(dip.unavailable_factors()) == 4)
check("A8b 降级项 value 为 None",
      all(x["value"] is None for x in dip.unavailable_factors()))

# A9. 参数溯源
dp = dip.dip_params()
check("A9 含 4 个阈值且带依据",
      len(dp) == 4 and all(v["basis"] and v["source_label"] for v in dp.values()))
check("A9b 来源分档正确",
      dp["dip_normal_pct"]["source"] == "heuristic"
      and dp["dip_slope_k"]["source"] == "backtest")


# ===========================================================================
print("\n=== B. tail.py 尾盘预判 ===")

# B1. 六因子权重表与需求一致
W = tail.FACTOR_WEIGHTS
check("B1 六因子齐全", len(W) == 6)
check("B1b 权重为需求指定值",
      W["no_new_low"][0] == 0.25 and W["above_vwap"][0] == 0.30
      and W["active_buying"][0] == 0.20 and W["sector_follow"][0] == 0.10
      and W["history_deep"][0] == 0.08 and W["market_warm"][0] == 0.07)
check("B1c 权重合计 = 1.00",
      abs(sum(v[0] for v in W.values()) - 1.0) < 1e-9,
      f"got {sum(v[0] for v in W.values())}")

# B2. 全天数据可分析（覆盖 09:30-11:30 + 13:00-15:00，含 14:00-14:30 窗口）
full = full_day("2026-01-05",
                [10.0 + 0.002 * i for i in range(120)] +
                [10.24 - 0.001 * i for i in range(120)])
bt = tail.tail_analysis(mk(full))
check("B2 全天数据 ok=True", bt.get("ok") is True, f"err={bt.get('error')}")
check("B2b 概率在 0~100", 0 <= bt["prob_pct"] <= 100, f"got {bt['prob_pct']}")
check("B2c 三结论之一",
      bt["conclusion"]["type"] in ("real", "fake", "flat"),
      f"got {bt['conclusion']['type']}")
check("B2d 含 14:30 预警线对象", isinstance(bt.get("warning"), dict)
      and "label" in bt["warning"])
check("B2e breakdown 含六因子且各带权重/依据",
      len(bt["breakdown"]) == 6
      and all("weight" in b and "basis" in b and "detail" in b for b in bt["breakdown"]))
check("B2f 概率阈值 70%", bt["threshold_pct"] == 70.0)

# B3. 代理指标必须被显式标注
ab = [b for b in bt["breakdown"] if b["key"] == "active_buying"][0]
check("B3 主动买盘因子标记 proxy=True", ab["proxy"] is True)
check("B3b 详情说明为代理而非真实逐笔",
      "代理" in ab["detail"] or "占比" in ab["detail"], f"detail={ab['detail']}")

# B4. 数据不可得时 hit=None 且计降级（不编造）
sf = [b for b in bt["breakdown"] if b["key"] == "sector_follow"][0]
check("B4 无板块数据时 sector_follow 为 None",
      sf["hit"] is None and sf["degraded"] is True,
      f"hit={sf['hit']} deg={sf['degraded']}")

# B5. 覆盖率计算（缺因子时 <1 且给出说明）
check("B5 缺数据时 coverage < 1", bt["coverage"] < 1.0, f"got {bt['coverage']}")
check("B5b coverage_note 说明未计入分母", "未计入" in bt["coverage_note"])

# B6. 全因子可得时 coverage = 1
sector_ok = {"industry": "测试行业", "board": {"pct": 1.2}, "rank": 5, "total": 80}
idx_ok = {"name": "上证指数", "pct": 0.5}
bt2 = tail.tail_analysis(mk(full), sector=sector_ok, index_quote=idx_ok,
                         daily=mk(day_rows("2025-12-01", [10 + i * 0.1 for i in range(60)])))
check("B6 全因子可得时 coverage = 1.0", abs(bt2["coverage"] - 1.0) < 1e-9,
      f"got {bt2['coverage']} "
      f"miss={[b['key'] for b in bt2['breakdown'] if b['hit'] is None]}")

# B7. 三种结论可达性
check("B7 磨尾盘可判定（极窄振幅）",
      tail.tail_conclusion(0.5, {"tail_range_pct": 0.2, "vwap_gap_pct": 0,
                                 "tail_vol_expand": 1, "day_gain_pct": 0},
                           {}, 1.0)["type"] == "flat")
fake = tail.tail_conclusion(0.3, {"tail_range_pct": 2.0, "vwap_gap_pct": 5.0,
                                  "tail_vol_expand": 0.5, "day_gain_pct": 9.0},
                            {"above_vwap": {"hit": False}}, 1.0)
check("B7b 假拉升可判定（无量+偏离大+高位）", fake["type"] == "fake", f"got {fake['type']}")
real = tail.tail_conclusion(0.9, {"tail_range_pct": 3.0, "vwap_gap_pct": 0.5,
                                  "tail_vol_expand": 2.0, "day_gain_pct": 3.0},
                            {"above_vwap": {"hit": True},
                             "sector_follow": {"hit": True}}, 1.0)
check("B7c 真抢筹可判定（站回均价+放量+板块）", real["type"] == "real", f"got {real['type']}")

# B8. 权重被自迭代覆盖后来源变为 tuned
def fake_tuned(path, fallback):
    return 0.4 if path == "tail.w_above_vwap" else fallback


pt = tail.tail_params(fake_tuned)
av = [p for p in pt if p["key"] == "tail.w_above_vwap"][0]
check("B8 权重被覆盖后来源标为 tuned",
      av["source"] == "tuned" and av["value"] == 0.4, f"got {av['source']}")
check("B8b 覆盖后依据标注默认值", "默认值" in av["basis"])

# B9. 降级声明
check("B9 tail 声明 3 项不可得因子", len(tail.unavailable_factors()) == 3)

# B10. 涨跌幅/覆盖率边界：无因子可得时不崩
check("B10 全因子缺失不崩溃", tail.tail_analysis(mk(full))["ok"] is True)
check("B10b 超短数据 ok=False",
      tail.tail_analysis(mk(day_rows("2026-01-05", [10.0] * 5))).get("ok") is False)


# ===========================================================================
print("\n=== C. 午间复盘 ===")

noon = tail.noon_snapshot(mk(full))
check("C1 午间快照 ok=True", noon.get("ok") is True, f"err={noon.get('error')}")
check("C1b 含上午最低/最大跌幅/均价线/量能占比",
      all(k in noon["snapshot"] for k in ("am_low", "max_drop_pct", "am_vwap",
                                          "am_vol_share_pct")))
check("C2 三剧本齐全且概率合计 ≈100%",
      len(noon["scenarios"]) == 3
      and abs(sum(s["prob_pct"] for s in noon["scenarios"]) - 100.0) < 0.6,
      f"got {[s['prob_pct'] for s in noon['scenarios']]}")
check("C2b 剧本按概率降序且带 rank",
      [s["rank"] for s in noon["scenarios"]] == [1, 2, 3]
      and noon["scenarios"][0]["prob_pct"] >= noon["scenarios"][1]["prob_pct"])
check("C2c 剧本标签为需求指定的三种",
      sorted(s["label"] for s in noon["scenarios"]) ==
      sorted(["午后继续杀", "水下震荡磨筹码", "尾盘拉板"]))
check("C3 primary 指向排名第一的剧本",
      noon["primary"]["rank"] == 1)
check("C3b 上午 bar 数 <= 121（09:30-11:30）",
      noon["snapshot"]["am_bars"] <= 121, f"got {noon['snapshot']['am_bars']}")


# ===========================================================================
print("\n=== D. 日内自适应校准 ===")

# D1. 样本不足 → 不校准且诚实说明
d1 = calibration.calibrate(mk(day_rows("2026-01-05", [10.0] * 60)))
check("D1 单日数据不校准", d1["applied"] is False, f"got {d1}")
check("D1b 给出降级原因", "不足" in d1["reason"], f"reason={d1['reason']}")
check("D1c 未校准时沿用基准阈值",
      d1["effective"]["wave_pct"] == 3.0 and d1["scale"] == 1.0)
check("D1d 未校准时溯源标为 degraded",
      [p for p in d1["params"] if p["key"] == "calib_scale"][0]["source"] == "degraded")

# D2. 高波动日 → 上调且钳制在 2.0
rng = np.random.default_rng(42)
hi_rows = []
for d in range(4):
    hi_rows += day_rows(f"2026-01-0{d+1}", list(10 + rng.normal(0, 0.004, 60)))
hi_rows += day_rows("2026-01-05", list(10 + rng.normal(0, 0.05, 60)))
d2 = calibration.calibrate(mk(hi_rows))
check("D2 多日样本触发校准", d2["applied"] is True, f"got {d2}")
check("D2b 高波动日缩放 > 1 且 <= 2.0", 1.0 < d2["scale"] <= 2.0,
      f"scale={d2['scale']}")
check("D2c 生效阈值 = 基准 × 缩放",
      abs(d2["effective"]["wave_pct"] - 3.0 * d2["scale"]) < 0.02,
      f"got {d2['effective']['wave_pct']}")
check("D2d 量能阈值按平方根缩放（低于价格缩放）",
      d2["effective"]["vol_ratio"] < 2.5 * d2["scale"],
      f"vol={d2['effective']['vol_ratio']} price_scale={d2['scale']}")
check("D2e 说明为「上调」", "上调" in d2["reason"], f"reason={d2['reason']}")

# D3. 低波动日 → 下调
lo_rows = []
for d in range(4):
    lo_rows += day_rows(f"2026-01-0{d+1}", list(10 + rng.normal(0, 0.02, 60)))
lo_rows += day_rows("2026-01-05", list(10 + rng.normal(0, 0.0005, 60)))
d3 = calibration.calibrate(mk(lo_rows))
check("D3 低波动日缩放 < 1", d3["applied"] and d3["scale"] < 1.0,
      f"scale={d3.get('scale')}")
check("D3b 缩放不低于下限 0.5", d3["scale"] >= 0.5)
check("D3c 说明为「下调」", "下调" in d3["reason"], f"reason={d3['reason']}")

# D4. 溯源完整性
check("D4 校准含 4 条溯源",
      len(d2["params"]) == 4 and all(p["basis"] for p in d2["params"]))
check("D4b 校准窗口标注为 30 分钟",
      [p for p in d2["params"] if p["key"] == "calib_window"][0]["value"] == 30)


# ===========================================================================
print("\n=== E. HTTP 层 ===")

for path, key in (("/api/dip?symbol=000001", "stage"),
                  ("/api/tail?symbol=000001", "conclusion"),
                  ("/api/noon?symbol=000001", "scenarios"),
                  ("/api/calibration?symbol=000001", "effective")):
    st, r = req("GET", path)
    check(f"E1 GET {path.split('?')[0]} 200", st == 200, f"status={st}")
    check(f"E1b {path.split('?')[0]} 返回关键字段 {key}",
          r.get("ok") is not False and key in r, f"keys={list(r)[:8]}")

st, r = req("GET", "/api/p1/overview?symbol=000001")
check("E2 GET /api/p1/overview 200", st == 200, f"status={st}")
check("E2b 三合一含 dip/tail/noon/calibration",
      all(k in r for k in ("dip", "tail", "noon", "calibration")))
check("E2c dip 与 tail 均成功", r["dip"].get("ok") and r["tail"].get("ok"))

for p in ("/api/dip", "/api/tail", "/api/noon", "/api/calibration"):
    st, _ = req("GET", p)
    check(f"E3 {p} 缺 symbol → 400", st == 400, f"status={st}")

st, r = req("GET", "/api/dip?symbol=999999XY")
check("E4 非法代码返回 404 且给出可读错误",
      st == 404 and "未找到" in (r.get("error") or ""),
      f"status={st} err={str(r.get('error'))[:80]}")

st, r = req("GET", "/api/tail?symbol=000001&bars=20")
check("E5 小样本请求优雅返回（200 且结构完整，不抛异常）",
      st == 200 and ("conclusion" in r or r.get("ok") is False),
      f"status={st} ok={r.get('ok')} err={r.get('error')}")

st, r = req("GET", "/api/dip?symbol=000001&bars=30")
check("E5b /api/dip 小样本同样优雅",
      st == 200 and ("stage" in r or r.get("ok") is False),
      f"status={st} ok={r.get('ok')}")

st, r = req("GET", "/api/p1/overview?symbol=999999XY")
check("E6 overview 非法代码返回 404 而非 5xx", st == 404, f"status={st}")


# ===========================================================================
print("\n=== F. 前端静态检查 ===")

with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
    html = f.read()

for el in ("p1-body", "p1-scope", "p1-scope-body", "p1-range"):
    check(f"F1 存在元素 #{el}", f'id="{el}"' in html)
for fn in ("loadP1", "renderP1", "toggleP1Settings"):
    check(f"F2 存在函数 {fn}", re.search(r"function\s+" + fn + r"\s*\(", html) is not None)
check("F3 四阶段步骤条样式", "stage-step" in html and "st-panic" in html)
check("F4 评分条含游标", "dqs-marker" in html and "dqs-score" in html)
check("F5 尾盘结论三色徽章", "tail-badge" in html and "t-real" in html
      and "t-fake" in html and "t-flat" in html)
check("F6 六因子贡献行样式", "tail-factor" in html)
check("F7 代理指标徽章", "proxy-tag" in html and "代理" in html)
check("F8 14:30 预警线四色", "warn-line" in html and "w-green" in html
      and "w-red" in html)
check("F9 午间剧本卡（第一名高亮）", "scenario" in html and "rank1" in html)
check("F10 自适应校准面板", "自适应校准" in html and "缩放系数" in html)
check("F11 图表加载时联动 P1", "loadP1(force)" in html)
check("F12 判定口径面板可展开", "toggleP1Settings" in html)


# ===========================================================================
print("\n" + "=" * 60)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAILURES:
    print("  失败项：")
    for f in FAILURES:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
