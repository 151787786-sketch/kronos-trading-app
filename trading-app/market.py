"""Market data module: fetch A-share daily K-lines and realtime quotes via Tencent API.

All requests bypass the Windows system proxy (Clash etc.) which blocks CN quote sites.
Data is cached under trading-app/data/ to avoid re-fetching.
"""
import os
import time

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

session = requests.Session()
session.trust_env = False
session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
REALTIME_URL = "https://qt.gtimg.cn/q="  # realtime quote, multiple codes comma separated

# Cache of last fetch time per symbol, to throttle
_last_fetch = {}


def market_prefix(symbol: str) -> str:
    """1 = Shanghai, 0 = Shenzhen"""
    if symbol.startswith(("6", "9", "5")):
        return "sh"
    return "sz"


def full_symbol(symbol: str) -> str:
    return f"{market_prefix(symbol)}{symbol}"


def _get_json(url, params=None, retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


# Supported periods: day / 1m / 5m / 15m / 30m / 60m  (Tencent klt codes)
PERIODS = {
    "day": {"klt": "101", "cache": "day"},
    "1m":  {"klt": "1",  "cache": "1m"},
    "5m":  {"klt": "5",  "cache": "5m"},
    "15m": {"klt": "15", "cache": "15m"},
    "30m": {"klt": "30", "cache": "30m"},
    "60m": {"klt": "60", "cache": "60m"},
}


def fetch_daily(symbol: str, bars: int = 600, force: bool = False) -> pd.DataFrame:
    """Fetch daily OHLCV for an A-share symbol. Returns sorted DataFrame with
    columns timestamps/open/high/low/close/volume/amount.

    Note: the Tencent API returns at most ~640 qfq-adjusted daily bars for most
    symbols, so bars is a target, not a guarantee. The cache file name includes
    the requested bar count so different consumers never share stale caches.
    """
    return fetch_kline(symbol, period="day", bars=bars, force=force)


def _cache_ttl(period: str) -> int:
    """Smart cache TTL: during A-share trading hours data changes constantly
    (refresh minute data every 1 min, daily every 5 min); outside trading hours
    a long TTL (12h) is fine."""
    import datetime
    now = datetime.datetime.now()
    # trading days: Mon-Fri, sessions 09:30-11:30 and 13:00-15:00
    if now.weekday() < 5:
        t = now.hour * 60 + now.minute
        in_session = (9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60)
        if in_session:
            return 60 if period != "day" else 300  # minute: 1min, daily: 5min
    return 12 * 3600


def fetch_kline(symbol: str, period: str = "day", bars: int = 600, force: bool = False) -> pd.DataFrame:
    """Fetch OHLCV K-lines for an A-share symbol at any supported period.

    period: day / 1m / 5m / 15m / 30m / 60m
    Returns sorted DataFrame with columns
    timestamps/open/high/low/close/volume/amount.
    """
    if period not in PERIODS:
        raise ValueError(f"unsupported period {period}, use {list(PERIODS)}")

    fs = full_symbol(symbol)
    spec = PERIODS[period]
    cache = os.path.join(DATA_DIR, f"{symbol}_{spec['cache']}_{bars}.csv")

    if not force and os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["timestamps"])
        mtime = os.path.getmtime(cache)
        if time.time() - mtime < _cache_ttl(period):
            return df

    if period == "day":
        param = f"{fs},day,,,{bars},qfq"
        j = _get_json(KLINE_URL, {"param": param})
        d = (j.get("data") or {}).get(fs, {})
        klines = d.get("qfqday") or d.get("day") or []
        if not klines:
            raise RuntimeError(
                f"未找到 {symbol} 的日线数据（请确认代码正确：6 位数字，如 600585/000001）"
            )
        # day rows: [date, open, close, high, low, volume, ...]
        rows = []
        for k in klines:
            rows.append({
                "timestamps": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]),
            })
    else:
        # minute bars come from the mkline endpoint
        m = spec["klt"]
        j = _get_json("https://ifzq.gtimg.cn/appstock/app/kline/mkline",
                      {"param": f"{fs},m{m},,{bars}"})
        d = (j.get("data") or {}).get(fs, {})
        klines = d.get(f"m{m}") or []
        if not klines:
            raise RuntimeError(f"no klines for {symbol} {period}: {str(j)[:200]}")
        # minute rows: [YYYYMMDDHHMM, open, close, high, low, volume, {}, pct]
        rows = []
        for k in klines:
            ts = pd.to_datetime(k[0], format="%Y%m%d%H%M")
            rows.append({
                "timestamps": ts,
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"未获取到 {symbol} 的 {period} 周期数据，请稍后重试")
    df["timestamps"] = pd.to_datetime(df["timestamps"])
    df = df.sort_values("timestamps").reset_index(drop=True)
    df["amount"] = (df["volume"] * df["close"]).round(2)

    df.to_csv(cache, index=False)
    return df


def fetch_realtime(codes) -> list:
    """Fetch realtime quotes for a list of 6-digit symbols.
    Returns list of dicts: symbol/name/price/change/pct/open/high/low/volume/amount/time."""
    if not codes:
        return []
    fs = [full_symbol(c) for c in codes]
    url = REALTIME_URL + ",".join(fs)
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        text = r.text
    except Exception:
        return []

    result = []
    for line in text.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, payload = line.split("=", 1)
        payload = payload.strip('"').split("~")
        if len(payload) < 40:
            continue
        try:
            result.append({
                "symbol": key[-6:],
                "name": payload[1],
                "price": float(payload[3]),
                "change": float(payload[31]) if payload[31] else 0.0,
                "pct": float(payload[32]) if payload[32] else 0.0,
                "open": float(payload[5]),
                "high": float(payload[33]) if payload[33] else 0.0,
                "low": float(payload[34]) if payload[34] else 0.0,
                "volume": float(payload[36]) if payload[36] else 0.0,
                "amount": float(payload[37]) if payload[37] else 0.0,
                "time": payload[30],
                "turnover": float(payload[38]) if payload[38] else 0.0,  # 换手率%
                "volume_ratio": float(payload[46]) if len(payload) > 46 and payload[46] else 0.0,  # 量比
            })
        except (ValueError, IndexError):
            continue
    return result


def fetch_realtime_one(symbol: str):
    rows = fetch_realtime([symbol])
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# symbol resolution: accept 6-digit codes OR Chinese/English stock names
# ---------------------------------------------------------------------------

SEARCH_URL = "https://smartbox.gtimg.cn/s3/"
_search_cache = {}


def search_stock(query: str) -> list:
    """Search A-share/HK/US symbols by code or name via Tencent smartbox.
    Returns list of dicts: market/symbol/name/full_symbol/pinyin/type."""
    query = query.strip()
    if not query:
        return []
    if query in _search_cache:
        return _search_cache[query]

    try:
        r = session.get(SEARCH_URL, params={"q": query, "t": "all"}, timeout=15)
        r.encoding = "gbk"
        text = r.text
    except Exception:
        return []

    def _decode_name(s):
        # smartbox escapes non-ASCII names as \uXXXX sequences
        try:
            if "\\u" in s:
                return s.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
        return s

    results = []
    # format: v_hint="sz~002202~金风科技~jfkj~GP-A^hk~02208~...^..."
    if "=" not in text:
        return []
    payload = text.split("=", 1)[1].strip().strip('"')
    for item in payload.split("^"):
        parts = item.split("~")
        if len(parts) < 5:
            continue
        market_code, code, name, pinyin, typ = parts[0], parts[1], parts[2], parts[3], parts[4]
        # keep A-share (GP-A / GP-A-KCB) and HK (GP)
        if typ.startswith("GP") or typ.startswith("GP-A"):
            results.append({
                "market": market_code,      # sh / sz / hk
                "symbol": code,
                "name": _decode_name(name),
                "pinyin": pinyin,
                "type": typ,
            })
    _search_cache[query] = results
    return results


def resolve_symbol(query: str) -> dict:
    """Resolve a user input (6-digit code, Chinese name, or pinyin) into an
    A-share symbol. Returns {'symbol','name','market','full_symbol'} or raises
    ValueError when nothing A-share is found.

    Prefers A-share results (sh/sz); falls back to HK only if no A-share.
    """
    query = query.strip()
    if not query:
        raise ValueError("输入为空")

    results = search_stock(query)
    if not results:
        raise ValueError(f"未找到「{query}」对应的股票，请检查输入（6 位代码或名称）")

    # exact code match first
    if query.isdigit() and len(query) == 6:
        for r in results:
            if r["symbol"] == query and r["market"] in ("sh", "sz"):
                full = f"{r['market']}{r['symbol']}"
                return {"symbol": r["symbol"], "name": r["name"],
                        "market": r["market"], "full_symbol": full}

    # prefer A-share
    for r in results:
        if r["market"] in ("sh", "sz"):
            full = f"{r['market']}{r['symbol']}"
            return {"symbol": r["symbol"], "name": r["name"],
                    "market": r["market"], "full_symbol": full}

    # fall back to first result (e.g. HK)
    r = results[0]
    return {"symbol": r["symbol"], "name": r["name"],
            "market": r["market"], "full_symbol": f"{r['market']}{r['symbol']}"}
