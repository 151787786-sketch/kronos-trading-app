"""下探过程实时预警 + 下探质量评分 (Dip Quality Score) + 尾盘走势预判

需求清单 V3.0 / P1 模块。

数据口径声明（重要）
--------------------
全部计算基于 **1 分钟 K 线**（含成交额 amount，用于还原均价线 VWAP）。

需求中依赖 Level-2 / 逐笔数据的因子在本机数据源下 **无法取得**，一律走
`anomaly.SRC_DEGRADED` 通道显式降级，不编造数值：
  * 委托簿撤单率（下探评分因子之一）
  * 大单流向（下探评分因子之一）
  * 封单比 / 封单稳定性（尾盘"真抢筹"判定因子之一）
  * 逐笔主动性买单（尾盘因子之一 → 用「上涨分钟占比 + 量能放大」代理，并标注为代理指标）

可取得的因子：
  * 均价线 VWAP：由 amount/volume 累计还原（真实可得）
  * 板块共振：industry.industry_for() 的行业涨跌幅（真实可得，需联网）
  * 大盘环境：指数实时行情（真实可得，需联网）
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

from anomaly import (SRC_DEGRADED, SRC_BACKTEST, SRC_HEURISTIC, SRC_TUNED,
                     _ensure_df, _is_closing_auction, _is_halted, _provenance,
                     SRC_LABEL)

# ---------------------------------------------------------------------------
# 交易时段常量
# ---------------------------------------------------------------------------

MORNING_OPEN = dtime(9, 30)
MORNING_CLOSE = dtime(11, 30)
AFTERNOON_OPEN = dtime(13, 0)
NOON_SNAPSHOT = dtime(11, 30)     # 午间快照时点
TAIL_START = dtime(14, 0)         # 尾盘拉板信号起算
TAIL_WARN = dtime(14, 30)         # 14:30 预警线
TAIL_END = dtime(15, 0)

#: 下探阶段
DIP_STAGES = {
    "normal": ("正常回撤", "跌幅 < 1.5%，量能正常", "不预警"),
    "accelerating": ("加速下杀", "N 分钟内连跌且斜率变陡", "触发关注级"),
    "panic": ("恐慌释放", "放量下跌，买盘稀疏", "触发警告级"),
    "exhausted": ("抛压衰竭", "二次下杀量 < 首次，不再创新低", "可能止跌（蓝条提示）"),
}


def _stage_provenance():
    return [
        _provenance("dip_normal_pct", 1.5, "%", SRC_HEURISTIC,
                    "跌幅 <1.5% 视为正常回撤。该幅度内的日内波动在 A 股属于常态噪声，"
                    "统计上不足以区分洗盘与趋势性下跌。"),
        _provenance("dip_slope_k", 2.0, "倍", SRC_BACKTEST,
                    "加速下杀判定：近 5 分钟跌幅斜率 ≥ 前 15 分钟斜率的 2 倍。"
                    "斜率突变（加速）比跌幅绝对值更能识别资金夺路而逃。"),
        _provenance("dip_panic_vol", 2.0, "倍", SRC_BACKTEST,
                    "恐慌释放判定：下跌分钟量能 ≥ 近 20 根均量的 2 倍，"
                    "且下跌分钟占比 ≥ 65%（买盘稀疏）。"),
        _provenance("dip_exhaust_ratio", 0.7, "倍", SRC_HEURISTIC,
                    "抛压衰竭判定：二次下杀量能 < 首次下杀量能的 70%，且未创新低。"
                    "缩量止跌是抛压耗尽的典型特征。"),
    ]


def unavailable_factors() -> list:
    """下探/尾盘模块中当前数据源不可得的因子，显式声明。"""
    return [
        _provenance("book_cancel_rate", None, "", SRC_DEGRADED,
                    "委托簿撤单率需要 Level-2 逐笔委托数据，本机仅有 1 分钟 OHLCV，"
                    "该因子未参与下探质量评分。"),
        _provenance("large_order_flow", None, "", SRC_DEGRADED,
                    "大单流向需要逐笔成交明细（Level-2），当前数据源不提供，未参与评分。"),
        _provenance("seal_order_ratio", None, "", SRC_DEGRADED,
                    "封单比与封单稳定性需要盘口挂单快照，当前数据源不提供，"
                    "「真抢筹」判定中该维度已降级。"),
        _provenance("active_buy_orders", None, "", SRC_DEGRADED,
                    "逐笔主动性买单需要成交方向标记。本模块以「上涨分钟占比 + 量能放大」"
                    "作为**代理指标**参与尾盘评分，并已标注为代理而非真实主动买盘。"),
    ]


# ---------------------------------------------------------------------------
# 均价线 (VWAP)
# ---------------------------------------------------------------------------

def vwap(df: pd.DataFrame) -> pd.Series:
    """当日累计均价线（VWAP）。

    精度说明（重要）
    ----------------
    腾讯 1 分钟 K 线**不返回成交额**（已实测确认：分钟线第 7 位是空 dict），
    本项目里的 `amount` 列是 `volume × close` 的估算值。因此这里的累计均价
    本质上是「收盘价的成交量加权」，而不是「真实成交均价的成交量加权」。

    在正常行情下两者差异很小（收盘价贴近均价），VWAP 仍落在当日高低区间内，
    对「股价在均价线上方还是下方」这类判断足够可靠。但需注意：
      · 大幅单边行情中，收盘价系统性偏离均价，VWAP 会有偏移
      · 需要精确 VWAP 时，应切换到提供真实成交额的 Wind 后端

    后端若无 amount 列，退回 (high+low+close)/3 的典型价累计。
    """
    d = _ensure_df(df)
    if "amount" in d.columns and (d["amount"] > 0).any() and (d["volume"] > 0).any():
        amt = pd.to_numeric(d["amount"], errors="coerce").fillna(0.0)
        vol = pd.to_numeric(d["volume"], errors="coerce").fillna(0.0)
        cum_amt = amt.cumsum()
        cum_vol = vol.cumsum().replace(0, np.nan)
        out = cum_amt / cum_vol
        # 成交量极小处用典型价兜底，避免除零/抖动
        typ = (d["high"] + d["low"] + d["close"]) / 3.0
        out = out.where(out > 0, typ)
        return out.ffill().bfill()
    typ = (d["high"] + d["low"] + d["close"]) / 3.0
    return typ.cumsum() / pd.Series(np.arange(1, len(typ) + 1), index=typ.index)


# ---------------------------------------------------------------------------
# 下探过程四阶段 + 质量评分
# ---------------------------------------------------------------------------

def _is_down_bar(row, prev_close=None) -> bool:
    """判定一根 K 线是否属于「下跌」。

    优先用 close < open；若两者相等（数据源给出平开盘、或某些分钟数据
    open 就是上一根 close 的近似），退回用 close < prev_close 判断，
    避免整段单调下跌被误判成「无下跌 bar」。
    """
    if row["close"] < row["open"]:
        return True
    if row["close"] > row["open"]:
        return False
    # open == close：用与上一根的收盘比较兜底
    if prev_close is not None and prev_close > 0:
        return row["close"] < prev_close
    return False


def _segments(d: pd.DataFrame) -> list:
    """把当日 1 分钟 K 线切成连续下跌段（用于下探阶段的强弱对比）。"""
    segs, cur = [], None
    prev_close = None
    for _, row in d.iterrows():
        down = _is_down_bar(row, prev_close)
        if down:
            if cur is None:
                cur = {"start": row["timestamps"], "end": row["timestamps"],
                       "vol": 0.0, "low": row["low"], "bars": 0}
            cur["end"] = row["timestamps"]
            cur["vol"] += float(row["volume"])
            cur["low"] = min(cur["low"], float(row["low"]))
            cur["bars"] += 1
        else:
            if cur is not None:
                segs.append(cur)
                cur = None
        prev_close = float(row["close"])
    if cur is not None:
        segs.append(cur)
    return segs


def _today_only(d: pd.DataFrame) -> pd.DataFrame:
    """只取最新交易日的数据。

    下探阶段/均价线/尾盘信号都是**日内**概念，跨日混算会把昨天的走势
    当成今天的量价结构，必须按交易日切分。
    """
    if d is None or len(d) == 0:
        return d
    days = d["timestamps"].dt.date
    last_day = days.iloc[-1]
    return d[days == last_day].reset_index(drop=True)


def dip_stage(df: pd.DataFrame, params: dict = None) -> dict:
    """判定当前下探所处阶段 + 下探质量评分 (0-100)。

    评分逻辑（洗盘型高分 / 出货型低分，透明可审计）：
      基准 50
      + 缩量止跌（二次下杀缩量）        → 洗盘特征
      + 未有效跌破均价线/昨收           → 洗盘特征
      + 快速收复（下探后回到均价线上方） → 洗盘特征
      + 板块未同步崩塌                  → 洗盘特征
      - 放量下跌（恐慌释放）            → 出货特征
      - 跌破关键支撑且不收回            → 出货特征
      - 板块共振下杀                    → 出货特征
    """
    params = params or dip_params()
    if df is None or len(df) < 10:
        return {"ok": False, "error": "数据不足（需至少 10 根 1 分钟 K 线）"}

    d_all = _ensure_df(df)
    d_all["_auc"] = d_all["timestamps"].apply(_is_closing_auction)
    d_all["_halt"] = d_all.apply(_is_halted, axis=1)
    d_all["_valid"] = ~(d_all["_auc"] | d_all["_halt"])
    d = _today_only(d_all)
    dv = d[d["_valid"]].reset_index(drop=True)
    if len(dv) < 5:
        return {"ok": False, "error": "当日有效 K 线不足（停牌/竞价 bar 过多）"}

    vw = vwap(dv)
    dv = dv.assign(vwap=vw.values)
    last = dv.iloc[-1]
    day_open = float(dv["open"].iloc[0])
    close = float(last["close"])
    high = float(dv["high"].max())
    low = float(dv["low"].min())
    vwap_last = float(last["vwap"])
    prev_close = day_open  # 1 分钟数据无昨收，用当日开盘作参照并标注

    drop_from_high = (high - close) / high * 100 if high > 0 else 0.0
    depth_pct = drop_from_high
    below_vwap = close < vwap_last
    below_open = close < day_open

    # --- 量能结构 ---
    # 关键：量能基准必须取「当前观察窗口之前」的**中位数**，而不是紧邻的均值。
    # 原因：持续放量时，紧邻窗口的均值会被放量本身抬高，使比值永远趋近 1.0 ——
    # 恰恰在最该报警的持续放量时失效。用中位数对少数放量 bar 不敏感。
    recent_n = min(10, len(dv))
    recent = dv.tail(recent_n)
    pre = dv.iloc[:-recent_n] if len(dv) > recent_n else dv.iloc[:0]
    if len(pre) >= 5:
        vol_base = float(pre["volume"].median())
    else:
        vol_base = float(dv["volume"].median())

    # 下跌占比用 _is_down_bar，兼容 open==close 的数据源
    _pc = None
    _down_flags = []
    for _, _r in dv.iterrows():
        _down_flags.append(_is_down_bar(_r, _pc))
        _pc = float(_r["close"])
    down_ratio = float(np.mean(_down_flags[-recent_n:])) if _down_flags else 0.0

    recent_vol_avg = float(recent["volume"].mean() or 0)
    vol_expand = (recent_vol_avg / vol_base) if vol_base > 0 else 0.0

    # --- 斜率（加速检测）---
    def _slope(sub):
        if len(sub) < 2:
            return 0.0
        y = sub["close"].values.astype(float)
        x = np.arange(len(y), dtype=float)
        return float(np.polyfit(x, y, 1)[0])

    slope_recent = _slope(dv.tail(5))
    slope_prior = _slope(dv.tail(20).head(15)) if len(dv) >= 20 else 0.0
    slope_ratio = abs(slope_recent) / abs(slope_prior) if abs(slope_prior) > 1e-9 else 0.0

    # --- 下探段对比（抛压衰竭）---
    segs = _segments(dv)
    exhaust = False
    seg_note = ""
    if len(segs) >= 2:
        s1, s2 = segs[-2], segs[-1]
        if s2["low"] >= s1["low"] and s1["vol"] > 0:
            if s2["vol"] < s1["vol"] * params["dip_exhaust_ratio"]["value"]:
                exhaust = True
                seg_note = (f"二次下杀量能 {s2['vol']:.0f} < 首次 {s1['vol']:.0f} 的 "
                            f"{params['dip_exhaust_ratio']['value']*100:.0f}%，且未创新低")

    # --- 阶段判定（优先级从高到低）---
    stage, stage_why = "normal", ""
    if exhaust:
        stage = "exhausted"
        stage_why = seg_note
    elif (vol_expand >= params["dip_panic_vol"]["value"] and down_ratio >= 0.65
          and below_vwap):
        stage = "panic"
        stage_why = (f"下跌分钟占比 {down_ratio*100:.0f}%、量能放大 {vol_expand:.2f} 倍，"
                     f"且股价位于均价线下方")
    elif slope_ratio >= params["dip_slope_k"]["value"] and below_open:
        stage = "accelerating"
        stage_why = f"近 5 分钟下跌斜率是前段的 {slope_ratio:.2f} 倍（斜率变陡）"
    elif depth_pct < params["dip_normal_pct"]["value"]:
        stage = "normal"
        stage_why = f"自高点回撤 {depth_pct:.2f}%，未达 {params['dip_normal_pct']['value']}% 警戒线"
    else:
        stage = "normal"
        stage_why = f"回撤 {depth_pct:.2f}% 但未见加速或放量特征"

    label, chars, action = DIP_STAGES[stage]

    # --- Dip Quality Score ---
    factors, risks = [], []
    score = 50.0

    # 洗盘正贡献
    if exhaust:
        factors.append({"name": "二次下杀缩量且未创新低", "weight": 0.30, "delta": 18.0,
                        "basis": params["dip_exhaust_ratio"]["basis"]})
        score += 18.0
    if not below_vwap:
        factors.append({"name": "股价站于均价线上方", "weight": 0.25, "delta": 12.0,
                        "basis": "均价线是多空成本分界，站上说明承接资金主动。"})
        score += 12.0
    if not below_open:
        factors.append({"name": "未跌破当日开盘价", "weight": 0.15, "delta": 8.0,
                        "basis": "开盘价是日内多空分水岭，守住说明抛压有限。"})
        score += 8.0
    if down_ratio < 0.5 and drop_from_high > 0.5:
        factors.append({"name": "近期下跌分钟占比低", "weight": 0.15, "delta": 6.0,
                        "basis": f"近 {recent_n} 分钟仅 {down_ratio*100:.0f}% 为阴线，"
                                 f"跌势不连续，更像震荡洗盘。"})
        score += 6.0

    # 出货负贡献
    if stage == "panic":
        risks.append({"name": "放量恐慌释放", "delta": -20.0,
                      "basis": f"量能放大 {vol_expand:.2f} 倍且买盘稀疏，"
                               f"属出货型特征而非洗盘。"})
        score -= 20.0
    if below_vwap and below_open:
        risks.append({"name": "同时跌破均价线与开盘价", "delta": -12.0,
                      "basis": "双支撑失守，短线多头结构破坏。"})
        score -= 12.0
    if depth_pct >= 5.0:
        risks.append({"name": "回撤深度过大", "delta": -8.0,
                      "basis": f"自高点已回撤 {depth_pct:.2f}%，深水区风险显著。"})
        score -= 8.0

    score = round(max(0.0, min(100.0, score)), 1)

    # 类型判定：高分=洗盘（承接强/抛压竭），低分=出货（放量下跌/破位）
    if score >= 65:
        dtype, dtype_label = "wash", "洗盘型下探"
    elif score <= 40:
        dtype, dtype_label = "distribution", "出货型下探"
    else:
        dtype, dtype_label = "mixed", "中性下探"

    return {
        "ok": True,
        "stage": stage,
        "stage_label": label,
        "stage_chars": chars,
        "stage_action": action,
        "stage_why": stage_why,
        "score": score,
        "dip_type": dtype,
        "dip_type_label": dtype_label,
        "summary": f"{dtype_label} {score} 分",
        "metrics": {
            "close": round(close, 3),
            "day_open": round(day_open, 3),
            "vwap": round(vwap_last, 3),
            "high": round(high, 3),
            "low": round(low, 3),
            "depth_pct": round(depth_pct, 2),
            "below_vwap": bool(below_vwap),
            "below_open": bool(below_open),
            "down_ratio": round(down_ratio, 2),
            "vol_expand": round(vol_expand, 2),
            "slope_ratio": round(slope_ratio, 2),
            "down_segments": len(segs),
        },
        "factors": factors,
        "risks": risks,
        "params": list(params.values()),
        "degraded": unavailable_factors(),
        "bars_used": int(len(dv)),
        "excluded_bars": int((~d["_valid"]).sum()),
        "session_date": str(d["timestamps"].iloc[0].date()) if len(d) else None,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def dip_params(tuned_getter=None) -> dict:
    """下探模块的阈值 + 溯源。"""
    defaults = {
        "dip_normal_pct": (1.5, "%", SRC_HEURISTIC,
                           "跌幅 <1.5% 视为正常回撤。该幅度内的日内波动在 A 股属于常态噪声，"
                           "统计上不足以区分洗盘与趋势性下跌。"),
        "dip_slope_k": (2.0, "倍", SRC_BACKTEST,
                        "加速下杀判定：近 5 分钟跌幅斜率 ≥ 前 15 分钟斜率的 2 倍。"
                        "斜率突变（加速）比跌幅绝对值更能识别资金夺路而逃。"),
        "dip_panic_vol": (2.0, "倍", SRC_BACKTEST,
                          "恐慌释放判定：下跌分钟量能 ≥ 近 20 根均量的 2 倍，"
                          "且下跌分钟占比 ≥ 65%（买盘稀疏）。"),
        "dip_exhaust_ratio": (0.7, "倍", SRC_HEURISTIC,
                              "抛压衰竭判定：二次下杀量能 < 首次下杀量能的 70%，且未创新低。"
                              "缩量止跌是抛压耗尽的典型特征。"),
    }
    out = {}
    for key, (default, unit, src, basis) in defaults.items():
        value, source, basis_txt = default, src, basis
        if tuned_getter is not None:
            try:
                got = tuned_getter(f"dip.{key}", default)
                if got is not None and abs(float(got) - float(default)) > 1e-9:
                    value, source = float(got), SRC_TUNED
                    basis_txt = f"{basis} 【已由自迭代校准，默认值 {default}{unit}】"
            except Exception:
                pass
        out[key] = _provenance(key, value, unit, source, basis_txt,
                               confidence=0.65 if source == SRC_BACKTEST else 0.5)
    return out
