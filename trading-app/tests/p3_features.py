"""Round 11: 科学依据中心（溯源/诊断/模拟器）+ 高级算法预判 V3.1 (P3) 回归测试。

运行前需先启动 APP（python app.py）。

覆盖：
  A. evidence_center.py —— 溯源总览、诊断、模拟器（真实回测）、周月校准
  B. advanced.py        —— 动量衰减（5 种场景）、盘口降级、ML 概率分（含拒绝训练）
  C. HTTP 层            —— /api/evidence/* /api/simulate /api/advanced/* /api/p3/overview
  D. 前端静态检查
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


def req(method, path, body=None, timeout=180):
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

import advanced  # noqa: E402
import evidence_center as ec  # noqa: E402


def mk(rows):
    return pd.DataFrame(
        [{"timestamps": r[0], "open": r[1], "high": r[2], "low": r[3],
          "close": r[4], "volume": r[5]} for r in rows])


def synth(direction=1, n=60, step=0.02, flat_tail=0, start_px=10.0):
    rows, px = [], start_px
    for i in range(n):
        if i < n - flat_tail:
            px += direction * step
            rows.append((f"2026-01-05 10:{i%60:02d}", px - direction * step,
                         px + 0.005, px - direction * step * 1.2, px, 1000))
        else:
            rows.append((f"2026-01-05 10:{i%60:02d}", px, px + 0.002, px - 0.002, px, 1000))
    return mk(rows)


# ===========================================================================
print("\n=== A. evidence_center.py ===")

# A1. 溯源总览
pt = ec.provenance_table()
check("A1 溯源总览 ok", pt.get("ok") is True)
check("A1b 参数总数 > 25", pt["total"] > 25, f"got {pt['total']}")
check("A1c 含分组", len(pt["groups"]) >= 5, f"got {len(pt['groups'])}")
check("A1d 每项含 key/effective/source/basis",
      all(all(k in it for k in ("key", "effective", "source", "basis"))
          for it in pt["items"]))

# A2. 三个模块的参数都被收录（曾漏收 tail）
keys = {it["key"] for it in pt["items"]}
for k in ("anomaly.wave_pct", "anomaly.lead_minutes", "dip.dip_normal_pct",
          "tail.w_above_vwap", "tail.w_no_new_low"):
    check(f"A2 收录参数 {k}", k in keys, f"missing {k}")

# A3. 六因子权重齐全
tail_w = [it for it in pt["items"] if it["key"].startswith("tail.w_")]
check("A3 尾盘六因子权重全部收录", len(tail_w) == 6, f"got {len(tail_w)}")
check("A3b 权重值与需求一致",
      {it["key"]: it["effective"] for it in tail_w}.get("tail.w_above_vwap") == 0.3)

# A4. 降级项显式标注
degraded = [it for it in pt["items"] if it["degraded"]]
check("A4 降级项被标注", len(degraded) >= 4, f"got {len(degraded)}")
check("A4b 降级项来源为「数据不可用」",
      all(it["source"] == "数据不可用" for it in degraded))
check("A4c 降级项 effective 为 None（不编造数值）",
      all(it["effective"] is None for it in degraded))

# A5. 改动标记
check("A5 changed_count 与实际相符",
      pt["changed_count"] == sum(1 for it in pt["items"] if it["changed"]))

# A6. 诊断
dg = ec.diagnose(days=30)
check("A6 诊断 ok", dg.get("ok") is True)
check("A6b 含 findings 且每项有 level/title/action",
      len(dg["findings"]) >= 3
      and all(all(k in f for k in ("level", "title", "action")) for f in dg["findings"]))
check("A6c level 取值合法",
      all(f["level"] in ("ok", "warn", "info") for f in dg["findings"]))
check("A6d level_counts 与 findings 一致",
      dg["level_counts"]["ok"] == sum(1 for f in dg["findings"] if f["level"] == "ok"))
check("A6e 声明结论仅供参考", "仅供参考" in dg["note"] or "不构成" in dg["note"])

# A7. 模拟器（真实数据）
df_real = pd.read_csv(os.path.join(ROOT, "data", "000001_1m_400.csv"))
sim = ec.simulate("000001", df_real, {"wave_pct": 2.0, "vol_ratio": 1.8})
check("A7 模拟器 ok", sim.get("ok") is True, f"err={sim.get('error')}")
check("A7b 含 baseline / modified 两组",
      "baseline" in sim and "modified" in sim)
check("A7c 两组都有触发次数与兑现率字段",
      all(all(k in sim[g] for k in ("triggers", "sample")) for g in ("baseline", "modified")))
check("A7d 放宽阈值触发次数增加",
      sim["modified"]["triggers"] > sim["baseline"]["triggers"],
      f"{sim['baseline']['triggers']} -> {sim['modified']['triggers']}")
check("A7e delta_triggers 计算正确",
      sim["delta_triggers"] == sim["modified"]["triggers"] - sim["baseline"]["triggers"])
check("A7f 给出变化解读", len(sim["notes"]) >= 2)
check("A7g 含免责声明", "不代表未来表现" in sim["disclaimer"])

# A8. 收紧阈值 → 触发减少
sim2 = ec.simulate("000001", df_real, {"wave_pct": 9.0, "vol_ratio": 5.0})
check("A8 收紧阈值触发次数减少",
      sim2["modified"]["triggers"] <= sim2["baseline"]["triggers"],
      f"{sim2['baseline']['triggers']} -> {sim2['modified']['triggers']}")

# A9. 数据不足必须拒绝（不编造）
sim3 = ec.simulate("X", df_real.head(20))
check("A9 数据不足时拒绝模拟", sim3.get("ok") is False)
check("A9b 拒绝时说明原因且强调未编造",
      "不足" in sim3["error"] and "编造" in sim3["error"],
      f"got {sim3['error'][:70]}")
check("A9c 拒绝时不返回 baseline（不造假数据）", "baseline" not in sim3)

# A10. 周月校准
cs = ec.calibration_schedule()
check("A10 校准计划 ok", cs.get("ok") is True)
check("A10b 含 weekly/monthly 且各有 due/last_run",
      all(k in cs[w] for w in ("weekly", "monthly") for k in ("due", "last_run")))
check("A10c 周度周期描述含「周」", "周" in cs["weekly"]["schedule"])
check("A10d 月度周期描述含「月」", "月" in cs["monthly"]["schedule"])

# A11. 标记校准（用临时 tuning 文件避免污染真实配置）
import nightly  # noqa: E402
import tempfile  # noqa: E402
_tmp = tempfile.mkdtemp(prefix="kronos_cal_")
_orig_tuning = nightly.TUNING_PATH
try:
    nightly.TUNING_PATH = os.path.join(_tmp, "tuning.json")
    r = ec.mark_calibration("weekly")
    check("A11 标记周度校准成功", r.get("ok") and r["kind"] == "weekly")
    cfg = nightly.load_tuning()
    check("A11b 写入 last_weekly_calib_ts", "last_weekly_calib_ts" in cfg)
    cs2 = ec.calibration_schedule()
    check("A11c 标记后周度不再 due", cs2["weekly"]["due"] is False,
          f"got {cs2['weekly']}")
    try:
        ec.mark_calibration("bogus")
        check("A11d 非法 kind 被拒绝", False)
    except ValueError:
        check("A11d 非法 kind 被拒绝", True)
finally:
    nightly.TUNING_PATH = _orig_tuning


# ===========================================================================
print("\n=== B. advanced.py 高级预判 ===")

# B1. 动量：持续上涨不应报衰减
m_up = advanced.momentum_analysis(synth(1, 60, 0.02))
check("B1 持续上涨不报衰减",
      m_up["ok"] and not m_up["decayed"] and not m_up["flat"],
      f"decayed={m_up.get('decayed')} flat={m_up.get('flat')}")

# B2. 动量：真正横盘 → flat
flat_rows = [("2026-01-05 10:%02d" % i, 10.0, 10.005, 9.995, 10.0, 1000)
             for i in range(40)]
m_flat = advanced.momentum_analysis(mk(flat_rows))
check("B2 真横盘判为 flat", m_flat["ok"] and m_flat["flat"] is True,
      f"got flat={m_flat.get('flat')}")
check("B2b 横盘时不报 decayed（无动量≠衰减）",
      m_flat["decayed"] is False and m_flat["warning"]["type"] == "flat")
check("B2c 横盘说明中解释不构成拐点",
      "不构成" in m_flat["warning"]["why"])

# B3. 动量：涨完横住 → 应识别为停滞/衰减（而非误报横盘）
m_stall = advanced.momentum_analysis(synth(1, 60, 0.03, flat_tail=35))
check("B3 涨完横住识别为停滞/衰减",
      m_stall["ok"] and (m_stall["decayed"] or m_stall.get("stalled")),
      f"flat={m_stall.get('flat')} decayed={m_stall.get('decayed')} "
      f"stalled={m_stall.get('stalled')}")
check("B3b 该情形不应误判为「本来就横盘」", m_stall["flat"] is False)

# B4. 动量：数据不足拒绝
m_short = advanced.momentum_analysis(mk(flat_rows[:10]))
check("B4 数据不足时拒绝计算", m_short["ok"] is False)

# B5. 动量：基于真实数据能跑通
m_real = advanced.momentum_analysis(df_real)
check("B5 真实数据动量分析 ok", m_real["ok"] is True)
check("B5b 含速度/加速度/强度字段",
      all(k in m_real for k in ("velocity", "acceleration", "strength")))
check("B5c 含参数溯源", len(m_real["params"]) == 3)

# B6. 盘口能力声明
cap = advanced.orderbook_capability()
check("B6 明确声明盘口数据不可得", cap["available"] is False)
check("B6b 说明需要哪些数据", len(cap["requires"]) >= 3)
check("B6c 列出降级因子", len(cap["degraded_factors"]) == 3)
check("B6d 降级因子 value 为 None",
      all(d["value"] is None for d in cap["degraded_factors"]))
check("B6e 提供代理方案并标注", cap["proxy_available"] is True)

# B7. 盘口代理
px = advanced.orderbook_proxy(df_real)
check("B7 代理指标 ok", px.get("ok") is True)
check("B7b 明确标注 proxy=True", px["proxy"] is True)
check("B7c 含免责声明（不是真实盘口）",
      "不是" in px["disclaimer"] and "Level-2" in px["disclaimer"])
check("B7d 含量能与振幅字段",
      all(k in px for k in ("volume_ratio", "amp_recent_pct", "amp_prev_pct")))

# B8. ML 训练（真实数据）
model = advanced.train_model(df_real)
check("B8 ML 训练 ok", model.get("ok") is True, f"err={model.get('error')}")
check("B8b 含样本量与准确率",
      all(k in model for k in ("samples", "train_acc", "baseline_rate")))
check("B8c 训练准确率与基线率均在一个合理区间",
      0 <= model["train_acc"] <= 1 and 0 <= model["baseline_rate"] <= 1)
check("B8d 提供 informative 判断",
      isinstance(model.get("informative"), bool))
check("B8e 含特征重要性", len(model["importance"]) == len(advanced.FEATURE_NAMES))
check("B8f 特征重要性之和 ≈ 1",
      abs(sum(i["importance"] for i in model["importance"]) - 1.0) < 0.02,
      f"got {sum(i['importance'] for i in model['importance'])}")
check("B8g 含样本外准确率字段", "oos_acc" in model)

# B9. ML 样本不足必须拒绝
model_short = advanced.train_model(df_real.head(30))
check("B9 样本不足时拒绝训练", model_short.get("ok") is False)
check("B9b 标记 insufficient", model_short.get("insufficient") is True)
check("B9c 说明拒绝理由（不给无意义分数）",
      "拒绝训练" in model_short["error"], f"got {model_short['error'][:60]}")

# B10. ML 打分
if model.get("ok"):
    pr = advanced.ml_predict(model, df_real)
    check("B10 打分 ok", pr.get("ok") is True)
    check("B10b 概率在 0~1", 0 <= pr["prob_up"] <= 1, f"got {pr['prob_up']}")
    check("B10c prob_up + prob_down = 1",
          abs(pr["prob_up"] + pr["prob_down"] - 1.0) < 1e-6)
    check("B10d 含基线对比与边际", "baseline_rate" in pr and "edge" in pr)
    check("B10e 信号三选一", pr["signal"] in ("偏多", "偏空", "中性"))
    check("B10f 提示模型是否优于基线",
          "基线" in pr["confidence_note"])
    check("B10g 含当前特征值", len(pr["features_now"]) == len(advanced.FEATURE_NAMES))

# B11. 用坏模型打分应拒绝
check("B11 无效模型拒绝打分",
      advanced.ml_predict({}, df_real).get("ok") is False)

# B12. 模型不可信时必须说出来（关键诚实性检查）
if model.get("ok"):
    if model["train_acc"] <= model["baseline_rate"] + 0.02:
        check("B12 模型未优于基线时给出警示",
              "信息量有限" in model["caveat"], f"got {model['caveat'][:60]}")
    else:
        check("B12 模型优于基线时说明有信息量",
              "信息量" in model["caveat"])


# ===========================================================================
print("\n=== C. HTTP 层 ===")

st, r = req("GET", "/api/evidence/params")
check("C1 GET /api/evidence/params 200", st == 200, f"status={st}")
check("C1b 返回 groups/items/total",
      all(k in r for k in ("groups", "items", "total")))

st, r = req("GET", "/api/evidence/diagnose?days=30")
check("C2 GET /api/evidence/diagnose 200", st == 200, f"status={st}")
check("C2b 返回 findings", isinstance(r.get("findings"), list))

st, r = req("GET", "/api/evidence/schedule")
check("C3 GET /api/evidence/schedule 200", st == 200, f"status={st}")
check("C3b 含 weekly/monthly", "weekly" in r and "monthly" in r)

st, r = req("POST", "/api/simulate",
            body={"symbol": "000001", "wave_pct": 2.0, "vol_ratio": 1.8})
check("C4 POST /api/simulate 200", st == 200, f"status={st}")
check("C4b 返回 baseline/modified", "baseline" in r and "modified" in r)
check("C4c 用真实数据（bars>0）", (r.get("bars") or 0) > 0, f"bars={r.get('bars')}")

st, r = req("POST", "/api/simulate", body={})
check("C5 simulate 缺 symbol → 400", st == 400, f"status={st}")

st, r = req("GET", "/api/advanced/momentum?symbol=000001")
check("C6 GET /api/advanced/momentum 200", st == 200, f"status={st}")
check("C6b 返回 velocity/acceleration",
      r.get("ok") is True and "velocity" in r, f"got {str(r)[:100]}")

st, r = req("GET", "/api/advanced/orderbook?symbol=000001")
check("C7 GET /api/advanced/orderbook 200", st == 200, f"status={st}")
check("C7b 含 capability 且声明不可得",
      r["capability"]["available"] is False)
check("C7c 含 proxy 结果", "proxy" in r)

st, r = req("GET", "/api/advanced/ml?symbol=000001")
check("C8 GET /api/advanced/ml 200", st == 200, f"status={st}")
check("C8b 返回 model 与 prediction",
      "model" in r and "prediction" in r, f"keys={list(r)[:8]}")

st, r = req("GET", "/api/p3/overview?symbol=000001")
check("C9 GET /api/p3/overview 200", st == 200, f"status={st}")
check("C9b 含 momentum/orderbook/ml",
      all(k in r for k in ("momentum", "orderbook", "ml")))

for p in ("/api/advanced/momentum", "/api/advanced/orderbook", "/api/advanced/ml",
          "/api/p3/overview"):
    st, _ = req("GET", p)
    check(f"C10 {p} 缺 symbol → 400", st == 400, f"status={st}")

st, r = req("GET", "/api/advanced/momentum?symbol=999999XY")
check("C11 非法代码返回 404 且可读", st == 404 and "未找到" in (r.get("error") or ""),
      f"status={st}")


# ===========================================================================
print("\n=== D. 前端静态检查 ===")

with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as f:
    html = f.read()

for el in ("p3-body", "p3-range"):
    check(f"D1 存在元素 #{el}", f'id="{el}"' in html)
for fn in ("loadP3", "renderP3", "switchP3", "renderParams", "renderDiagnose",
           "renderSimulator", "runSimulation", "renderSimResult", "renderAdvanced",
           "resetSimInputs", "p3TabsHtml"):
    check(f"D2 存在函数 {fn}", f"function {fn}(" in html)

for el in ("sim-wave", "sim-vol", "sim-cool", "sim-hz", "sim-result"):
    check(f"D3 模拟器控件 #{el}", f'id="{el}"' in html)
check("D4 模拟器有强度滑块（range）",
      'type="range" id="sim-wave"' in html and 'type="range" id="sim-hz"' in html)
check("D5 模拟器调用 /api/simulate", "/api/simulate" in html)
check("D6 模拟器展示前后对比卡", "sim-card" in html and "sim-compare" in html)
check("D7 模拟器含免责声明展示", "disclaimer" in html)
check("D8 溯源表样式与改动高亮", "pt-table" in html and "pt-changed" in html)
check("D9 降级项样式", "pt-degraded" in html)
check("D10 诊断项三色", "diag-item" in html and "d-ok" in html and "d-warn" in html)
check("D11 动量徽章四态", "adv-badge" in html and "a-flat" in html
      and "a-decay" in html and "a-reversal" in html)
check("D12 盘口降级区块", "degraded-box" in html)
check("D13 ML 概率条含基线标记", "prob-bar" in html and "pb-base" in html)
check("D14 ML 展示训练/基线/样本外三指标",
      "训练准确率" in html and "基线率" in html and "样本外准确率" in html)
check("D15 高级预判调用 /api/p3/overview", "/api/p3/overview" in html)
check("D16 页面初始化加载 P3", "loadP3();" in html)

# D17. 模板可被 Jinja 解析 —— 关键回归防线
#      曾因在 JS 里写 ${{...}} 对象字面量，被 Jinja 当成表达式解析，
#      导致首页直接 500（整个 APP 打不开）。这里直接编译模板来拦截此类问题。
from jinja2 import Environment, FileSystemLoader  # noqa: E402
_env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")))
try:
    _env.get_template("index.html")
    _jinja_ok, _jinja_err = True, ""
except Exception as e:
    _jinja_ok, _jinja_err = False, f"{type(e).__name__}: {str(e)[:150]}"
check("D17 index.html 可被 Jinja 正常解析（首页不会 500）", _jinja_ok, _jinja_err)
check("D17b 模板内无 Jinja 冲突的 ${{ 写法", "${{" not in html)
check("D17c 模板内无裸 {{ 写法", "{{" not in html)

st, r = req("GET", "/")
check("D18 首页可正常访问（HTTP 200）", st == 200, f"status={st}")
# 注意：req() 对非 JSON 响应只回传前 300 字符，无法用来断言页面中后段的内容。
# 这里单独取完整首页 HTML 来验证 P3 面板确实被服务端渲染出去了。
try:
    with _OPENER.open(BASE + "/", timeout=30) as _resp:
        _home = _resp.read().decode("utf-8", "replace")
    _home_ok = True
except Exception as _e:
    _home, _home_ok = "", False
check("D18b 首页返回完整 HTML", _home_ok and len(_home) > 50000,
      f"len={len(_home)}")
check("D18c 首页含 P3 面板标记", "p3-body" in _home and "科学依据中心" in _home)
check("D18d 首页含 P0/P1/P2 面板标记",
      all(k in _home for k in ("flash-overlay", "p1-body", "p2-body")))


# ===========================================================================
print("\n" + "=" * 60)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAILURES:
    print("  失败项：")
    for f in FAILURES:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
