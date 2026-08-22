"""Download HK stock 0020 (SenseTime 商汤) daily K-line data for Kronos.

- Fetches daily OHLCV from eastmoney (bypasses the local proxy that blocks CN finance sites)
- Saves CSV in the format the Kronos webui expects (timestamps, open, high, low, close, volume, amount)
- Output: data\0020_SenseTime_daily.csv
"""
import os
import sys
import time

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data")
os.makedirs(OUT_DIR, exist_ok=True)

# Bypass the Windows system proxy (Clash etc.) which blocks CN finance endpoints.
session = requests.Session()
session.trust_env = False
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
})

URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
PARAMS = {
    "secid": "116.00020",          # 116 = HK market, 00020 = SenseTime
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    "klt": "101",                   # daily
    "fqt": "0",                     # no adjustment
    "end": "20500000",              # latest
}

def fetch(lmt):
    p = dict(PARAMS, lmt=str(lmt))
    last_err = None
    for attempt in range(4):
        try:
            r = session.get(URL, params=p, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err

def main():
    # Fetch in chunks (server drops very large single requests).
    all_klines = []
    cursor = 0
    chunk = 500
    while True:
        j = fetch(chunk)
        klines = (j.get("data") or {}).get("klines") or []
        if not klines:
            break
        all_klines.extend(klines)
        cursor += len(klines)
        if len(klines) < chunk:
            break
        # kline[0] is the date of the oldest item in this chunk; fetch earlier than that.
        earliest = klines[0].split(",")[0]
        PARAMS["end"] = earliest
        print(f"  fetched {cursor} rows, continuing before {earliest} ...")

    if not all_klines:
        raise SystemExit("no klines returned")

    rows = []
    for k in all_klines:
        p = k.split(",")
        # f51 date, f52 open, f53 close, f54 high, f55 low, f56 volume, f57 amount, ...
        rows.append({
            "timestamps": p[0],
            "open": float(p[1]),
            "close": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
            "volume": float(p[5]),
            "amount": float(p[6]),
        })

    df = pd.DataFrame(rows)
    df["timestamps"] = pd.to_datetime(df["timestamps"])
    df = df.sort_values("timestamps").reset_index(drop=True)

    out = os.path.join(OUT_DIR, "0020_SenseTime_daily.csv")
    df.to_csv(out, index=False)
    print(f"OK: {len(df)} rows")
    print(f"range: {df['timestamps'].iloc[0]} ~ {df['timestamps'].iloc[-1]}")
    print(f"latest close: {df['close'].iloc[-1]}")
    print(f"saved -> {out}")
    print("columns:", list(df.columns))

if __name__ == "__main__":
    main()
