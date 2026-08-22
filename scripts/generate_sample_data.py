"""Generate sample OHLCV K-line data for the Kronos webui (BTC/USDT 5min)."""
import os
import numpy as np
import pandas as pd

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(OUT, exist_ok=True)

rng = np.random.default_rng(42)
n = 800  # enough for lookback=400 + pred_len=120 + slack

# Random walk for close price, starting ~ 67000 (BTC)
close = np.zeros(n)
close[0] = 67000.0
for i in range(1, n):
    ret = rng.normal(0, 0.0016)
    vol_shock = rng.normal(0, 0.004) * (0.5 if rng.random() < 0.5 else 1.0)
    close[i] = close[i - 1] * (1 + ret + vol_shock)
close = np.abs(close)

open_ = np.empty(n)
open_[0] = close[0]
open_[1:] = close[:-1]
high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0012, n)))
low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0012, n)))
volume = rng.integers(50, 500, n).astype(float) * (1 + np.abs(rng.normal(0, 0.5, n)))
amount = volume * close

ts = pd.date_range("2025-06-01 00:00", periods=n, freq="5min")

df = pd.DataFrame({
    "timestamps": ts,
    "open": open_.round(2),
    "high": high.round(2),
    "low": low.round(2),
    "close": close.round(2),
    "volume": volume.round(2),
    "amount": amount.round(2),
})

path = os.path.join(OUT, "BTC_USDT_5min_sample.csv")
df.to_csv(path, index=False)
print(f"Wrote {len(df)} rows -> {path}")
