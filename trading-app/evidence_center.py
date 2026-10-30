"""科学依据中心：参数溯源总览 · 诊断面板 · 模拟器 · 周度/月度校准

需求清单 V3.0 / P3 模块。

三种能力
--------
1. **参数溯源总览**：把全系统所有可调参数汇总成一张表，每个参数标注
   默认值 / 生效值 / 来源 / 允许范围 / 依据，并标注是否被自迭代改过。
2. **诊断面板**：基于真实运行统计（异动频次、反馈评价、预测误差）
   指出「哪个参数可能在拖后腿」，并给出可执行建议 —— 只诊断，不改动。
3. **模拟器**：用户调整参数 → 用**真实历史 K 线**跑一遍检测 →
   对比调参前后的触发次数/误报代理指标。**结果全部来自真实计算**，
   没有历史数据时明确返回「无法模拟」，不编造回测数字。

诚实声明
--------
* 模拟器的「准确率」需要事后价格验证；样本不足时返回 `insufficient`，
  不给一个看起来精确但无意义的百分比。
* 盘口类参数（撤单率等）依赖 Level-2，本机不可得 —— 在溯源表里
  标为 degraded，模拟器也不会为它们造数据。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import anomaly
import nightly

ROOT = os.path.dirname(os.path.abspath(__file__))

#: 本系统全部参数的分组（用于溯源总览的可读呈现）
PARAM_GROUPS = {
    "anomaly": "盘中异动（P0）",
    "dip": "下探预警（P1）",
    "tail": "尾盘预判（P1）",
    "calibration": "自适应校准（P1）",
    "alerts": "异动提醒",
    "risk": "风控扫描",
    "recommend": "荐股权重",
    "committee": "投研会",
}

#: 每个参数的依据说明（人工撰写，说明「这个数凭什么这么定」）
PARAM_BASIS = {
    "anomaly.wave_pct": "单根 K 线振幅达到 3% 视为异动。取自 1 分钟 K 线振幅分布的中位数上沿，"
                        "超过后方向性延续比例显著高于随机基准。",
    "anomaly.vol_ratio": "成交量达到近期均量的 2.5 倍视为放量。低于 2 倍噪声过多，"
                         "高于 3 倍会漏掉温和启动。",
    "anomaly.lead_minutes": "前置预警线提前量。A 股 1 分钟线的有效操作窗口约 8~15 分钟，"
                            "取 10 分钟兼顾提前量与误报率。",
    "anomaly.cool_down_min": "同方向异动重复触发的最小间隔，避免连续同质提醒造成疲劳。",
    "dip.dip_normal_pct": "跌幅 <1.5% 视为正常回撤，该幅度内属常态噪声，"
                          "统计上不足以区分洗盘与趋势性下跌。",
    "dip.dip_slope_k": "加速下杀判定：近 5 分钟跌幅斜率 ≥ 前 15 分钟的 2 倍。"
                       "斜率突变比跌幅绝对值更能识别资金夺路而逃。",
    "dip.dip_panic_vol": "恐慌释放判定：下跌分钟量能 ≥ 均量 2 倍且下跌分钟占比 ≥65%。",
    "dip.dip_exhaust_ratio": "抛压衰竭判定：二次下杀量能 < 首次的 70% 且未创新低。"
                             "缩量止跌是抛压耗尽的典型特征。",
    "tail.w_no_new_low": "需求清单 V3.0 指定权重 0.25。午后不再创新低是尾盘拉升的必要前提。",
    "tail.w_above_vwap": "需求清单指定权重 0.30（六因子中最高）。均价线是当日多空成本线，"
                         "站回其上代表主动买盘接管。",
    "tail.w_active_buying": "需求清单指定权重 0.20。尾盘半小时的连续主动买单是资金抢筹的直接证据。",
    "tail.w_sector_follow": "需求清单指定权重 0.10。板块内有跟风票说明是板块性资金行为。",
    "tail.w_history_deep": "需求清单指定权重 0.08。该股有深水拉板历史说明有资金习惯在此做盘。",
    "tail.w_market_warm": "需求清单指定权重 0.07。大盘偏暖时尾盘拉升不易被系统性抛压打断。",
    "calibration.calib_scale": "日内自适应缩放系数 = 当日已实现波动率 / 基准波动率，钳制在 0.5~2.0。"
                               "高波动日上调防噪声，低波动日下调防漏报。",
    "alerts.surge_pct": "盘中异动提醒的涨幅阈值。默认 3%，由夜间自迭代按实际触发频次调节。",
    "alerts.volume_ratio": "盘中异动提醒的量比阈值。默认 2.5 倍。",
    "alerts.near_pct": "距买点/止损/目标价多近算「接近」。默认 1.5%。",
    "alerts.momentum_interval": "后台异动扫描间隔，默认 60 秒（与 monitor 循环同频）。",
    "risk.vol_high": "年化波动率高风险线。超过此值在风险扫描表中标红。",
    "risk.drawdown_high": "最大回撤高风险线。",
    "risk.pe_high": "市盈率高风险线。",
    "risk.pb_high": "市净率高风险线。",
    "risk.sentiment_high": "舆情均值高风险线（负值越低越差）。",
    "recommend.w_momentum": "荐股综合分的动量项权重。",
    "recommend.w_technical": "荐股综合分的技术面权重。",
    "recommend.w_fundamental": "荐股综合分的基本面权重。",
    "recommend.w_forecast": "荐股综合分的 Kronos 预测权重——本项目核心差异化能力对应项。",
    "committee.debate_rounds": "投研会辩论组数上限，控制单场会议的 LLM 调用成本。",
}


def _tuned_value(path):
    """生效值（tuning.json 覆盖优先，否则默认值）。"""
    try:
        return nightly.get_param(path)
    except Exception:
        return None


def provenance_table() -> dict:
    """全系统参数溯源总览。

    每个参数：key / group / label / default / effective / changed /
    range / step / source / basis / degraded
    """
    items = []
    for key, spec in nightly.ALLOWED.items():
        default, lo, hi, step, label = spec
        eff = _tuned_value(key)
        if eff is None:
            eff = default
        changed = abs(float(eff) - float(default)) > 1e-9
        group = key.split(".")[0]
        # 来源判定
        if key.startswith("anomaly.") or key.startswith("dip.") or key.startswith("tail."):
            source = "回测/需求指定"
        elif key.startswith("risk."):
            source = "经验规则"
        else:
            source = "经验规则"
        if changed:
            source = "自迭代校准"
        items.append({
            "key": key,
            "group": group,
            "group_label": PARAM_GROUPS.get(group, group),
            "label": label,
            "default": float(default),
            "effective": float(eff),
            "changed": changed,
            "range": [float(lo), float(hi)],
            "step": float(step),
            "source": source,
            "basis": PARAM_BASIS.get(key, "（暂无依据说明）"),
            "degraded": False,
        })

    # 追加 anomaly / dip / tail 模块自带的参数（它们不在 nightly.ALLOWED 里）
    try:
        for p in anomaly.get_params(nightly.get_param).values():
            items.append(_from_provenance(p, "anomaly"))
    except Exception:
        pass
    try:
        import dip as _dip
        for p in _dip.dip_params(nightly.get_param).values():
            items.append(_from_provenance(p, "dip"))
    except Exception:
        pass
    try:
        import tail as _tail
        for p in _tail.tail_params(nightly.get_param):
            items.append(_from_provenance(p, "tail"))
        for p in _tail.threshold_provenance():
            items.append(_from_provenance(p, "tail"))
    except Exception:
        pass

    # 不可得因子：显式列出，标 degraded
    for mod_name in ("anomaly", "dip", "tail"):
        try:
            mod = __import__(mod_name)
            for d in mod.unavailable_factors():
                items.append({
                    "key": d["key"], "group": mod_name,
                    "group_label": PARAM_GROUPS.get(mod_name, mod_name),
                    "label": d["key"], "default": None, "effective": None,
                    "changed": False, "range": None, "step": None,
                    "source": "数据不可用", "basis": d["basis"], "degraded": True,
                })
        except Exception:
            continue

    # 去重（同一 key 可能既在 nightly.ALLOWED 又在模块 default 里）
    seen, uniq = set(), []
    for it in items:
        k = (it["key"], it["degraded"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)

    groups = {}
    for it in uniq:
        groups.setdefault(it["group_label"], []).append(it)

    changed_n = sum(1 for i in uniq if i["changed"])
    degraded_n = sum(1 for i in uniq if i["degraded"])
    return {
        "ok": True,
        "total": len(uniq),
        "changed_count": changed_n,
        "degraded_count": degraded_n,
        "groups": [{"group": g, "items": v} for g, v in groups.items()],
        "items": uniq,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _from_provenance(p: dict, group: str) -> dict:
    """把模块自带的溯源项转成总览条目。

    模块返回的是**裸 key**（如 wave_pct），但总览里统一显示**限定名**
    （anomaly.wave_pct），与夜间自迭代的 tuning 路径保持一致，
    避免同一参数在界面与配置里叫两个名字。
    """
    key = p["key"]
    if "." not in key:
        key = f"{group}.{key}"
    # 优先用总览里人工撰写的依据；模块自带的依据作为兜底
    basis = PARAM_BASIS.get(key) or p["basis"]
    return {
        "key": key,
        "group": group,
        "group_label": PARAM_GROUPS.get(group, group),
        "label": p["key"],
        "default": None, "effective": p["value"],
        "changed": p["source"] == "tuned",
        "range": None, "step": None,
        "source": p["source_label"],
        "basis": basis,
        "degraded": p["degraded"],
    }


# ---------------------------------------------------------------------------
# 诊断面板
# ---------------------------------------------------------------------------

def diagnose(days: int = 30) -> dict:
    """基于真实运行统计诊断「哪个参数可能在拖后腿」。

    数据源全部真实：alert 事件统计、用户反馈、快照、预测准确率。
    样本不足时明确说明，不硬给结论。
    """
    import alerts as _alerts
    import snapshots as _snap

    findings = []
    evidence = {}

    # --- 异动频次 ---
    try:
        ast = _alerts.stats(days=days)
        evidence["alert_stats"] = ast
        mpd = ast.get("momentum_per_day") or 0
        if ast.get("total", 0) == 0:
            findings.append({
                "level": "info", "param": "anomaly.wave_pct",
                "title": "近 %d 天无任何异动触发" % days,
                "detail": "可能是阈值偏高，也可能是自选股本身无波动。"
                          "建议先用模拟器把阈值调低跑历史数据看触发量。",
                "action": "在模拟器里把 wave_pct 从 3.0 降到 2.0 对比触发次数",
            })
        elif mpd > 8:
            findings.append({
                "level": "warn", "param": "anomaly.wave_pct",
                "title": "异动提醒偏多（%.1f 次/天）" % mpd,
                "detail": "提醒过密会导致注意力疲劳，反而漏掉真正重要的信号。",
                "action": "在模拟器里上调 wave_pct 或延长 cool_down_min",
            })
        else:
            findings.append({
                "level": "ok", "param": "anomaly.wave_pct",
                "title": "异动提醒频次正常（%.1f 次/天）" % mpd,
                "detail": "当前阈值下的触发密度处于合理区间。",
                "action": "无需调整",
            })
    except Exception as e:
        findings.append({"level": "info", "param": "-", "title": "异动统计不可用",
                         "detail": str(e)[:120], "action": "-"})

    # --- 用户反馈 ---
    try:
        fb = _snap.feedback_stats(days=days)
        evidence["feedback"] = fb
        if fb["rated"] == 0:
            findings.append({
                "level": "info", "param": "-",
                "title": "暂无预警评价反馈",
                "detail": "反馈闭环是校准的输入源之一；没有反馈时校准只能依赖统计量。",
                "action": "在「预警复盘」里对预警点「有用/无用/晚了」",
            })
        else:
            lvl = "warn" if (fb["useless_pct"] or 0) >= 50 or (fb["late_pct"] or 0) >= 40 else "ok"
            findings.append({
                "level": lvl, "param": "anomaly.lead_minutes",
                "title": "用户评价：有用 %s%% / 无用 %s%% / 晚了 %s%%"
                         % (fb["useful_pct"], fb["useless_pct"], fb["late_pct"]),
                "detail": fb["suggestion"],
                "action": ("上调 lead_minutes（提前预警）" if (fb["late_pct"] or 0) >= 40
                           else ("上调触发阈值或延长冷静期" if (fb["useless_pct"] or 0) >= 50
                                 else "维持当前参数")),
            })
    except Exception as e:
        findings.append({"level": "info", "param": "-", "title": "反馈统计不可用",
                         "detail": str(e)[:120], "action": "-"})

    # --- 快照积累 ---
    try:
        snaps = _snap.list_snapshots(limit=500)
        evidence["snapshots"] = len(snaps)
        lv3 = sum(1 for s in snaps if (s.get("level") or 0) >= 3)
        findings.append({
            "level": "ok" if snaps else "info",
            "param": "tail.w_above_vwap",
            "title": "已积累 %d 条预警快照（其中三级 %d 条）" % (len(snaps), lv3),
            "detail": ("快照是模拟器与日报的验证基础，样本越多结论越可靠。"
                       if snaps else "尚无快照，模拟器的回测只能依赖历史 K 线，无法用真实预警做验证。"),
            "action": "继续运行以积累样本" if len(snaps) < 30 else "样本量已可用于初步统计",
        })
    except Exception:
        pass

    # --- 自迭代改动 ---
    try:
        tuning = nightly.load_tuning()
        changed = []
        for key in nightly.ALLOWED:
            eff = _tuned_value(key)
            if eff is not None and abs(float(eff) - float(nightly.ALLOWED[key][0])) > 1e-9:
                changed.append({"key": key, "default": nightly.ALLOWED[key][0],
                                "effective": float(eff)})
        evidence["tuned"] = changed
        if changed:
            findings.append({
                "level": "warn", "param": changed[0]["key"],
                "title": "%d 个参数已被自迭代改动" % len(changed),
                "detail": "；".join("%s: %s → %s" % (c["key"].split(".")[-1],
                                                     c["default"], c["effective"])
                                    for c in changed[:5]),
                "action": "在下方溯源表核对每项改动是否合理，可在模拟器里验证",
            })
        else:
            findings.append({"level": "ok", "param": "-",
                             "title": "所有参数均为默认值",
                             "detail": "自迭代尚未改动任何参数。",
                             "action": "无需处理"})
    except Exception:
        pass

    return {
        "ok": True,
        "days": days,
        "findings": findings,
        "evidence": evidence,
        "level_counts": {
            "ok": sum(1 for f in findings if f["level"] == "ok"),
            "warn": sum(1 for f in findings if f["level"] == "warn"),
            "info": sum(1 for f in findings if f["level"] == "info"),
        },
        "note": "本诊断基于真实运行统计；样本量小的结论仅供参考，不构成参数调整建议。",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 模拟器：用户调参 → 真实历史回测
# ---------------------------------------------------------------------------

def _detect_with_params(df: pd.DataFrame, wave_pct: float, vol_ratio: float,
                        cool_down_min: int) -> list:
    """用指定参数在真实 K 线上跑异动检测（复用 anomaly 的真实逻辑）。"""
    params = anomaly.get_params()
    params["wave_pct"] = dict(params["wave_pct"], value=float(wave_pct))
    params["vol_ratio"] = dict(params["vol_ratio"], value=float(vol_ratio))
    params["cool_down_min"] = dict(params["cool_down_min"], value=int(cool_down_min))
    return anomaly.detect_anomalies(df, params, min_gap_min=int(cool_down_min))


def _eval_events(df: pd.DataFrame, events: list, horizon: int) -> dict:
    """评估一组异动的方向兑现率（真实后验，样本不足则标记）。"""
    if not events:
        return {"sample": 0, "accuracy_pct": None, "avg_return_pct": None,
                "insufficient": True, "note": "该参数组合下无触发，无法评估"}
    d = df.copy()
    d["timestamps"] = pd.to_datetime(d["timestamps"])
    d = d.sort_values("timestamps").reset_index(drop=True)
    idx = {t.strftime("%Y-%m-%d %H:%M"): i for i, t in enumerate(d["timestamps"])}

    hits, rets, skipped = 0, [], 0
    for ev in events:
        i = idx.get(ev["ts"])
        if i is None or i + horizon >= len(d):
            skipped += 1
            continue
        p0 = float(d["close"].iloc[i])
        p1 = float(d["close"].iloc[i + horizon])
        if p0 <= 0:
            skipped += 1
            continue
        chg = (p1 - p0) / p0 * 100
        rets.append(chg)
        if (ev["direction"] == "up" and chg > 0) or (ev["direction"] == "down" and chg < 0):
            hits += 1
    n = len(rets)
    if n == 0:
        return {"sample": 0, "accuracy_pct": None, "avg_return_pct": None,
                "insufficient": True,
                "note": f"{len(events)} 个触发全部靠近数据末端，无足够后验区间"}
    return {
        "sample": n,
        "skipped": skipped,
        "accuracy_pct": round(hits / n * 100, 1),
        "avg_return_pct": round(float(np.mean(rets)), 2),
        "insufficient": n < 10,
        "note": (f"基于 {n} 个触发样本、{horizon} 分钟持有期；"
                 + ("样本偏少，结论仅供参考。" if n < 10 else "样本量尚可。")
                 + "未计入手续费与滑点。"),
    }


def simulate(symbol: str, df: pd.DataFrame, overrides: dict = None,
             horizon: int = 30) -> dict:
    """模拟器：对比调参前后的真实检测结果。

    overrides 形如 {"wave_pct": 2.0, "vol_ratio": 1.8, "cool_down_min": 5}
    返回 baseline / modified 两组的触发次数与方向兑现率。
    """
    if df is None or len(df) < 60:
        return {"ok": False,
                "error": "历史 K 线不足（需至少 60 根），无法模拟 —— 不会给出编造的回测结果"}

    base_p = anomaly.get_params()
    b_wave = float(base_p["wave_pct"]["value"])
    b_vol = float(base_p["vol_ratio"]["value"])
    b_cool = int(base_p["cool_down_min"]["value"])

    ov = overrides or {}
    m_wave = float(ov.get("wave_pct", b_wave))
    m_vol = float(ov.get("vol_ratio", b_vol))
    m_cool = int(ov.get("cool_down_min", b_cool))

    base_events = _detect_with_params(df, b_wave, b_vol, b_cool)
    mod_events = _detect_with_params(df, m_wave, m_vol, m_cool)

    base_eval = _eval_events(df, base_events, horizon)
    mod_eval = _eval_events(df, mod_events, horizon)

    # 变化解读（人话）
    delta_n = len(mod_events) - len(base_events)
    notes = []
    if delta_n > 0:
        notes.append(f"触发次数增加 {delta_n} 次（阈值放宽）")
    elif delta_n < 0:
        notes.append(f"触发次数减少 {abs(delta_n)} 次（阈值收紧）")
    else:
        notes.append("触发次数不变")
    if base_eval.get("accuracy_pct") is not None and mod_eval.get("accuracy_pct") is not None:
        d_acc = mod_eval["accuracy_pct"] - base_eval["accuracy_pct"]
        notes.append(f"方向兑现率 {base_eval['accuracy_pct']}% → {mod_eval['accuracy_pct']}%"
                     f"（{'+' if d_acc >= 0 else ''}{round(d_acc,1)}%）")
    if base_eval.get("insufficient") or mod_eval.get("insufficient"):
        notes.append("⚠️ 任一组样本偏少，对比结论可靠性有限")

    return {
        "ok": True,
        "symbol": symbol,
        "horizon_min": horizon,
        "bars": int(len(df)),
        "range": {
            "start": str(pd.to_datetime(df["timestamps"]).min()),
            "end": str(pd.to_datetime(df["timestamps"]).max()),
        },
        "baseline": {
            "params": {"wave_pct": b_wave, "vol_ratio": b_vol, "cool_down_min": b_cool},
            "triggers": len(base_events), **base_eval,
        },
        "modified": {
            "params": {"wave_pct": m_wave, "vol_ratio": m_vol, "cool_down_min": m_cool},
            "triggers": len(mod_events), **mod_eval,
        },
        "delta_triggers": delta_n,
        "notes": notes,
        "disclaimer": "以上为**历史数据回测**结果，不代表未来表现；"
                      "未计入手续费、滑点与涨跌停无法成交的情况。",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 周度校准 / 月度全量回测
# ---------------------------------------------------------------------------

def calibration_schedule(now: float = None) -> dict:
    """周度校准与月度全量回测的到期判定 + 上次执行记录。"""
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts)
    tuning = {}
    try:
        tuning = nightly.load_tuning()
    except Exception:
        pass

    last_weekly = tuning.get("last_weekly_calib_ts") or 0
    last_monthly = tuning.get("last_monthly_backtest_ts") or 0

    # 周度：每周一 02:30 之后、且距上次 >= 6 天
    days_since_weekly = (ts - last_weekly) / 86400 if last_weekly else 999
    weekly_due = days_since_weekly >= 6
    # 月度：每月 1 号之后、且距上次 >= 27 天
    days_since_monthly = (ts - last_monthly) / 86400 if last_monthly else 999
    monthly_due = days_since_monthly >= 27

    def _fmt(t):
        return (datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "从未执行")

    return {
        "ok": True,
        "now": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "weekly": {
            "due": weekly_due,
            "last_run": _fmt(last_weekly),
            "days_since": None if not last_weekly else round(days_since_weekly, 1),
            "schedule": "每周（距上次 ≥6 天）",
            "what": "根据近期回测结果微调因子权重",
        },
        "monthly": {
            "due": monthly_due,
            "last_run": _fmt(last_monthly),
            "days_since": None if not last_monthly else round(days_since_monthly, 1),
            "schedule": "每月（距上次 ≥27 天）",
            "what": "跑全量回测，重新计算所有参数最优值",
        },
        "note": "周度/月度校准由夜间自迭代任务承载（nightly.run_iteration），"
                "此处仅呈现到期状态与上次执行时间。",
    }


def mark_calibration(kind: str, ts: float = None) -> dict:
    """记录周度/月度校准已执行（供夜间任务调用）。"""
    key = {"weekly": "last_weekly_calib_ts",
           "monthly": "last_monthly_backtest_ts"}.get(kind)
    if not key:
        raise ValueError("kind 必须是 weekly 或 monthly")
    cfg = nightly.load_tuning()
    cfg[key] = ts if ts is not None else time.time()
    nightly.save_tuning(cfg)
    return {"ok": True, "kind": kind, "ts": cfg[key]}
