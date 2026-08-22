"""Convert user's own K-line data files to the Kronos webui format.

Place your raw file (CSV/Excel) anywhere, then run:
    python convert_data.py <input_file> [output_name]

Recognized column aliases:
    date/timestamps/时间/日期/date   -> timestamps
    open/开盘                        -> open
    high/最高/最高价                  -> high
    low/最低/最低价                   -> low
    close/收盘/收盘价                 -> close
    volume/成交量/vol                 -> volume
    amount/成交额/成交金额             -> amount

Output: D:\\a\\kronos\\data\\<output_name>.csv  (auto-named if omitted)

Also supports Chinese column names and files where the first column is the date
without a header name (e.g. exported from some tools).
"""
import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data")
os.makedirs(OUT_DIR, exist_ok=True)

ALIASES = {
    "timestamps": ["timestamps", "timestamp", "date", "datetime", "日期", "时间", "时间戳", "交易日", "交易日期"],
    "open": ["open", "o", "开盘", "开盘价", "今开", "开"],
    "high": ["high", "h", "最高", "最高价", "最高点", "高"],
    "low": ["low", "l", "最低", "最低价", "最低点", "低"],
    "close": ["close", "c", "收盘", "收盘价", "昨收", "收"],
    "volume": ["volume", "vol", "成交量", "量", "交易量"],
    "amount": ["amount", "amt", "成交额", "成交金额", "额"],
}


def find_col(cols, key):
    lower = {str(c).strip().lower(): str(c) for c in cols}
    for alias in ALIASES[key]:
        a = alias.lower()
        if a in lower:
            return lower[a]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="path to your CSV/Excel file")
    ap.add_argument("output", nargs="?", default=None, help="output name (without .csv)")
    args = ap.parse_args()

    src = args.input
    if not os.path.exists(src):
        raise SystemExit(f"file not found: {src}")

    if src.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(src)
    else:
        # Try common encodings for CN-exported CSVs
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            try:
                df = pd.read_csv(src, encoding=enc)
                break
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        else:
            raise SystemExit("cannot decode file, try saving as .xlsx")

    print("input columns:", list(df.columns))

    mapping = {}
    for key in ALIASES:
        mapping[key] = find_col(df.columns, key)

    missing = [k for k, v in mapping.items() if v is None and k != "amount"]
    if missing:
        raise SystemExit(f"missing required columns: {missing}")

    out_df = pd.DataFrame()
    out_df["timestamps"] = pd.to_datetime(df[mapping["timestamps"]])
    for key in ("open", "high", "low", "close", "volume"):
        col = mapping[key]
        out_df[key] = pd.to_numeric(df[col], errors="coerce")
    if mapping["amount"]:
        out_df["amount"] = pd.to_numeric(df[mapping["amount"]], errors="coerce")
    else:
        out_df["amount"] = out_df["close"] * out_df["volume"]

    out_df = out_df.dropna(subset=["open", "high", "low", "close"]).sort_values("timestamps").reset_index(drop=True)

    if len(out_df) < 400:
        print(f"WARNING: only {len(out_df)} rows; Kronos needs >= 400 for prediction")

    name = args.output or os.path.splitext(os.path.basename(src))[0]
    out_path = os.path.join(OUT_DIR, f"{name}.csv")
    out_df.to_csv(out_path, index=False)
    print(f"OK: {len(out_df)} rows -> {out_path}")
    print("columns:", list(out_df.columns))
    print(f"range: {out_df['timestamps'].iloc[0].date()} ~ {out_df['timestamps'].iloc[-1].date()}")


if __name__ == "__main__":
    main()
