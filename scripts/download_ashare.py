"""Download A-share daily K-line data for the Kronos webui.

Generic script: D:\a\kronos\scripts\download_ashare.py --symbol 600585 --name Conch_Cement
Fetches daily OHLCV via the Tencent quote API (works behind the local proxy),
saves CSV in the Kronos webui format (timestamps, open, high, low, close, volume, amount).

Usage:
    python download_ashare.py --symbol 600585 --name Conch_Cement [--bars 1000]
"""
import argparse
import os
import time

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data")
os.makedirs(OUT_DIR, exist_ok=True)

session = requests.Session()
session.trust_env = False  # bypass the Windows system proxy (blocks CN quote sites)
session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})

URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def fetch(symbol_market: str, bars: int) -> list:
    """symbol_market: e.g. sh600585"""
    params = {"param": f"{symbol_market},day,,,{bars},qfq"}
    last_err = None
    for attempt in range(5):
        try:
            r = session.get(URL, params=params, timeout=25)
            r.raise_for_status()
            j = r.json()
            d = (j.get("data") or {}).get(symbol_market, {})
            ks = d.get("qfqday") or d.get("day") or []
            if ks:
                return ks
            last_err = RuntimeError("empty klines: " + str(j)[:200])
        except Exception as e:
            last_err = e
        time.sleep(2 * (attempt + 1))
    raise last_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="6-digit A-share code, e.g. 600585")
    ap.add_argument("--name", required=True, help="short name for the output file, e.g. Conch_Cement")
    ap.add_argument("--bars", type=int, default=1000, help="number of daily bars to fetch")
    args = ap.parse_args()

    # 1 = Shanghai, 0 = Shenzhen
    market = "sh" if args.symbol.startswith(("6", "9")) else "sz"
    sym = f"{market}{args.symbol}"

    klines = fetch(sym, args.bars)
    rows = []
    for k in klines:
        # Tencent day format: [date, open, close, high, low, volume, ...]
        rows.append({
            "timestamps": k[0],
            "open": float(k[1]),
            "close": float(k[2]),
            "high": float(k[3]),
            "low": float(k[4]),
            "volume": float(k[5]),
        })

    df = pd.DataFrame(rows)
    df["timestamps"] = pd.to_datetime(df["timestamps"])
    df = df.sort_values("timestamps").reset_index(drop=True)
    # Kronos webui accepts amount as optional; derive a reasonable proxy from volume*close
    df["amount"] = (df["volume"] * df["close"]).round(2)

    out = os.path.join(OUT_DIR, f"{args.symbol}_{args.name}_daily.csv")
    df.to_csv(out, index=False)
    print(f"OK: {len(df)} rows")
    print(f"range: {df['timestamps'].iloc[0].date()} ~ {df['timestamps'].iloc[-1].date()}")
    print(f"latest close: {df['close'].iloc[-1]}")
    print(f"saved -> {out}")
    print("columns:", list(df.columns))


if __name__ == "__main__":
    main()
