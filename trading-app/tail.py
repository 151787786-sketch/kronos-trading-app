"""尾盘走势预判：拉板信号六因子加权 + 三种尾盘结论 + 14:30 预警线

需求清单 V3.0 / P1 模块。

权重表（需求原样，可被夜间自迭代覆盖）：
    午后不再创新低          0.25
    股价站回均价线上方      0.30
    14:00-14:30 连续主动性买单  0.20
    板块内有跟风票          0.10
    该股近20日深水拉板历史  0.08
    大盘环境偏暖            0.07
    ------------------------------
    合计                    1.00

数据口径声明（重要）
--------------------
* 「连续主动性买单」需要逐笔成交方向标记（Level-2）。本机不可得，
  以 **代理指标** 计算：14:00-14:30 区间「上涨分钟占比」+「量能相对放大」，
  并在输出中明确标注 `proxy=True`，绝不冒称是真实主动买盘。
* 「板块内有跟风票」依赖行业板块实时数据（industry.py，需联网）；取不到时
  该项降级为中性值并标注，而非编造。
* 「大盘环境」依赖指数实时行情（需联网）；同样支持降级。
* 「近20日深水拉板历史」依赖日线数据（market.fetch_daily，真实可得）。
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

from anomaly import (SRC_BACKTEST, SRC_HEURISTIC, SRC_TUNED, SRC_DEGRADED,
                     _ensure_df, _is_closing_auction, _is_halted, _provenance)
from dip import _today_only, vwap

# ---------------------------------------------------------------------------
# 六因子权重表（需求原样）
# ---------------------------------------------------------------------------

FACTOR_WEIGHTS = {
    "no_new_low":      (0.25, "午后不再创新低"),
    "above_vwap":      (0.30, "股价站回均价线上方"),
    "active_buying":   (0.20, "14:00-14:30 连续主动性买单"),
    "sector_follow":   (0.10, "板块内有跟风票"),
    "history_deep":    (0.08, "该股近20日深水拉板历史"),
    "market_warm":     (0.07, "大盘环境偏暖"),
}

FACTOR_BASIS = {
    "no_new_low": "午后不再创新低说明抛压已在前半段释放完毕，是尾盘拉升的必要前提。",
    "above_vwap": "均价线是当日多空成本线；站回其上代表主动买盘接管，权重最高。",
    "active_buying": "尾盘半小时的连续主动性买单是资金抢筹的直接证据（本机以代理指标计算）。",
    "sector_follow": "板块内有跟风票说明是板块性资金行为，非个股孤立脉冲，持续性更强。",
    "history_deep": "该股有深水拉板的近期历史，说明有资金习惯在此位置做盘。",
    "market_warm": "大盘环境偏暖时，尾盘拉升不易被系统性抛压打断。",
}


def tail_params(tuned_getter=None) -> list:
    """六因子权重的生效值 + 溯源。"""
    out = []
    for key, (w, label) in FACTOR_WEIGHTS.items():
        value, source = w, SRC_HEURISTIC
        basis = (f"权重 {w}（需求清单 V3.0 指定）。{FACTOR_BASIS[key]}")
        if tuned_getter is not None:
            try:
                got = tuned_getter(f"tail.w_{key}", w)
                if got is not None and abs(float(got) - w) > 1e-9:
                    value, source = float(got), SRC_TUNED
                    basis = f"{basis} 【已由自迭代校准，默认值 {w}】"
            except Exception:
                pass
        out.append(_provenance(f"tail.w_{key}", value, "", source, basis,
                               confidence=0.6))
    return out


def threshold_provenance():
    """尾盘判定的阈值溯源。"""
    return [
        _provenance("tail_deep_pct", 5.0, "%", SRC_HEURISTIC,
                    "「深水」定义为当日自高点回撤 ≥5%。该深度以下的拉板才算真正意义上的"
                    "深水反包，浅水拉升参考价值低。"),
        _provenance("tail_board_pct", 9.0, "%", SRC_BACKTEST,
                    "「拉板」以涨幅 ≥9%（接近涨停）为标志。主板 10% 涨停、创业板 20%，"
                    "取 9% 作为兼顾两个板块的近似阈值。"),
        _provenance("tail_proxy_buy_ratio", 0.6, "", SRC_HEURISTIC,
                    "代理主动买盘判定：14:00-14:30 上涨分钟占比 ≥60% 视为买盘占优。"
                    "该阈值为经验值，真实主动买盘需 Level-2 逐笔数据。"),
        _provenance("tail_warn_prob", 0.70, "", SRC_HEURISTIC,
                    "概率 ≥70% 触发「尾盘转强」绿色预警（需求清单指定阈值）。"),
    ]


def unavailable_factors() -> list:
    return [
        _provenance("active_buy_orders", None, "", SRC_DEGRADED,
                    "逐笔主动性买单需要成交方向标记（Level-2）。本模块使用"
                    "「上涨分钟占比 + 量能放大」作为代理指标，输出中已标注 proxy=true。"),
        _provenance("seal_order_ratio", None, "", SRC_DEGRADED,
                    "封单比/封单稳定性需要盘口挂单快照，当前数据源不提供。"
                    "「真抢筹」判定中该维度已降级，改以量能与均价线关系近似。"),
        _provenance("sector_follow_detail", None, "", SRC_DEGRADED,
                    "同板块跟风个股的逐笔明细不可得。本模块以行业板块整体涨幅作为"
                    "板块共振的代理，取不到时该因子按中性处理并标注。"),
    ]


# ---------------------------------------------------------------------------
# 因子计算
# ---------------------------------------------------------------------------

def _factor_no_new_low(d: pd.DataFrame) -> dict:
    """午后（13:00 后）是否不再创新低。"""
    aft = d[d["timestamps"].dt.time >= dtime(13, 0)]
    morn = d[d["timestamps"].dt.time < dtime(13, 0)]
    if len(aft) == 0 or len(morn) == 0:
        return {"hit": None, "detail": "数据不足，无法比较上下午低点", "proxy": False}
    morn_low = float(morn["low"].min())
    aft_low = float(aft["low"].min())
    hit = aft_low >= morn_low
    return {
        "hit": bool(hit),
        "detail": (f"上午最低 {morn_low:.2f}，午后最低 {aft_low:.2f}，"
                   + ("午后未创新低" if hit else "午后已创新低")),
        "proxy": False,
    }


def _factor_above_vwap(d: pd.DataFrame) -> dict:
    """股价是否站回均价线上方（收盘价 vs 当日 VWAP）。"""
    vw = vwap(d)
    last_close = float(d["close"].iloc[-1])
    last_vwap = float(vw.iloc[-1])
    hit = last_close >= last_vwap
    gap = (last_close - last_vwap) / last_vwap * 100 if last_vwap > 0 else 0.0
    return {
        "hit": bool(hit),
        "detail": (f"现价 {last_close:.2f} vs 均价线 {last_vwap:.2f}"
                   f"（{'高于' if hit else '低于'} {abs(gap):.2f}%）"),
        "proxy": False,
    }


def _factor_active_buying(d: pd.DataFrame, th: float = 0.6) -> dict:
    """14:00-14:30 连续主动性买单 —— 代理指标。

    真实主动买盘需 Level-2 逐笔方向标记。此处以该区间「上涨分钟占比」
    与「量能相对放大」合成代理分，并显式标注 proxy=True。

    量能基准取 14:00 **之前**的均量，避免窗口内的放量把基准一起抬高。
    """
    win = d[(d["timestamps"].dt.time >= dtime(14, 0)) & (d["timestamps"].dt.time < dtime(14, 30))]
    if len(win) < 5:
        return {"hit": None, "detail": "14:00-14:30 数据不足", "proxy": True}
    up_ratio = float((win["close"] >= win["open"]).mean())
    pre = d[d["timestamps"].dt.time < dtime(14, 0)]
    base_pool = pre if len(pre) >= 5 else d
    vol_base = float(base_pool["volume"].median())   # 中位数，抗放量自污染
    vol_ratio = float(win["volume"].mean() / vol_base) if vol_base > 0 else 0.0
    # 代理分：涨跌占比为主(0.7)、量能放大为辅(0.3)
    proxy_score = up_ratio * 0.7 + min(1.0, vol_ratio / 2.0) * 0.3
    hit = up_ratio >= th
    return {
        "hit": bool(hit),
        "detail": (f"该区间上涨分钟占比 {up_ratio*100:.0f}%、量能 {vol_ratio:.2f} 倍"
                   f"（代理分 {proxy_score:.2f}，阈值 上涨占比≥{th*100:.0f}%；"
                   f"量能基准取 14:00 前均量 {vol_base:.0f}）"),
        "proxy": True,
        "up_ratio": round(up_ratio, 2),
        "vol_ratio": round(vol_ratio, 2),
        "proxy_score": round(proxy_score, 2),
    }


def _factor_sector_follow(sector: dict | None) -> dict:
    """板块内有跟风票 —— 以行业板块整体涨幅为代理。"""
    if not sector:
        return {"hit": None, "detail": "板块数据不可用（未联网或未取到行业分类）",
                "proxy": True, "degraded": True}
    board = sector.get("board") or {}
    pct = board.get("pct")
    if pct is None:
        return {"hit": None, "detail": "未匹配到对应行业板块", "proxy": True, "degraded": True}
    hit = float(pct) > 0
    return {
        "hit": bool(hit),
        "detail": (f"{sector.get('industry') or '所属板块'} 板块涨幅 {float(pct):+.2f}%"
                   f"（行业排名 {sector.get('rank')}/{sector.get('total')}）"),
        "proxy": True,
        "sector_pct": round(float(pct), 2),
    }


def _factor_history_deep(daily: pd.DataFrame | None, deep_pct: float = 5.0,
                         board_pct: float = 9.0, lookback: int = 20) -> dict:
    """该股近 20 日是否有「深水拉板」历史。

    定义：当日振幅内先深跌（自日内高点回撤 ≥ deep_pct）后收盘涨幅 ≥ board_pct。
    用日线 OHLC 近似判定（真实可得）。
    """
    if daily is None or len(daily) < 5:
        return {"hit": None, "detail": "日线数据不足，无法统计历史", "proxy": False}
    d = _ensure_df(daily).tail(lookback)
    hits = []
    for _, r in d.iterrows():
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        if h <= 0 or o <= 0:
            continue
        close_pct = (c - o) / o * 100
        intraday_drop = (h - l) / h * 100
        if close_pct >= board_pct * 0.7 and intraday_drop >= deep_pct * 0.6:
            hits.append(str(pd.to_datetime(r["timestamps"]).date()))
    hit = len(hits) > 0
    return {
        "hit": bool(hit),
        "detail": (f"近 {len(d)} 个交易日中 {len(hits)} 次深水反包"
                   + (f"（{', '.join(hits[-3:])}）" if hits else "")),
        "proxy": False,
        "count": len(hits),
    }


def _factor_market_warm(index_quote: dict | None) -> dict:
    """大盘环境偏暖 —— 以指数涨跌幅判定。"""
    if not index_quote:
        return {"hit": None, "detail": "大盘指数数据不可用（未联网）",
                "proxy": True, "degraded": True}
    pct = index_quote.get("pct")
    if pct is None:
        return {"hit": None, "detail": "未取到指数涨跌幅", "proxy": True, "degraded": True}
    hit = float(pct) > 0
    return {
        "hit": bool(hit),
        "detail": f"{index_quote.get('name') or '大盘'} {float(pct):+.2f}%",
        "proxy": True,
        "index_pct": round(float(pct), 2),
    }


# ---------------------------------------------------------------------------
# 综合评分 + 三种结论
# ---------------------------------------------------------------------------

def _score(factors: dict, weights: dict) -> tuple:
    """加权求和 → 概率（0~1）。

    未取到的因子（hit=None）不计入分母，权重按剩余因子归一化，
    并在结果中标注 coverage（有效权重占比）——不假装拿到全部数据。
    """
    total_w, got_w, acc = 0.0, 0.0, 0.0
    for key, (w, _label) in FACTOR_WEIGHTS.items():
        got = factors.get(key) or {}
        hit = got.get("hit")
        wv = float(weights.get(key, w))
        total_w += wv
        if hit is None:
            continue
        got_w += wv
        if hit:
            acc += wv
    if total_w <= 0:
        return 0.0, 0.0
    prob = acc / got_w if got_w > 0 else 0.0
    coverage = got_w / total_w
    return prob, coverage


def tail_conclusion(prob: float, metrics: dict, factors: dict,
                    coverage: float) -> dict:
    """三种尾盘结论：真抢筹 / 假拉升 / 磨尾盘。

    判定要点（需求清单）：
      真抢筹：放量回均价线、板块共振、封单稳
      假拉升：无量直拉、偏离均价线大、日线高位
      磨尾盘：横住不杀也不拉
    """
    above_vwap = (factors.get("above_vwap") or {}).get("hit")
    vol_expand = metrics.get("tail_vol_expand") or 0.0
    vwap_gap = metrics.get("vwap_gap_pct") or 0.0
    range_pct = metrics.get("tail_range_pct") or 0.0
    sector_hit = (factors.get("sector_follow") or {}).get("hit")
    day_gain = metrics.get("day_gain_pct") or 0.0

    # 磨尾盘：波动极小、横住
    if range_pct < 0.6:
        return {
            "type": "flat", "label": "磨尾盘",
            "hint": "无趋势，观望",
            "why": f"尾盘区间振幅仅 {range_pct:.2f}%，既未下杀也未拉升，属横盘磨筹。",
        }

    # 假拉升：无量 / 偏离均价线过大 / 已在高位
    fake_reasons = []
    if vol_expand < 1.0:
        fake_reasons.append(f"量能未放大（{vol_expand:.2f} 倍）")
    if vwap_gap > 3.0:
        fake_reasons.append(f"偏离均价线过大（+{vwap_gap:.2f}%）")
    if day_gain > 7.0:
        fake_reasons.append(f"日线已处高位（当日 +{day_gain:.2f}%）")
    if above_vwap is False:
        fake_reasons.append("未站回均价线")

    # 真抢筹：站回均价线 + 放量 + （板块共振）
    real_reasons = []
    if above_vwap:
        real_reasons.append("站回均价线上方")
    if vol_expand >= 1.5:
        real_reasons.append(f"量能放大 {vol_expand:.2f} 倍")
    if sector_hit:
        real_reasons.append("板块同步走强")

    if len(real_reasons) >= 2 and not fake_reasons:
        return {
            "type": "real", "label": "真抢筹",
            "hint": "尾盘攻击概率高，可跟踪打板",
            "why": "；".join(real_reasons) + "。",
        }
    if fake_reasons:
        return {
            "type": "fake", "label": "假拉升",
            "hint": "诱多概率高，次日易低开",
            "why": "；".join(fake_reasons) + "。",
        }
    return {
        "type": "flat", "label": "磨尾盘",
        "hint": "无趋势，观望",
        "why": "多空信号互相抵消，未见明确方向。",
    }


def tail_analysis(df: pd.DataFrame, sector: dict = None,
                  daily: pd.DataFrame = None, index_quote: dict = None,
                  tuned_getter=None) -> dict:
    """尾盘拉板信号完整分析（14:00 后持续计算）。"""
    if df is None or len(df) < 20:
        return {"ok": False, "error": "数据不足（需至少 20 根 1 分钟 K 线）"}

    d_all = _ensure_df(df)
    d_all["_valid"] = ~(d_all["timestamps"].apply(_is_closing_auction)
                        | d_all.apply(_is_halted, axis=1))
    d = _today_only(d_all)
    dv = d[d["_valid"]].reset_index(drop=True)
    if len(dv) < 10:
        return {"ok": False, "error": "当日有效 K 线不足"}

    weights = {k: v[0] for k, v in FACTOR_WEIGHTS.items()}
    if tuned_getter is not None:
        for k in weights:
            try:
                got = tuned_getter(f"tail.w_{k}", weights[k])
                if got is not None:
                    weights[k] = float(got)
            except Exception:
                pass

    # --- 六因子 ---
    factors = {
        "no_new_low": _factor_no_new_low(dv),
        "above_vwap": _factor_above_vwap(dv),
        "active_buying": _factor_active_buying(dv),
        "sector_follow": _factor_sector_follow(sector),
        "history_deep": _factor_history_deep(daily),
        "market_warm": _factor_market_warm(index_quote),
    }
    prob, coverage = _score(factors, weights)

    # --- 尾盘区间指标 ---
    tail = dv[dv["timestamps"].dt.time >= dtime(14, 0)]
    tail_src = tail if len(tail) >= 3 else dv.tail(10)
    vw = vwap(dv)
    last_close = float(dv["close"].iloc[-1])
    last_vwap = float(vw.iloc[-1])
    vwap_gap = (last_close - last_vwap) / last_vwap * 100 if last_vwap > 0 else 0.0
    tail_vol_base_pool = dv[dv["timestamps"].dt.time < dtime(14, 0)]
    tail_vol_base = float((tail_vol_base_pool if len(tail_vol_base_pool) >= 5
                           else dv)["volume"].median())
    tail_vol_expand = (float(tail_src["volume"].mean()) / tail_vol_base
                       if tail_vol_base > 0 else 0.0)
    t_hi, t_lo = float(tail_src["high"].max()), float(tail_src["low"].min())
    tail_range_pct = (t_hi - t_lo) / t_lo * 100 if t_lo > 0 else 0.0
    day_open = float(dv["open"].iloc[0])
    day_gain = (last_close - day_open) / day_open * 100 if day_open > 0 else 0.0
    day_high = float(dv["high"].max())
    depth = (day_high - last_close) / day_high * 100 if day_high > 0 else 0.0

    metrics = {
        "close": round(last_close, 3),
        "vwap": round(last_vwap, 3),
        "vwap_gap_pct": round(vwap_gap, 2),
        "tail_vol_expand": round(tail_vol_expand, 2),
        "tail_range_pct": round(tail_range_pct, 2),
        "day_gain_pct": round(day_gain, 2),
        "depth_from_high_pct": round(depth, 2),
        "tail_bars": int(len(tail_src)),
    }

    concl = tail_conclusion(prob, metrics, factors, coverage)
    prob_pct = round(prob * 100, 1)
    warn_th = 0.70

    # --- 预警线（14:30）---
    now_t = datetime.now().time()
    warn = None
    if now_t >= dtime(14, 30):
        above = (factors["above_vwap"] or {}).get("hit")
        sector_hit = (factors["sector_follow"] or {}).get("hit")
        if above and (sector_hit or prob >= warn_th):
            warn = {"type": "strong", "color": "green", "label": "尾盘转强",
                    "detail": (f"站回均价线（{last_close:.2f} > {last_vwap:.2f}）"
                               + ("，板块同步走强" if sector_hit else "")
                               + f"，拉板概率 {prob_pct}%")}
        elif (not above) and tail_vol_expand < 1.0:
            warn = {"type": "weak", "color": "red", "label": "尾盘杀跌延续",
                    "detail": (f"仍在均价线下方（{last_close:.2f} < {last_vwap:.2f}）"
                               f"且量能萎缩（{tail_vol_expand:.2f} 倍），承接不足")}
        else:
            warn = {"type": "watch", "color": "amber", "label": "观察",
                    "detail": f"信号不一致，拉板概率 {prob_pct}%，建议继续观察"}
    else:
        warn = {"type": "pending", "color": "muted", "label": "未到 14:30",
                "detail": "14:30 预警线尚未触发（当前 " + now_t.strftime("%H:%M") + "）"}

    # --- 依据卡片明细 ---
    breakdown = []
    for key, (w, label) in FACTOR_WEIGHTS.items():
        f = factors.get(key) or {}
        hit = f.get("hit")
        wv = weights.get(key, w)
        delta = round(wv * 100, 1) if hit else 0.0
        breakdown.append({
            "key": key,
            "name": label,
            "weight": wv,
            "hit": hit,
            "delta": delta if hit else 0.0,
            "contribution_pct": delta if hit else 0.0,
            "detail": f.get("detail") or "",
            "basis": FACTOR_BASIS.get(key, ""),
            "proxy": bool(f.get("proxy")),
            "degraded": bool(f.get("degraded")),
        })

    return {
        "ok": True,
        "prob_pct": prob_pct,
        "prob": round(prob, 3),
        "coverage": round(coverage, 2),
        "coverage_note": ("全部因子数据可得" if coverage >= 0.999
                          else f"仅 {coverage*100:.0f}% 权重的因子数据可得，"
                               f"其余按中性处理未计入分母"),
        "threshold_pct": warn_th * 100,
        "triggered": prob >= warn_th,
        "conclusion": concl,
        "warning": warn,
        "metrics": metrics,
        "breakdown": breakdown,
        "params": tail_params(tuned_getter) + threshold_provenance(),
        "degraded": unavailable_factors(),
        "session_date": str(dv["timestamps"].iloc[0].date()) if len(dv) else None,
        "is_tail_window": bool(len(tail) >= 3),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 午间快照（11:30）+ 午间三剧本
# ---------------------------------------------------------------------------

NOON_SCENARIOS = [
    {"key": "continue_down", "label": "午后继续杀",
     "cond": "上午尾段仍在下行、跌破均价线且放量",
     "action": "不接飞刀，等二次下杀缩量再看"},
    {"key": "range_grind", "label": "水下震荡磨筹码",
     "cond": "上午缩量、跌幅温和、围绕均价线反复",
     "action": "观察 14:00 后能否站回均价线"},
    {"key": "tail_surge", "label": "尾盘拉板",
     "cond": "上午深水但缩量止跌、午后不再创新低",
     "action": "14:30 站回均价线+板块强 → 跟踪打板"},
]


def noon_snapshot(df: pd.DataFrame, sector: dict = None) -> dict:
    """午间快照：上午关键数据 + 三种剧本假设。"""
    if df is None or len(df) < 10:
        return {"ok": False, "error": "数据不足"}

    d_all = _ensure_df(df)
    d_all["_valid"] = ~(d_all["timestamps"].apply(_is_closing_auction)
                        | d_all.apply(_is_halted, axis=1))
    d = _today_only(d_all)
    dv = d[d["_valid"]].reset_index(drop=True)
    am = dv[dv["timestamps"].dt.time <= dtime(11, 30)]
    if len(am) < 5:
        return {"ok": False, "error": "上午数据不足（需至少 5 根有效 K 线）"}

    vw = vwap(am)
    am_open = float(am["open"].iloc[0])
    am_last = float(am["close"].iloc[-1])
    am_low = float(am["low"].min())
    am_high = float(am["high"].max())
    am_vwap = float(vw.iloc[-1])
    max_drop = (am_high - am_low) / am_high * 100 if am_high > 0 else 0.0
    am_gain = (am_last - am_open) / am_open * 100 if am_open > 0 else 0.0

    # 上午量能占全日预估比例（历史经验：上午约占 45%~55%）
    am_vol = float(am["volume"].sum())
    full_days = d_all.groupby(d_all["timestamps"].dt.date)["volume"].sum()
    typical_am_share = 0.50
    am_vol_share_pct = None
    if len(full_days) >= 2:
        am_vol_share_pct = round(am_vol / max(1e-9, float(full_days.mean())) * 100, 1)

    # 剧本概率（依据上午特征）
    below_vwap = am_last < am_vwap
    last10 = am.tail(10)
    down_ratio_last = float((last10["close"] < last10["open"]).mean())
    vol_ma = float(am["volume"].rolling(20, min_periods=5).mean().iloc[-1] or 0)
    last_vol_ratio = (float(last10["volume"].mean()) / vol_ma) if vol_ma > 0 else 0.0

    scores = {}
    # 继续杀：跌破均价线 + 尾段仍在下行 + 放量
    s = 0.0
    if below_vwap:
        s += 0.35
    if down_ratio_last >= 0.6:
        s += 0.30
    if last_vol_ratio >= 1.5:
        s += 0.20
    if max_drop >= 3.0:
        s += 0.15
    scores["continue_down"] = s
    # 磨筹码：缩量 + 跌幅温和 + 围绕均价线
    s = 0.0
    if last_vol_ratio < 1.2:
        s += 0.35
    if max_drop < 3.0:
        s += 0.30
    if abs(am_last - am_vwap) / am_vwap * 100 < 1.0 if am_vwap > 0 else False:
        s += 0.20
    if down_ratio_last < 0.6:
        s += 0.15
    scores["range_grind"] = s
    # 尾盘拉板：深水 + 缩量止跌 + 尾段回升
    s = 0.0
    if max_drop >= 3.0:
        s += 0.30
    if last_vol_ratio < 1.0:
        s += 0.25
    if not below_vwap or am_last >= am_low * 1.01:
        s += 0.25
    if down_ratio_last < 0.5:
        s += 0.20
    scores["tail_surge"] = s

    tot = sum(scores.values()) or 1.0
    scenarios = []
    for sc in NOON_SCENARIOS:
        p = scores.get(sc["key"], 0.0) / tot
        scenarios.append({**sc, "prob_pct": round(p * 100, 1)})
    scenarios.sort(key=lambda x: -x["prob_pct"])
    for i, sc in enumerate(scenarios):
        sc["rank"] = i + 1

    return {
        "ok": True,
        "session_date": str(am["timestamps"].iloc[0].date()),
        "snapshot": {
            "am_open": round(am_open, 3),
            "am_close": round(am_last, 3),
            "am_low": round(am_low, 3),
            "am_high": round(am_high, 3),
            "am_vwap": round(am_vwap, 3),
            "am_gain_pct": round(am_gain, 2),
            "max_drop_pct": round(max_drop, 2),
            "below_vwap": bool(below_vwap),
            "am_volume": round(am_vol, 0),
            "am_vol_share_pct": am_vol_share_pct,
            "am_bars": int(len(am)),
        },
        "sector": sector if sector else None,
        "scenarios": scenarios,
        "primary": scenarios[0] if scenarios else None,
        "params": threshold_provenance(),
        "degraded": [x for x in unavailable_factors()
                     if x["key"] in ("book_cancel_rate", "large_order_flow")],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

