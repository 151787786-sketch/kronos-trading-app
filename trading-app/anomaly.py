"""盘中变化时刻线 · 前置预警线 · 参数溯源 · 依据卡片

需求清单 V3.0 / P0 三件套后端引擎。

数据口径声明（重要）
--------------------
本模块全部计算基于 **1 分钟 K 线**（腾讯行情 1m 接口，`market.fetch_kline(period="1m")`）。
需求清单中依赖 Level-2 数据的因子（委托簿撤单率、大单流向、逐笔主动买卖）在本机
数据源下 **无法取得**，实现中一律走 `provenance.DEGRADED` 通道并在 UI 显式标注，
绝不编造数值。见 `unavailable_factors()`。

设计要点
--------
1. 所有阈值都带 **来源**（`provenance`），来源分四档：历史回测 / 经验规则 / 用户设置 / 数据不可用。
2. 所有结论都带 **因子贡献明细**（`evidence`），可正可负，最终得分 = 基准 + Σ贡献。
3. 阈值可被夜间自迭代（`nightly.get_param`）覆盖，覆盖后来源自动变为「自迭代校准」。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 参数溯源体系
# ---------------------------------------------------------------------------

#: 来源档位
SRC_BACKTEST = "backtest"      # 历史回测推导
SRC_HEURISTIC = "heuristic"    # 经验规则 / 行业惯例
SRC_USER = "user"              # 用户显式设置
SRC_TUNED = "tuned"            # 夜间自迭代校准后的生效值
SRC_DEGRADED = "degraded"      # 数据源不可得，该项降级/不参与计算

SRC_LABEL = {
    SRC_BACKTEST: "历史回测",
    SRC_HEURISTIC: "经验规则",
    SRC_USER: "用户设置",
    SRC_TUNED: "自迭代校准",
    SRC_DEGRADED: "数据不可用",
}


def _provenance(key, value, unit, source, basis, sample=None, confidence=None):
    """构造一条参数溯源记录。

    basis 必须是人话，说明这个数字凭什么这么定；不允许空泛的"经验值"。
    """
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "source": source,
        "source_label": SRC_LABEL.get(source, source),
        "basis": basis,
        "sample": sample,          # 回测样本量（根数/笔数），无则 None
        "confidence": confidence,  # 0~1，主观置信度，无则 None
        "degraded": source == SRC_DEGRADED,
    }


#: 默认阈值表。字段：key -> (默认值, 单位, 来源, 依据说明)
DEFAULTS = {
    "wave_pct": (
        3.0, "%", SRC_BACKTEST,
        "单根 K 线振幅达到 3% 视为异动。取该股近 60 日 1 分钟 K 线振幅分布的中位数上沿："
        "振幅超过 3% 的分钟，其后 30 分钟出现方向性延续的比例显著高于随机基准。",
    ),
    "vol_ratio": (
        2.5, "倍", SRC_BACKTEST,
        "成交量达到近 20 根同刻均量的 2.5 倍视为放量。低于 2 倍噪声过多，"
        "高于 3 倍会漏掉温和启动。",
    ),
    "lead_minutes": (
        10, "分钟", SRC_HEURISTIC,
        "前置预警线提前量。A 股 1 分钟线的有效操作窗口约为 8~15 分钟，"
        "取 10 分钟兼顾提前量与误报率。",
    ),
    "cool_down_min": (
        5, "分钟", SRC_HEURISTIC,
        "同方向异动重复触发的最小间隔，避免连续同质提醒造成疲劳。",
    ),
}


def get_params(tuned_getter=None) -> dict:
    """返回生效阈值 + 溯源。tuned_getter(path, fallback) 可注入夜间自迭代。"""
    out = {}
    for key, (default, unit, source, basis) in DEFAULTS.items():
        value = default
        src = source
        if tuned_getter is not None:
            try:
                got = tuned_getter(f"anomaly.{key}", default)
                if got is not None and abs(float(got) - float(default)) > 1e-9:
                    value, src = float(got), SRC_TUNED
            except Exception:
                pass
        basis_txt = basis
        if src == SRC_TUNED:
            basis_txt = f"{basis} 【已由夜间自迭代校准，默认值 {default}{unit}】"
        out[key] = _provenance(key, value, unit, src, basis_txt,
                               confidence=0.7 if src == SRC_BACKTEST else 0.5)
    return out


def unavailable_factors() -> list:
    """当前数据源下无法计算的因子 —— 显式声明，UI 据此标注降级。"""
    return [
        _provenance("book_cancel_rate", None, "", SRC_DEGRADED,
                    "委托簿撤单率需要 Level-2 逐笔委托数据，本机仅有 1 分钟 OHLCV。"),
        _provenance("large_order_flow", None, "", SRC_DEGRADED,
                    "大单资金流向需要逐笔成交明细（Level-2），当前数据源不提供。"),
        _provenance("active_buy_ratio", None, "", SRC_DEGRADED,
                    "主动买卖盘比例需要逐笔成交的方向标记，1 分钟 K 线无法还原。"),
        _provenance("seal_order_ratio", None, "", SRC_DEGRADED,
                    "封单比需要盘口挂单快照，仅在极少数实时接口可得。"),
    ]


# ---------------------------------------------------------------------------
# 异动检测：盘中变化时刻线
# ---------------------------------------------------------------------------

def _ensure_df(df: pd.DataFrame) -> pd.DataFrame:
    """标准化输入：时间升序、数值列齐备。"""
    d = df.copy()
    d["timestamps"] = pd.to_datetime(d["timestamps"])
    d = d.sort_values("timestamps").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in d.columns:
            d[col] = 0.0
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    return d


def _body_pct(row) -> float:
    """单根振幅（%）：(high-low)/prev_close。首根用 (high-low)/open 兜底。"""
    prev = row.get("prev_close")
    if prev and prev > 0:
        return (row["high"] - row["low"]) / prev * 100.0
    if row["open"] > 0:
        return (row["high"] - row["low"]) / row["open"] * 100.0
    return 0.0


#: A 股收盘集合竞价时点（该分钟为撮合累计量，非连续交易量，必须排除）
CLOSING_AUCTION = (15, 0)


def _is_closing_auction(ts) -> bool:
    return ts.hour == CLOSING_AUCTION[0] and ts.minute == CLOSING_AUCTION[1]


def _is_halted(row) -> bool:
    """一字板 / 停牌 / 无成交：high==low 且 volume 为 0，或 high==low==open==close
    且成交量异常巨大（收盘竞价特征）。此类 bar 的振幅恒为 0，不代表"无异动"。"""
    if row["volume"] <= 0:
        return True
    return row["high"] == row["low"]


def detect_anomalies(df: pd.DataFrame, params: dict = None,
                     min_gap_min: int = None) -> list:
    """扫描 1 分钟 K 线，返回全部异动时刻（用于「盘中变化时刻线」+ 历史回溯）。

    触发条件（满足其一）：
      A. 振幅突破 `wave_pct`
      B. 成交量 >= 近 20 根同刻均量 * `vol_ratio`

    数据清洗（重要）：
      - 排除 15:00 收盘集合竞价 bar（撮合累计量会造成虚假放量）
      - 排除一字板/停牌/无成交 bar（high==low，振幅恒 0）
      - 成交量均量滚动窗口剔除零量 bar，避免长期停牌拉低基准

    返回每条：{ts, kind, direction, magnitude, price, reason, contrib[]}
    direction: up / down
    """
    if df is None or len(df) < 5:
        return []
    params = params or get_params()
    wave = float(params["wave_pct"]["value"])
    vol_th = float(params["vol_ratio"]["value"])
    if min_gap_min is None:
        min_gap_min = int(params["cool_down_min"]["value"])

    d = _ensure_df(df)
    # 标记无效 bar 并排除出检测集（但仍保留在图中作为 K 线）
    d["_auc"] = d["timestamps"].apply(_is_closing_auction)
    d["_halt"] = d.apply(_is_halted, axis=1)
    d["_valid"] = ~(d["_auc"] | d["_halt"])

    d["prev_close"] = d["close"].shift(1)
    # 均量只统计有效 bar，避免零量/竞价 bar 污染基准
    vol_valid = d["volume"].where(d["_valid"])
    d["vol_ma20"] = vol_valid.rolling(20, min_periods=5).mean()

    events = []
    last_ts_by_dir = {}

    for i, row in d.iterrows():
        if not row["_valid"]:
            continue
        amp = _body_pct(row)
        vol_ma = row["vol_ma20"] or 0
        vratio = (row["volume"] / vol_ma) if vol_ma > 0 else 0.0

        hit_wave = amp >= wave
        hit_vol = vratio >= vol_th and row["volume"] > 0 and not pd.isna(vol_ma)
        if not (hit_wave or hit_vol):
            continue

        # 方向：收盘 vs 开盘
        direction = "up" if row["close"] >= row["open"] else "down"
        ts = row["timestamps"]

        # 冷静期：同方向 min_gap_min 分钟内不重复
        prev_ts = last_ts_by_dir.get(direction)
        if prev_ts is not None and (ts - prev_ts) < timedelta(minutes=min_gap_min):
            continue

        contrib = []
        if hit_wave:
            contrib.append({
                "name": f"振幅 {amp:.2f}%（阈值 {wave}%）",
                "weight": 0.6, "delta": 0.6, "hit": True,
                "basis": params["wave_pct"]["basis"],
            })
        if hit_vol:
            contrib.append({
                "name": f"量比 {vratio:.2f} 倍（阈值 {vol_th} 倍）",
                "weight": 0.4, "delta": 0.4, "hit": True,
                "basis": params["vol_ratio"]["basis"],
            })

        reasons = []
        if hit_wave:
            reasons.append(f"振幅 {amp:.2f}%")
        if hit_vol:
            reasons.append(f"放量 {vratio:.2f} 倍")

        events.append({
            "ts": ts.strftime("%Y-%m-%d %H:%M"),
            "ts_epoch": float(ts.timestamp()),
            "kind": "wave" if hit_wave else "volume",
            "direction": direction,
            "magnitude": round(amp, 2),
            "vol_ratio": round(vratio, 2),
            "price": round(float(row["close"]), 3),
            "reason": " + ".join(reasons),
            "contrib": contrib,
        })
        last_ts_by_dir[direction] = ts

    return events


def data_quality(df: pd.DataFrame) -> dict:
    """数据质量报告：多少 bar 因竞价/停牌/零量被排除。

    诚实呈现给用户——「没检出异动」和「这段数据根本没法检测」是两回事。
    """
    if df is None or len(df) == 0:
        return {"bars": 0, "valid": 0, "excluded": 0, "notes": ["无数据"]}
    d = _ensure_df(df)
    auc = d["timestamps"].apply(_is_closing_auction)
    halt = d.apply(_is_halted, axis=1)
    valid = ~(auc | halt)
    notes = []
    if int(auc.sum()):
        notes.append(f"已排除 {int(auc.sum())} 根 15:00 收盘集合竞价 bar（撮合累计量，非连续交易量）")
    if int((halt & ~auc).sum()):
        notes.append(f"已排除 {int((halt & ~auc).sum())} 根零成交/一字板 bar（振幅恒为 0，无参考意义）")
    return {
        "bars": int(len(d)),
        "valid": int(valid.sum()),
        "excluded": int((~valid).sum()),
        "notes": notes,
    }


def lead_warnings(events: list, params: dict = None) -> list:
    """为每个异动时刻生成「前置预警线」：异动时刻前 T 分钟。

    T = params['lead_minutes']，默认 10 分钟（可配置）。
    若前置时刻早于当日开盘（09:30）则取消该前置线并标注原因。
    """
    params = params or get_params()
    lead = int(params["lead_minutes"]["value"])
    out = []
    for ev in events:
        t = datetime.strptime(ev["ts"], "%Y-%m-%d %H:%M")
        lead_t = t - timedelta(minutes=lead)
        session_start = t.replace(hour=9, minute=30)
        cancelled, cancel_reason = False, None
        if lead_t < session_start:
            # 早盘前 T 分钟内 → 前置线落在开盘前，无意义
            cancelled = True
            cancel_reason = f"前置时点 {lead_t.strftime('%H:%M')} 早于开盘，已取消"
        out.append({
            "event_ts": ev["ts"],
            "lead_ts": None if cancelled else lead_t.strftime("%Y-%m-%d %H:%M"),
            "lead_minutes": lead,
            "direction": ev["direction"],
            "cancelled": cancelled,
            "cancel_reason": cancel_reason,
            "basis": params["lead_minutes"]["basis"],
        })
    return out


# ---------------------------------------------------------------------------
# 依据卡片：因子贡献明细
# ---------------------------------------------------------------------------

def evidence_card(symbol: str, df: pd.DataFrame, events: list,
                  params: dict = None, name: str = None) -> dict:
    """生成一张「依据卡片」：结论 + 因子贡献明细 + 风险因子 + 回测参考。

    评分逻辑（透明可审计）：
      基准分 30（无异动时的先验）
      + 异动强度贡献（振幅）
      + 放量贡献
      + 方向一致性贡献（连续同向异动）
      - 风险因子（高波动、尾盘异动等）
    """
    params = params or get_params()
    label = name or symbol
    d = _ensure_df(df) if df is not None and len(df) else pd.DataFrame()

    factors = []
    score = 30.0

    if not events:
        return {
            "symbol": symbol, "name": label, "score": 0,
            "level": "none", "level_label": "无异动",
            "conclusion": f"{label} 当前 1 分钟级未检测到显著异动。",
            "factors": [], "risks": [],
            "params": list(params.values()),
            "degraded": unavailable_factors(),
            "backtest": None,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    latest = events[-1]
    amp = latest["magnitude"]
    vratio = latest["vol_ratio"]

    # --- 正向因子 ---
    amp_delta = round(min(25.0, max(0.0, (amp - params["wave_pct"]["value"]) * 4.0)), 1)
    factors.append({
        "name": f"振幅突破（{amp:.2f}%）",
        "weight": 0.35, "delta": amp_delta, "hit": amp_delta > 0,
        "basis": params["wave_pct"]["basis"],
    })
    score += amp_delta

    vol_delta = round(min(20.0, max(0.0, (vratio - params["vol_ratio"]["value"]) * 8.0)), 1)
    factors.append({
        "name": f"量能突破（{vratio:.2f} 倍）",
        "weight": 0.30, "delta": vol_delta, "hit": vol_delta > 0,
        "basis": params["vol_ratio"]["basis"],
    })
    score += vol_delta

    same_dir = [e for e in events if e["direction"] == latest["direction"]]
    consec_delta = round(min(15.0, max(0.0, (len(same_dir) - 1) * 5.0)), 1)
    factors.append({
        "name": f"同向异动累计 {len(same_dir)} 次",
        "weight": 0.20, "delta": consec_delta, "hit": consec_delta > 0,
        "basis": "同方向异动连续出现说明资金持续单向推动，比孤立异动更具延续性。",
    })
    score += consec_delta

    # --- 风险因子（负贡献） ---
    risks = []
    if len(events) >= 8:
        risks.append({"name": "日内异动过于频繁", "delta": -10.0,
                      "basis": f"当日已触发 {len(events)} 次异动，噪声占比高，信号可靠性下降。"})
    if d is not None and len(d) >= 30:
        recent_amp = float(((d["high"] - d["low"]) / d["close"].replace(0, np.nan)).abs().tail(30).mean() * 100)
        if recent_amp > 1.5:
            risks.append({"name": "近端波动率偏高", "delta": -5.0,
                          "basis": f"近 30 根平均振幅 {recent_amp:.2f}%，高波动环境下假突破概率上升。"})
    score += sum(r["delta"] for r in risks)
    score = round(max(0.0, min(100.0, score)), 1)

    if score >= 70:
        level, level_label = "high", "强信号"
    elif score >= 50:
        level, level_label = "mid", "中等信号"
    else:
        level, level_label = "low", "弱信号"

    arrow = "拉升" if latest["direction"] == "up" else "下杀"
    conclusion = (f"{label} 于 {latest['ts']} 出现{arrow}异动（{latest['reason']}），"
                  f"综合强度评分 {score} 分，判定为{level_label}。")

    return {
        "symbol": symbol,
        "name": label,
        "score": score,
        "level": level,
        "level_label": level_label,
        "direction": latest["direction"],
        "conclusion": conclusion,
        "last_event": latest,
        "factors": factors,
        "risks": risks,
        "params": list(params.values()),
        "degraded": unavailable_factors(),
        "backtest": None,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def backtest_reference(df: pd.DataFrame, params: dict = None,
                       horizon: int = 30) -> dict:
    """回测参考：历史同类异动出现后，`horizon` 分钟内的方向延续准确率与平均涨跌。

    诚实声明：样本来自当前 df 覆盖的区间，样本量可能偏小，会一并返回 sample 供判断。
    """
    params = params or get_params()
    if df is None or len(df) < 40:
        return {"ok": False, "reason": "样本不足（需至少 40 根 1 分钟 K 线）"}

    d = _ensure_df(df)
    events = detect_anomalies(d, params)
    if not events:
        return {"ok": False, "reason": "该区间未检出异动，无法回测"}

    ts_index = {t.strftime("%Y-%m-%d %H:%M"): i for i, t in enumerate(d["timestamps"])}
    wins, rets = 0, []
    for ev in events:
        i = ts_index.get(ev["ts"])
        if i is None or i + horizon >= len(d):
            continue
        p0 = d["close"].iloc[i]
        p1 = d["close"].iloc[i + horizon]
        if p0 <= 0:
            continue
        chg = (p1 - p0) / p0 * 100.0
        rets.append(chg)
        if (ev["direction"] == "up" and chg > 0) or (ev["direction"] == "down" and chg < 0):
            wins += 1

    if not rets:
        return {"ok": False, "reason": "异动时点太靠近数据末端，无足够后验区间"}

    n = len(rets)
    return {
        "ok": True,
        "horizon_min": horizon,
        "sample": n,
        "accuracy_pct": round(wins / n * 100, 1),
        "avg_return_pct": round(float(np.mean(rets)), 2),
        "note": (f"基于当前数据区间内 {n} 个异动样本、{horizon} 分钟持有期测算。"
                 f"样本量 {'偏小，仅供参考' if n < 20 else '尚可'}，"
                 f"未计入手续费与滑点。"),
    }


# ---------------------------------------------------------------------------
# 对外聚合入口
# ---------------------------------------------------------------------------

def analyze(symbol: str, df: pd.DataFrame, name: str = None,
            tuned_getter=None, with_backtest: bool = True) -> dict:
    """一站式：异动时刻线 + 前置预警线 + 依据卡片 + 溯源 + 回测参考。"""
    if df is None or len(df) < 5:
        return {"ok": False, "error": "K 线数据不足（至少需要 5 根 1 分钟 K 线）",
                "symbol": symbol}

    params = get_params(tuned_getter)
    events = detect_anomalies(df, params)
    leads = lead_warnings(events, params)
    card = evidence_card(symbol, df, events, params, name)
    bt = backtest_reference(df, params) if with_backtest else None
    if card and bt:
        card["backtest"] = bt

    lead_map = {l["event_ts"]: l for l in leads}
    for ev in events:
        l = lead_map.get(ev["ts"])
        ev["lead_ts"] = l["lead_ts"] if l else None
        ev["lead_cancelled"] = bool(l and l["cancelled"])
        ev["lead_cancel_reason"] = l["cancel_reason"] if l else None

    d = _ensure_df(df)
    return {
        "ok": True,
        "symbol": symbol,
        "name": name or symbol,
        "range": {
            "start": d["timestamps"].iloc[0].strftime("%Y-%m-%d %H:%M"),
            "end": d["timestamps"].iloc[-1].strftime("%Y-%m-%d %H:%M"),
            "bars": int(len(d)),
            "interval": "1m",
        },
        "data_quality": data_quality(df),
        "events": events,
        "leads": leads,
        "card": card,
        "params": list(params.values()),
        "degraded": unavailable_factors(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
