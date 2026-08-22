"""Buy/Sell point analysis: support/resistance levels, targets, stop-loss and
a combined actionable recommendation combining technicals + Kronos forecast."""
import numpy as np
import pandas as pd

from indicators import all_indicators


def _recent_pivots(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Mark local swing highs/lows over a rolling window."""
    high = df["high"]
    low = df["low"]
    pivot_high = high == high.rolling(window, center=True, min_periods=1).max()
    pivot_low = low == low.rolling(window, center=True, min_periods=1).min()
    out = pd.DataFrame({"pivot_high": pivot_high, "pivot_low": pivot_low}, index=df.index)
    return out


def support_resistance(df: pd.DataFrame, lookback: int = 120) -> dict:
    """Compute support and resistance levels from recent swings + indicators."""
    df = df.tail(lookback).reset_index(drop=True)
    piv = _recent_pivots(df, window=10)

    res_pts = sorted(df.loc[piv["pivot_high"], "high"].tolist())
    sup_pts = sorted(df.loc[piv["pivot_low"], "low"].tolist())

    # Cluster nearby levels
    def cluster(points, tol_pct=0.005):
        if not points:
            return []
        clusters = []
        cur = [points[0]]
        for p in points[1:]:
            if p / cur[0] - 1 < tol_pct:
                cur.append(p)
            else:
                clusters.append(float(np.mean(cur)))
                cur = [p]
        clusters.append(float(np.mean(cur)))
        return clusters

    resistances = cluster(res_pts)[-3:] if res_pts else []
    supports = cluster(sup_pts)[:3] if sup_pts else []

    last = df["close"].iloc[-1]
    # keep only levels near the current price (within 30%)
    resistances = [r for r in resistances if r > last * 1.001 and r < last * 1.30]
    supports = [s for s in supports if s < last * 0.999 and s > last * 0.70]

    # add indicator-based levels
    ind = all_indicators(df)
    boll_lower = float(ind["boll_lower"].iloc[-1])
    boll_upper = float(ind["boll_upper"].iloc[-1])
    ma20 = float(ind["ma20"].iloc[-1])
    ma60 = float(ind["ma60"].iloc[-1])

    return {
        "last_close": float(last),
        "resistances": [round(r, 2) for r in resistances[-3:]],
        "supports": [round(s, 2) for s in supports[:3]],
        "boll_upper": round(boll_upper, 2),
        "boll_lower": round(boll_lower, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
    }


def buy_sell_plan(df: pd.DataFrame, forecast: dict = None) -> dict:
    """Generate a complete buy/sell plan.

    forecast: optional result from kronos_service.forecast() for direction/targets.
    Returns dict with entry zones, targets, stop-loss, and recommendation.
    """
    ind = all_indicators(df)
    last = df["close"].iloc[-1]
    levels = support_resistance(df)

    # volatility-based stop (ATR-like: mean(|high-low|) over last 14 bars)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift(1)).abs(),
                    (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())

    rsi = float(ind["rsi"].iloc[-1])
    macd_hist = float(ind["macd"].iloc[-1])
    kdj_k = float(ind["kdj_k"].iloc[-1])
    kdj_d = float(ind["kdj_d"].iloc[-1])

    # Kronos direction/targets
    fc = forecast or {}
    pred_close = fc.get("end_close")
    pred_highs = fc.get("high") or [last]
    pred_lows = fc.get("low") or [last]
    pred_high = max(pred_highs[-1], last)
    pred_low = min(pred_lows[-1], last)

    support = levels["supports"][0] if levels["supports"] else levels["boll_lower"]
    resistance = levels["resistances"][-1] if levels["resistances"] else levels["boll_upper"]

    # ---- recommendation scoring ----
    score = 0.0
    reasons = []

    # trend
    if ind["ma5"].iloc[-1] > ind["ma20"].iloc[-1] > ind["ma60"].iloc[-1]:
        score += 1.0; reasons.append("均线多头排列（MA5>MA20>MA60）")
    elif ind["ma5"].iloc[-1] < ind["ma20"].iloc[-1] < ind["ma60"].iloc[-1]:
        score -= 1.0; reasons.append("均线空头排列（MA5<MA20<MA60）")
    else:
        reasons.append("均线缠绕，趋势不明")

    # MACD
    if macd_hist > 0 and ind["dif"].iloc[-1] > ind["dea"].iloc[-1]:
        score += 0.7; reasons.append("MACD 多头（DIF>DEA，红柱）")
    elif macd_hist < 0 and ind["dif"].iloc[-1] < ind["dea"].iloc[-1]:
        score -= 0.7; reasons.append("MACD 空头（DIF<DEA，绿柱）")
    else:
        reasons.append("MACD 走平")

    # RSI
    if rsi < 30:
        score += 0.8; reasons.append(f"RSI 超卖（{rsi:.0f}），存在反弹机会")
    elif rsi > 70:
        score -= 0.8; reasons.append(f"RSI 超买（{rsi:.0f}），警惕回调")
    else:
        reasons.append(f"RSI 中性（{rsi:.0f}）")

    # KDJ
    if kdj_k > kdj_d and kdj_k < 80:
        score += 0.5; reasons.append("KDJ 金叉向上")
    elif kdj_k < kdj_d and kdj_k > 20:
        score -= 0.5; reasons.append("KDJ 死叉向下")

    # Kronos direction
    if pred_close is not None:
        pct = (pred_close / last - 1) * 100
        if pct > 2:
            score += 1.2; reasons.append(f"Kronos 预测上涨 {pct:.1f}%")
        elif pct < -2:
            score -= 1.2; reasons.append(f"Kronos 预测下跌 {pct:.1f}%")
        else:
            reasons.append(f"Kronos 预测横盘（{pct:+.1f}%）")

    # ---- build plan ----
    stop_loss = round(last - 1.5 * atr, 2)
    target_up = round(last + 2.0 * atr, 2)
    target_down = round(last - 2.0 * atr, 2)

    if pred_close is not None and pred_high > 0:
        # Kronos-based targets
        target_up = round(max(target_up, pred_high * 0.98), 2)
        target_down = round(min(target_down, pred_low * 1.02), 2)

    if score >= 1.5:
        action = "买入"
        action_color = "buy"
        summary = f"综合得分 {score:+.1f}，信号偏多，可在支撑位分批建仓"
    elif score <= -1.5:
        action = "卖出"
        action_color = "sell"
        summary = f"综合得分 {score:+.1f}，信号偏空，建议减仓或离场"
    elif score > 0:
        action = "持有"
        action_color = "hold"
        summary = f"综合得分 {score:+.1f}，偏多但不强，持有为主，回踩支撑可加仓"
    else:
        action = "观望"
        action_color = "hold"
        summary = f"综合得分 {score:+.1f}，信号偏空/中性，观望等待明确方向"

    return {
        "action": action,
        "action_color": action_color,
        "score": round(score, 1),
        "summary": summary,
        "reasons": reasons,
        "last_close": round(float(last), 2),
        "entry_zone": {
            "low": round(float(support), 2),
            "high": round(float(resistance * 0.99), 2),
        },
        "support_levels": levels["supports"],
        "resistance_levels": levels["resistances"],
        "boll_upper": levels["boll_upper"],
        "boll_lower": levels["boll_lower"],
        "ma20": levels["ma20"],
        "ma60": levels["ma60"],
        "stop_loss": stop_loss,
        "target_up": target_up,
        "target_down": target_down,
        "atr": round(atr, 2),
        "rsi": round(rsi, 1),
        "macd_hist": round(macd_hist, 3),
        "kdj": {"k": round(kdj_k, 1), "d": round(kdj_d, 1)},
        "forecast_pct": round((pred_close / last - 1) * 100, 2) if pred_close else None,
    }
