"""Round 8: 盘中变化时刻线 + 前置预警线 + 参数溯源 + 依据卡片 + 全屏预警 (P0) 回归测试。

运行前需先启动 APP（python app.py）。

覆盖：
  A. 纯逻辑层（anomaly.py，不依赖网络）—— 阈值判定、竞价 bar 清洗、前置线、
     冷静期、因子贡献、参数溯源、降级声明
  B. HTTP 层（/api/anomaly*）—— 端点契约与数据结构
  C. 前端静态检查（index.html）—— 关键 UI 元素与函数存在
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

# 本机系统代理（Clash 等，HTTP_PROXY=127.0.0.1:7890）会拦截 127.0.0.1 的请求，
# 导致本地 API 调用被"积极拒绝"。绕开代理直连 —— 与 market.py 中
# session.trust_env = False 的做法一致。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=90):
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


# ===========================================================================
print("\n=== A. 逻辑层：anomaly.py ===")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import anomaly  # noqa: E402


def mk(rows):
    """rows: list of (ts, o, h, l, c, v)"""
    return pd.DataFrame(
        [{"timestamps": r[0], "open": r[1], "high": r[2], "low": r[3],
          "close": r[4], "volume": r[5]} for r in rows])


# --- A1. 空/短数据 ---
check("A1 空 DataFrame 返回 ok=False", anomaly.analyze("X", pd.DataFrame()).get("ok") is False)
check("A1b 短数据（3 根）返回 ok=False",
      anomaly.analyze("X", mk([("2026-01-01 09:30", 1, 1, 1, 1, 100)] * 3)).get("ok") is False)

# --- A2. 明显异动能被检出 ---
base = [("2026-01-05 09:%02d" % (30 + i), 10.0, 10.02, 9.98, 10.0, 1000) for i in range(20)]
spike = [("2026-01-05 09:50", 10.0, 10.9, 10.0, 10.8, 1000)]   # 振幅 9%
df_spike = mk(base + spike)
ev = anomaly.detect_anomalies(df_spike)
check("A2 9% 振幅异动被检出", len(ev) >= 1, f"got {len(ev)}")
check("A2b 异动方向判定为 up", ev and ev[-1]["direction"] == "up",
      f"got {ev[-1]['direction'] if ev else None}")
check("A2c 异动 magnitude ≈ 9", ev and abs(ev[-1]["magnitude"] - 9.0) < 0.1,
      f"got {ev[-1]['magnitude'] if ev else None}")

# --- A3. 平稳数据不误报 ---
flat = mk([("2026-01-05 09:%02d" % (30 + i), 10.0, 10.01, 9.99, 10.0, 1000) for i in range(30)])
check("A3 平稳数据无异动", len(anomaly.detect_anomalies(flat)) == 0)

# --- A4. 收盘集合竞价 bar 必须被排除（防虚假放量）---
auc = mk(base + [("2026-01-05 15:00", 10.0, 10.0, 10.0, 10.0, 999999)])
check("A4 15:00 收盘竞价 bar 未被误判为放量",
      len([e for e in anomaly.detect_anomalies(auc) if e["ts"].endswith("15:00")]) == 0)
dq = anomaly.data_quality(auc)
check("A4b data_quality 标记出竞价 bar", dq["excluded"] >= 1 and dq["valid"] < dq["bars"],
      f"dq={dq}")

# --- A5. 零成交/一字板 bar 被排除 ---
zero = mk(base + [("2026-01-05 10:00", 10.0, 10.0, 10.0, 10.0, 0)])
check("A5 零成交 bar 未被误判",
      len([e for e in anomaly.detect_anomalies(zero) if e["ts"].endswith("10:00")]) == 0)

# --- A6. 前置预警线 = 异动前 10 分钟（默认）---
params = anomaly.get_params()
check("A6 lead_minutes 默认 10", params["lead_minutes"]["value"] == 10)
leads = anomaly.lead_warnings(ev, params)
check("A6b 前置线早于异动 10 分钟",
      leads and leads[0]["lead_ts"] == "2026-01-05 09:40",
      f"got {leads[0]['lead_ts'] if leads else None}")

# --- A7. 开盘附近前置线应被取消并给出原因 ---
early = mk([("2026-01-05 09:%02d" % (30 + i), 10.0, 10.02, 9.98, 10.0, 1000) for i in range(20)]
           + [("2026-01-05 09:33", 10.0, 10.9, 10.0, 10.8, 1000)])
eev = anomaly.detect_anomalies(early)
eleads = anomaly.lead_warnings(eev, params)
early_cancel = [l for l in eleads if l["cancelled"]]
check("A7 开盘前 T 分钟内的前置线被取消", len(early_cancel) >= 1,
      f"cancelled={len(early_cancel)}")
check("A7b 取消时给出原因文字",
      early_cancel and "早于开盘" in (early_cancel[0]["cancel_reason"] or ""),
      f"reason={early_cancel[0]['cancel_reason'] if early_cancel else None}")

# --- A8. 冷静期：同方向密集异动被合并 ---
spam = []
for i in range(12):
    spam.append(("2026-01-05 10:%02d" % i, 10.0, 10.5, 10.0, 10.4, 1000))
spam_ev = anomaly.detect_anomalies(mk(spam), params)
check("A8 冷静期内同向异动被合并（12 根→少于 12）", len(spam_ev) < 12,
      f"got {len(spam_ev)}")

# --- A9. 参数溯源完整性 ---
p = anomaly.get_params()
for key in ("wave_pct", "vol_ratio", "lead_minutes", "cool_down_min"):
    ok = (key in p and p[key]["basis"] and p[key]["source_label"] and not p[key]["degraded"])
    check(f"A9 参数 {key} 带来源与依据", bool(ok))
check("A9b 来源分档正确（回测/经验）",
      p["wave_pct"]["source"] == "backtest" and p["lead_minutes"]["source"] == "heuristic")

# --- A10. 自迭代覆盖后来源变为 tuned ---
def fake_tuned(path, fallback):
    return 5.0 if path == "anomaly.wave_pct" else fallback


tuned_p = anomaly.get_params(fake_tuned)
check("A10 被自迭代覆盖的参数来源标为 tuned",
      tuned_p["wave_pct"]["source"] == "tuned" and tuned_p["wave_pct"]["value"] == 5.0,
      f"got {tuned_p['wave_pct']['source']}")
check("A10b 覆盖后依据文字标注了默认值",
      "默认值" in tuned_p["wave_pct"]["basis"])

# --- A11. 降级因子显式声明（不编造 Level-2 数值）---
deg = anomaly.unavailable_factors()
check("A11 声明 4 项不可得因子", len(deg) == 4, f"got {len(deg)}")
check("A11b 降级项 value 为 None（不编造数值）",
      all(d["value"] is None for d in deg))
check("A11c 降级项 source 标为 degraded",
      all(d["source"] == "degraded" and d["degraded"] for d in deg))

# --- A12. 依据卡片结构 ---
card = anomaly.evidence_card("000001", df_spike, ev, params, "测试股")
check("A12 卡片含结论文字", bool(card["conclusion"]))
check("A12b 卡片含因子明细且带 delta",
      len(card["factors"]) >= 2 and all("delta" in f for f in card["factors"]))
check("A12c 卡片 score 在 0~100", 0 <= card["score"] <= 100, f"got {card['score']}")
check("A12d 卡片含降级声明", len(card["degraded"]) == 4)

# --- A13. 因子贡献方向合理：振幅越大得分越高 ---
big = anomaly.evidence_card("S", mk(base + [("2026-01-05 09:50", 10.0, 11.5, 10.0, 11.4, 5000)]),
                            anomaly.detect_anomalies(mk(base + [("2026-01-05 09:50", 10.0, 11.5, 10.0, 11.4, 5000)])),
                            params)
check("A13 巨幅异动得分 >= 小幅异动得分", big["score"] >= card["score"],
      f"big={big['score']} small={card['score']}")

# --- A14. 无异动时卡片给出 none 级别 ---
none_card = anomaly.evidence_card("F", flat, [], params)
check("A14 无异动时 level=none", none_card["level"] == "none" and none_card["score"] == 0)

# --- A15. 回测参考 ---
# 回测需要 >=40 根且有足够后验区间，构造 80 根、异动出现在前段的样本
bt_rows = ([("2026-01-05 09:%02d" % (30 + i), 10.0, 10.02, 9.98, 10.0, 1000) for i in range(20)]
           + [("2026-01-05 09:%02d" % (50 + i), 10.0, 10.9, 10.0, 10.8, 1000) for i in range(3)]
           + [("2026-01-05 10:%02d" % i, 10.8, 10.82, 10.78, 10.8, 1000) for i in range(20)]
           + [("2026-01-05 10:%02d" % (20 + i), 10.8, 11.3, 10.8, 11.2, 1000) for i in range(3)]
           + [("2026-01-05 10:%02d" % (23 + i), 11.2, 11.22, 11.18, 11.2, 1000) for i in range(34)])
df_bt = mk(bt_rows)
check("A15-pre 回测样本 >= 40 根", len(df_bt) >= 40, f"got {len(df_bt)}")
bt = anomaly.backtest_reference(df_bt, params)
check("A15 回测返回结构完整",
      bt.get("ok") and "accuracy_pct" in bt and "sample" in bt and bt.get("note"),
      f"bt={bt}")
check("A15b 回测准确率在 0~100", 0 <= bt.get("accuracy_pct", -1) <= 100)
small_bt = anomaly.backtest_reference(mk(base), params)
check("A15c 样本不足时诚实返回 ok=False", small_bt.get("ok") is False,
      f"got {small_bt}")

# --- A16. analyze 聚合输出 ---
agg = anomaly.analyze("000001", df_spike, name="测试股")
check("A16 analyze 返回 ok=True", agg.get("ok") is True)
check("A16b 含 events / leads / card / params / degraded / data_quality",
      all(k in agg for k in ("events", "leads", "card", "params", "degraded", "data_quality")))
check("A16c events 内嵌 lead_ts 字段",
      all("lead_ts" in e for e in agg["events"]))
check("A16d 声明数据口径为 1m", agg["range"]["interval"] == "1m")


# ===========================================================================
print("\n=== B. HTTP 层：/api/anomaly* ===")

st, r = req("GET", "/api/anomaly/params")
check("B1 GET /api/anomaly/params 200", st == 200, f"status={st}")
check("B1b 返回 params/degraded/defaults",
      all(k in r for k in ("params", "degraded", "defaults")))
check("B1c params 每项带 source_label 与 basis",
      all(x.get("source_label") and x.get("basis") for x in r.get("params", [])))

st, r = req("GET", "/api/anomaly?symbol=000001&bars=300")
check("B2 GET /api/anomaly 200", st == 200, f"status={st}")
if st == 200 and r.get("ok"):
    check("B2b 返回 events 列表", isinstance(r.get("events"), list))
    check("B2c 返回 card 且带 score",
          isinstance(r.get("card"), dict) and r["card"].get("score") is not None)
    check("B2d 返回 data_quality",
          isinstance(r.get("data_quality"), dict) and "valid" in r["data_quality"])
    check("B2e 降级因子已声明", len(r.get("degraded") or []) == 4)
    if r.get("events"):
        e0 = r["events"][0]
        check("B2f 事件含前端渲染所需字段",
              all(k in e0 for k in ("ts", "direction", "magnitude", "reason", "lead_ts")),
              f"keys={list(e0.keys())}")
else:
    check("B2b-d 数据可用性", False, f"resp={str(r)[:200]}")

st, r = req("GET", "/api/anomaly?symbol=000001&backtest=0")
check("B3 backtest=0 时不返回回测", st == 200 and (r.get("card", {}).get("backtest") in (None, {})),
      f"got {r.get('card', {}).get('backtest')}")

st, r = req("GET", "/api/anomaly")
check("B4 缺 symbol 返回 400", st == 400, f"status={st}")

st, r = req("GET", "/api/anomaly?symbol=999999XY")
check("B5 非法代码返回 404 且给出可读错误", st == 404 and "未找到" in (r.get("error") or ""), f"status={st}")

st, r = req("GET", "/api/anomaly/scan?threshold=70&limit=3")
check("B6 GET /api/anomaly/scan 200", st == 200, f"status={st}")
check("B6b 返回 triggers 列表", isinstance(r.get("triggers"), list))
check("B6c 回显 threshold", r.get("threshold") == 70.0, f"got {r.get('threshold')}")

st, r = req("GET", "/api/anomaly/watchlist?limit=3")
check("B7 GET /api/anomaly/watchlist 200", st == 200, f"status={st}")
check("B7b 返回 items/ranked", isinstance(r.get("items"), list) and isinstance(r.get("ranked"), list))

st, r = req("GET", "/api/anomaly/scan?threshold=999")
check("B8 越界 threshold 被钳制而不报错", st == 200, f"status={st}")


# ===========================================================================
print("\n=== C. 前端静态检查：index.html ===")

html_path = os.path.join(ROOT, "templates", "index.html")
with open(html_path, encoding="utf-8") as f:
    html = f.read()

for el in ("flash-overlay", "flash-silence", "alert-dock", "anomaly-body",
           "alert-settings-panel", "anomaly-show-marks", "anomaly-show-lead"):
    check(f"C1 存在元素 #{el}", f'id="{el}"' in html)

for fn in ("loadAnomaly", "renderAnomaly", "applyAnomalyMarks", "toggleAnomalyMarks",
           "silenceFlash", "focusAnomaly", "pollAlerts", "applyAlertSettings",
           "requestNotifyPermission", "testFlashAlert", "startAlertPolling", "inDnd"):
    check(f"C2 存在函数 {fn}", re.search(r"function\s+" + fn + r"\s*\(", html) is not None
          or re.search(r"\b" + fn + r"\s*[:=]", html) is not None)

check("C3 一键静默按钮文案为「闭嘴」", "🤐 闭嘴" in html)
check("C4 支持 Esc 静默", "e.key === 'Escape'" in html or 'e.key === "Escape"' in html)
check("C5 支持点击遮罩静默", 'onclick="silenceFlash(' in html)
check("C6 包含 Vibration API 调用", "navigator.vibrate" in html)
check("C7 包含桌面通知 API", "Notification.requestPermission" in html)
check("C8 包含 Web Audio 提示音", "AudioContext" in html)
check("C9 全屏红绿双向样式", ".dir-up" in html and ".dir-down" in html)
check("C10 支持 prefers-reduced-motion 降级", "prefers-reduced-motion" in html)
check("C11 有冷静期逻辑", "silencedUntil" in html)
check("C12 有免打扰逻辑", "dndStart" in html and "dndEnd" in html)
check("C13 有单一标的锁定逻辑", "lockedSymbols" in html and "isLocked" in html)
check("C14 有强度滑块", 'id="ap-strength"' in html and 'type="range"' in html)
check("C15 时刻线以 Plotly shapes 绘制", "layout.shapes" in html)
check("C16 前置预警线使用虚线区分", "dash: 'dot'" in html or 'dash: "dot"' in html)
check("C17 依据卡片渲染因子贡献", "ev-factor" in html and "ef-delta" in html)
check("C18 参数溯源徽章样式存在", "ev-src" in html and "src-backtest" in html)
check("C19 降级声明区块存在", "ev-degraded" in html)
check("C20 异动时间轴可点击跳转", "ev-chip" in html and "focusAnomaly" in html)
check("C21 图例说明前置预警线含义", "前置预警" in html)
check("C22 设置本地持久化", "localStorage" in html and "kronos_alert_prefs" in html)
check("C23 图表重绘已注入时刻线", "applyAnomalyMarks(layout, bars)" in html)


# ===========================================================================
print("\n" + "=" * 60)
print(f"  通过 {PASS} · 失败 {FAIL}")
if FAILURES:
    print("  失败项：")
    for f in FAILURES:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
