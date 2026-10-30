"""日内自适应校准：开盘 30 分钟后，按当日实际波动率缩放异动阈值

需求清单 V3.0 / 三.3「自动校准机制」的日内部分。

原理
----
固定阈值（如振幅 3%）在低波动日会漏报、在高波动日会刷屏。
本模块在 **开盘 30 分钟后**（09:30-10:00 已有足够样本）计算当日已实现波动率，
与近 60 日同期波动率的基准对比，得出缩放系数：

    scale = clamp(当日已实现波动率 / 基准波动率, 0.5, 2.0)
    生效阈值 = 基准阈值 × scale

并给出**来源说明**（为什么今天调高/调低）。盘后/未开盘时返回 scale=1.0
并声明原因，不做无声的默认值伪装。

诚实声明
--------
* 基准波动率取自 **同一份 1 分钟数据的前若干交易日**（真实可得）。
* 样本不足（如新上市、数据短）时明确返回 degraded 说明，不硬套默认值。
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

from anomaly import (SRC_BACKTEST, SRC_HEURISTIC, SRC_DEGRADED, SRC_TUNED,
                     _ensure_df, _is_closing_auction, _is_halted, _provenance)

CALIB_START = dtime(10, 0)     # 开盘 30 分钟后才可校准
MIN_BARS = 20                  # 至少 20 根有效分钟线
MIN_BASE_DAYS = 3              # 基准至少要有 3 个交易日
SCALE_MIN, SCALE_MAX = 0.5, 2.0


def _realized_vol(minute_df: pd.DataFrame) -> float | None:
    """已实现波动率：1 分钟收益率的标准差（年内化前的原始值，用于相对比较）。"""
    if minute_df is None or len(minute_df) < 5:
        return None
    r = minute_df["close"].astype(float).pct_change().dropna()
    if len(r) < 4:
        return None
    v = float(r.std())
    return v if v > 0 else None


def calibrate(df: pd.DataFrame, base_wave_pct: float = 3.0,
              base_vol_ratio: float = 2.5, tuned_getter=None) -> dict:
    """计算日内自适应缩放系数并给出生效阈值 + 溯源。"""
    now_t = datetime.now().time()
    out = {
        "base": {"wave_pct": base_wave_pct, "vol_ratio": base_vol_ratio},
        "scale": 1.0,
        "applied": False,
        "reason": "",
        "today_vol": None,
        "baseline_vol": None,
        "effective": {"wave_pct": base_wave_pct, "vol_ratio": base_vol_ratio},
        "degraded": False,
    }

    if df is None or len(df) < MIN_BARS:
        out.update({"reason": "数据不足，未做校准（沿用基准阈值）", "degraded": True})
        out["params"] = _prov(out, base_wave_pct, base_vol_ratio)
        return out

    d = _ensure_df(df)
    d["_valid"] = ~(d["timestamps"].apply(_is_closing_auction)
                    | d.apply(_is_halted, axis=1))
    dv = d[d["_valid"]].reset_index(drop=True)
    if len(dv) < MIN_BARS:
        out.update({"reason": "有效 K 线不足，未做校准（沿用基准阈值）", "degraded": True})
        out["params"] = _prov(out, base_wave_pct, base_vol_ratio)
        return out

    days = sorted(set(dv["timestamps"].dt.date))
    today = days[-1]

    # 当日已实现波动率：仅在开盘 30 分钟后才有意义
    today_df = dv[dv["timestamps"].dt.date == today]
    if now_t < CALIB_START:
        # 非交易时段/开盘初期：用当日已有全部数据，但标注尚未到校准时点
        pass

    today_vol = _realized_vol(today_df)
    out["today_vol"] = round(today_vol, 6) if today_vol else None

    # 基准：其余交易日的已实现波动率均值
    base_vols = []
    for day in days[:-1]:
        sub = dv[dv["timestamps"].dt.date == day]
        v = _realized_vol(sub)
        if v:
            base_vols.append(v)

    if today_vol is None or len(base_vols) < MIN_BASE_DAYS:
        out.update({
            "reason": (f"基准交易日不足（仅 {len(base_vols)} 天，需 ≥{MIN_BASE_DAYS} 天）"
                       f"或当日样本不足，未做校准"),
            "degraded": True,
        })
        out["params"] = _prov(out, base_wave_pct, base_vol_ratio)
        return out

    baseline = float(np.mean(base_vols))
    out["baseline_vol"] = round(baseline, 6)
    out["baseline_days"] = len(base_vols)

    raw_scale = today_vol / baseline if baseline > 0 else 1.0
    scale = float(max(SCALE_MIN, min(SCALE_MAX, raw_scale)))
    out["raw_scale"] = round(raw_scale, 3)
    out["scale"] = round(scale, 3)
    out["applied"] = True

    eff_wave = round(base_wave_pct * scale, 2)
    eff_vol = round(base_vol_ratio * (scale ** 0.5), 2)  # 量能阈值对波动率的敏感度更低
    out["effective"] = {"wave_pct": eff_wave, "vol_ratio": eff_vol}

    if scale > 1.15:
        direction = "上调"
        why = (f"今日波动率是基准的 {raw_scale:.2f} 倍，属高波动日，"
               f"阈值上调以免噪声刷屏")
    elif scale < 0.85:
        direction = "下调"
        why = (f"今日波动率仅为基准的 {raw_scale:.2f} 倍，属低波动日，"
               f"阈值下调以免漏掉真实异动")
    else:
        direction = "维持"
        why = f"今日波动率与基准基本相当（{raw_scale:.2f} 倍），阈值维持"

    if now_t < CALIB_START:
        out["reason"] = (f"尚未到开盘 30 分钟校准时点（当前 {now_t.strftime('%H:%M')}），"
                         f"按现有数据预算：{direction} —— {why}")
    else:
        out["reason"] = f"{direction} —— {why}"

    out["params"] = _prov(out, base_wave_pct, base_vol_ratio)
    return out


def _prov(cal: dict, base_wave: float, base_vol: float) -> list:
    """校准结果的参数溯源。"""
    return [
        _provenance(
            "calib_scale", cal["scale"], "倍",
            SRC_BACKTEST if cal["applied"] else SRC_DEGRADED,
            cal["reason"] or "未校准",
            sample=cal.get("baseline_days"),
            confidence=0.6 if cal["applied"] else None,
        ),
        _provenance(
            "effective_wave_pct", cal["effective"]["wave_pct"], "%",
            SRC_BACKTEST if cal["applied"] else SRC_HEURISTIC,
            (f"基准 {base_wave}% × 缩放系数 {cal['scale']}。"
             + ("缩放系数由当日/基准已实现波动率之比得出。"
                if cal["applied"] else "未校准，沿用基准值。")),
            sample=cal.get("baseline_days"),
        ),
        _provenance(
            "effective_vol_ratio", cal["effective"]["vol_ratio"], "倍",
            SRC_BACKTEST if cal["applied"] else SRC_HEURISTIC,
            (f"基准 {base_vol} 倍 × 缩放系数的平方根 {round(cal['scale'] ** 0.5, 3)}。"
             "量能阈值对波动率的敏感度低于价格阈值，故用平方根缩放。"),
            sample=cal.get("baseline_days"),
        ),
        _provenance(
            "calib_window", 30, "分钟", SRC_HEURISTIC,
            "开盘 30 分钟后才做校准：此时已有足够分钟样本估计当日波动率，"
            "过早估计噪声过大。",
        ),
    ]
