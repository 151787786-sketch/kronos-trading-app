"""Draw Conch Cement forecast chart from the latest webui prediction result."""
import glob
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "600585_Conch_Cement_daily.csv")
OUT = os.path.join(ROOT, "webui", "prediction_results")

df = pd.read_csv(DATA)
df["timestamps"] = pd.to_datetime(df["timestamps"])

latest = sorted(glob.glob(os.path.join(OUT, "prediction_*.json")), key=os.path.getmtime)
if not latest:
    raise SystemExit("no prediction results found")
with open(latest[-1], encoding="utf-8") as f:
    data = json.load(f)

pred = pd.DataFrame(data["prediction_results"])
pred["timestamp"] = pd.to_datetime(pred["timestamp"])
pred = pred.sort_values("timestamp")

hist = df.iloc[-400:].copy()

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                               gridspec_kw={"height_ratios": [3, 1]})

ax1.plot(hist["timestamps"], hist["close"], color="#1f77b4", linewidth=1.2, label="History (400 bars, model input)")
ax1.plot(pred["timestamp"], pred["close"], color="#d62728", linewidth=1.6, label="Kronos forecast (next 120 trading days)")
ax1.axvline(hist["timestamps"].iloc[-1], color="gray", linestyle="--", linewidth=0.8)
ax1.set_ylabel("Price (CNY)")
ax1.set_title("Conch Cement (600585) - Kronos-base forecast: 400 history -> 120 future daily bars")
ax1.legend(loc="upper left", fontsize=10)
ax1.grid(True, alpha=0.3)

ax2.bar(hist["timestamps"], hist["volume"], width=1.0, color="#1f77b4", alpha=0.6, label="History volume")
ax2.bar(pred["timestamp"], pred["volume"], width=1.0, color="#d62728", alpha=0.6, label="Forecast volume")
ax2.set_ylabel("Volume")
ax2.legend(loc="upper left", fontsize=10)
ax2.grid(True, alpha=0.3)

fig.tight_layout()
out_path = os.path.join(ROOT, "conch_cement_forecast.png")
fig.savefig(out_path, dpi=110)
print(f"saved -> {out_path}")
print(f"history last close: {hist['close'].iloc[-1]}")
print(f"forecast close range: {pred['close'].min():.2f} ~ {pred['close'].max():.2f}")
print(f"forecast end close: {pred['close'].iloc[-1]:.2f}")
