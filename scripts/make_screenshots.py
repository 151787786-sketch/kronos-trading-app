"""Generate README preview screenshots from REAL app data (matplotlib).

Creates dashboard-style figures from the live API so the README preview
section shows actual data (global indices, recommend leaderboard, forecast).
"""
import json
import os
import sys
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:7071"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

# find a CJK font on Windows
cjk = None
for f in fm.findSystemFonts():
    if any(k in f.lower() for k in ["msyh", "simhei", "simsun", "notosanscjksc"]):
        cjk = f
        break
plt.rcParams["font.family"] = fm.FontProperties(fname=cjk).get_name() if cjk else "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

DARK = "#1a1d24"
PANEL = "#22262f"
GREEN = "#22c55e"
RED = "#ef4444"
BLUE = "#3b82f6"
AMBER = "#f59e0b"
TEXT = "#d8dee9"
MUTED = "#8a93a5"


def get(path, timeout=30):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=timeout).read().decode("utf-8", "replace"))


def panel(ax, title):
    ax.set_facecolor(PANEL)
    ax.set_title(title, color="#fff", fontsize=11, loc="left", pad=10)
    ax.tick_params(colors=MUTED, labelsize=9)
    for s in ax.spines.values():
        s.set_color("#2e3440")


# ---- 1. overview dashboard (global + tracks + movers) ----
fig, axes = plt.subplots(1, 3, figsize=(16, 5), facecolor=DARK)
fig.suptitle("Kronos 股票交易助手 · 盘前速览", color="#fff", fontsize=16, y=0.98)

# global markets
ax = axes[0]
panel(ax, "外围市场 Global Markets")
g = get("/api/global")
labels, vals, pcts = [], [], []
for m in g.get("markets", []):
    labels.append(m["name"])
    vals.append(m["pct"])
    pcts.append(m["pct"])
colors = [GREEN if p >= 0 else RED for p in pcts]
bars = ax.barh(labels[::-1], vals[::-1], color=colors[::-1], alpha=0.9, height=0.6)
for i, p in enumerate(vals):
    ax.text(p + (0.05 if p >= 0 else -0.05), i, f"{p:+.2f}%", va="center",
            ha="left" if p >= 0 else "right", color=TEXT, fontsize=9)
ax.axvline(0, color=MUTED, lw=0.6)
ax.set_xlim(min(vals) - 1, max(vals) + 1.5)

# recommend leaderboard
ax = axes[1]
panel(ax, "今日荐股 Top5（全市场扫描）")
r = get("/api/recommend?top_n=5&forecast=0")
names, scores, pctc = [], [], []
for rc in r.get("recommendations", []):
    names.append(f"{rc['rank']}. {rc['name']}")
    scores.append(rc["score"])
    pctc.append(rc["pct"])
colors = [GREEN if p >= 0 else RED for p in pctc]
bars = ax.barh(names[::-1], scores[::-1], color=BLUE, alpha=0.85, height=0.55)
for i, s in enumerate(scores):
    ax.text(s + 0.1, i, f"{s:.1f}", va="center", color=TEXT, fontsize=9)
ax.set_xlabel("综合评分", color=MUTED, fontsize=9)

# forecast example
ax = axes[2]
panel(ax, "Kronos 预测（600585 海螺水泥）")
k = get("/api/market/kline?symbol=600585&period=day&bars=120")
hist = k["bars"]
f = get("/api/forecast?symbol=600585&pred_len=30")
x_hist = [b["timestamps"][5:10] for b in hist]
y_hist = [b["close"] for b in hist]
x_fc = f["dates"]
y_fc = f["close"]
band = 0.15
ax.plot(x_hist, y_hist, color="#1f77b4", lw=1, label="历史收盘")
ax.plot(x_fc, y_fc, color="#e879f9", lw=1.8, label="Kronos预测")
ax.fill_between(range(len(x_fc)), [v*(1-band) for v in y_fc], [v*(1+band) for v in y_fc],
                color="#e879f9", alpha=0.15, label="±15%误差带")
ax.legend(fontsize=8, facecolor=PANEL, edgecolor="#2e3440", labelcolor=TEXT)
ax.tick_params(axis="x", labelsize=6, rotation=45)

fig.tight_layout(rect=[0, 0, 1, 0.93])
fig.savefig(os.path.join(OUT, "overview.png"), dpi=110, facecolor=DARK)
print("saved overview.png")

# ---- 2. recommend leaderboard card (no forecast for speed) ----
fig, ax = plt.subplots(figsize=(9, 6), facecolor=DARK)
panel(ax, "自动荐股 · 全市场涨幅榜 Top5")
r = get("/api/recommend?top_n=5&forecast=0")
rows = r.get("recommendations", [])
y = 0
for rc in rows:
    color = GREEN if rc["pct"] >= 0 else RED
    ax.text(0, y, f"#{rc['rank']}", fontsize=14, fontweight="bold", color=AMBER, va="center")
    ax.text(0.07, y, f"{rc['name']} ({rc['symbol']})", fontsize=13, color=TEXT, va="center")
    ax.text(0.62, y, f"{rc['pct']:+.1f}%", fontsize=13, color=color, va="center", ha="center")
    ax.text(0.72, y, f"量比 {rc['volume_ratio']:.1f}", fontsize=11, color=MUTED, va="center")
    ax.text(0.9, y, f"评分 {rc['score']:.1f}", fontsize=13, fontweight="bold", color=BLUE, va="center")
    ax.text(0, y-0.32, "  " + rc["summary"][:60], fontsize=9, color=MUTED, va="center")
    y -= 1.3
ax.set_xlim(0, 1.15)
ax.set_ylim(y-0.5, 1)
ax.axis("off")
fig.savefig(os.path.join(OUT, "recommend.png"), dpi=110, facecolor=DARK)
print("saved recommend.png")

print("done")
