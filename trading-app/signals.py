"""Trading signals derived from indicators and optionally Kronos forecast direction."""
import pandas as pd


def _cross_above(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_below(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def indicator_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Compute rule-based signals on a DataFrame that already has indicator columns.
    Returns DataFrame with columns: signal (BUY/SELL/HOLD), reason, strength (0-1)."""
    out = pd.DataFrame(index=df.index)
    out["signal"] = "HOLD"
    out["reason"] = ""
    out["strength"] = 0.0

    close = df["close"]

    # MA golden/death cross (ma5 vs ma20)
    golden = _cross_above(df["ma5"], df["ma20"])
    death = _cross_below(df["ma5"], df["ma20"])
    out.loc[golden, "signal"] = "BUY"
    out.loc[golden, "reason"] = "MA5 上穿 MA20（金叉）"
    out.loc[golden, "strength"] = 0.7
    out.loc[death, "signal"] = "SELL"
    out.loc[death, "reason"] = "MA5 下穿 MA20（死叉）"
    out.loc[death, "strength"] = 0.7

    # MACD cross
    macd_golden = _cross_above(df["dif"], df["dea"])
    macd_death = _cross_below(df["dif"], df["dea"])
    out.loc[macd_golden & (out["signal"] == "HOLD"), "signal"] = "BUY"
    out.loc[macd_golden & (out["signal"] == "HOLD"), "reason"] = "MACD 金叉"
    out.loc[macd_golden & (out["signal"] == "HOLD"), "strength"] = 0.6
    out.loc[macd_death & (out["signal"] == "HOLD"), "signal"] = "SELL"
    out.loc[macd_death & (out["signal"] == "HOLD"), "reason"] = "MACD 死叉"
    out.loc[macd_death & (out["signal"] == "HOLD"), "strength"] = 0.6

    # RSI oversold/overbought
    rsi = df["rsi"]
    oversold = rsi < 30
    overbought = rsi > 70
    out.loc[oversold & (out["signal"] == "HOLD"), "signal"] = "BUY"
    out.loc[oversold & (out["signal"] == "HOLD"), "reason"] = f"RSI 超卖 ({rsi.round(1)})"
    out.loc[oversold & (out["signal"] == "HOLD"), "strength"] = 0.5
    out.loc[overbought & (out["signal"] == "HOLD"), "signal"] = "SELL"
    out.loc[overbought & (out["signal"] == "HOLD"), "reason"] = f"RSI 超买 ({rsi.round(1)})"
    out.loc[overbought & (out["signal"] == "HOLD"), "strength"] = 0.5

    # KDJ golden/death cross
    kdj_golden = _cross_above(df["kdj_k"], df["kdj_d"])
    kdj_death = _cross_below(df["kdj_k"], df["kdj_d"])
    out.loc[kdj_golden & (out["signal"] == "HOLD"), "signal"] = "BUY"
    out.loc[kdj_golden & (out["signal"] == "HOLD"), "reason"] = "KDJ 金叉"
    out.loc[kdj_golden & (out["signal"] == "HOLD"), "strength"] = 0.5
    out.loc[kdj_death & (out["signal"] == "HOLD"), "signal"] = "SELL"
    out.loc[kdj_death & (out["signal"] == "HOLD"), "reason"] = "KDJ 死叉"
    out.loc[kdj_death & (out["signal"] == "HOLD"), "strength"] = 0.5

    # Bollinger band touches
    touch_low = df["close"] <= df["boll_lower"]
    touch_high = df["close"] >= df["boll_upper"]
    out.loc[touch_low & (out["signal"] == "HOLD"), "signal"] = "BUY"
    out.loc[touch_low & (out["signal"] == "HOLD"), "reason"] = "触及布林下轨"
    out.loc[touch_low & (out["signal"] == "HOLD"), "strength"] = 0.4
    out.loc[touch_high & (out["signal"] == "HOLD"), "signal"] = "SELL"
    out.loc[touch_high & (out["signal"] == "HOLD"), "reason"] = "触及布林上轨"
    out.loc[touch_high & (out["signal"] == "HOLD"), "strength"] = 0.4

    return out


def forecast_direction_signals(df: pd.DataFrame, forecast: dict) -> dict:
    """Combine Kronos forecast direction with the latest indicator signal.
    forecast: {'last_close': float, 'pred_close': float, 'pred_high': float, ...}
    Returns a dict with signal/reason/strength/confidence."""
    last_close = df["close"].iloc[-1]
    pred_close = forecast.get("pred_close", last_close)

    if pred_close > last_close * 1.02:
        direction = "BUY"
        reason = f"Kronos 预测看涨（{last_close:.2f} → {pred_close:.2f}，+{(pred_close/last_close-1)*100:.1f}%）"
        strength = min(0.9, 0.5 + (pred_close / last_close - 1) * 10)
    elif pred_close < last_close * 0.98:
        direction = "SELL"
        reason = f"Kronos 预测看跌（{last_close:.2f} → {pred_close:.2f}，{(pred_close/last_close-1)*100:.1f}%）"
        strength = min(0.9, 0.5 + (1 - pred_close / last_close) * 10)
    else:
        direction = "HOLD"
        reason = f"Kronos 预测震荡（{last_close:.2f} → {pred_close:.2f}）"
        strength = 0.3

    # Combine with the latest indicator signal
    sig_df = indicator_signals(df)
    last_sig = sig_df.iloc[-1]
    combined = direction
    if last_sig["signal"] != "HOLD" and last_sig["signal"] != direction:
        combined = "HOLD"
        reason += f"；指标信号反向（{last_sig['reason']}），建议观望"
    elif last_sig["signal"] == direction:
        reason += f"；指标同向（{last_sig['reason']}）"

    return {
        "signal": combined,
        "reason": reason,
        "strength": round(strength, 2),
    }
