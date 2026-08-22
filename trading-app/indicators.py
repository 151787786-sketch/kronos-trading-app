"""Technical indicators computed on OHLCV DataFrames (pure pandas)."""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=1).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def ma(df: pd.DataFrame, windows=(5, 10, 20, 60)) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for w in windows:
        out[f"ma{w}"] = sma(df["close"], w)
    return out


def macd(df: pd.DataFrame, fast=12, slow=26, signal=9) -> pd.DataFrame:
    ema_fast = ema(df["close"], fast)
    ema_slow = ema(df["close"], slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "macd": hist}, index=df.index)


def rsi(df: pd.DataFrame, n=14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - 100 / (1 + rs)
    rsi_val = rsi_val.fillna(50)
    return pd.DataFrame({"rsi": rsi_val}, index=df.index)


def kdj(df: pd.DataFrame, n=9, k_period=3, d_period=3) -> pd.DataFrame:
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / k_period, adjust=False).mean()
    d = k.ewm(alpha=1 / d_period, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": j}, index=df.index)


def boll(df: pd.DataFrame, n=20, k=2.0) -> pd.DataFrame:
    mid = sma(df["close"], n)
    std = df["close"].rolling(n, min_periods=1).std().fillna(0)
    return pd.DataFrame({
        "boll_mid": mid,
        "boll_upper": mid + k * std,
        "boll_lower": mid - k * std,
    }, index=df.index)


def all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Merge all indicator series into one DataFrame aligned with df."""
    out = df.reset_index(drop=True).copy()
    out = pd.concat([out, ma(df), macd(df), rsi(df), kdj(df), boll(df)], axis=1)
    return out
