"""Draw demo chart: history + Kronos prediction vs actual (from saved prediction results)."""
import os
import sys
import glob
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "BTC_USDT_5min_sample.csv")
OUT = os.path.join(ROOT, "webui", "prediction_results")

df = pd.read_csv(DATA)
df["timestamps"] = pd.to_datetime(df["timestamps"])

# latest saved prediction result from the webui demo run
results_dir = os.path.join(ROOT, "webui", "prediction_results")
latest = sorted(glob.glob(os.path.join(results_dir, "prediction_*.json")), key=os.path.getmtime)
if not latest:
    raise SystemExit("no prediction results found")

with open(latest[-1], encoding="utf-8") as f:
    data = json.load(f)

pred = pd.DataFrame(data["prediction_results"])
pred["timestamp"] = pd.to_datetime(pred["timestamp"])
pred = pred.sort_values("timestamp")

lookback = 400
hist = df.iloc[:lookback]
# actual values corresponding to the predicted window (already in file if available)
tail = df.iloc[lookback:]

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                               gridspec_kw={"height_ratios": [3, 1]})

# --- price panel ---
ax1.plot(hist["timestamps"], hist["close"], color="#1f77b4", linewidth=1.2, label="Historical close (input)")
ax1.plot(pred["timestamp"], pred["close"], color="#d62728", linewidth=1.6, label="Kronos forecast (120 bars)")
if len(tail):
    ax1.plot(tail["timestamps"], tail["close"], color="#2ca02c", linewidth=1.0, alpha=0.8, label="Actual (for comparison)")
ax1.axvline(hist["timestamps"].iloc[-1], color="gray", linestyle="--", linewidth=0.8)
ax1.text(hist["timestamps"].iloc[-1], ax1.get_ylim()[1], " forecast start", color="gray", fontsize=9)
ax1.set_ylabel("Price (USDT)")
ax1.set_title("Kronos-base demo: BTC/USDT 5min K-line forecast (400 history -> 120 future)")
ax1.legend(loc="upper left", fontsize=10)
ax1.grid(True, alpha=0.3)

# --- volume panel ---
ax2.bar(hist["timestamps"], hist["volume"], width=0.002, color="#1f77b4", alpha=0.6, label="Historical volume")
ax2.bar(pred["timestamp"], pred["volume"], width=0.002, color="#d62728", alpha=0.6, label="Forecast volume")
ax2.set_ylabel("Volume")
ax2.legend(loc="upper left", fontsize=10)
ax2.grid(True, alpha=0.3)

fig.tight_layout()
out_path = os.path.join(ROOT, "demo_forecast.png")
fig.savefig(out_path, dpi=110)
print(f"saved -> {out_path}")
