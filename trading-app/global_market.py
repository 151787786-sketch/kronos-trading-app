"""Global market panel: US indices, HK indices, Nikkei, KOSPI.

Tencent quote API for US/HK indices, Sina for Nikkei/KOSPI.
All requests bypass the Windows system proxy.
"""
import time

import requests

session = requests.Session()
session.trust_env = False
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Referer": "https://gu.qq.com/"})

TENCENT_CODES = {
    "usDJI": "道琼斯",
    "usIXIC": "纳斯达克",
    "usINX": "标普500",
    "hkHSI": "恒生指数",
    "hkHSCEI": "国企指数",
}

SINA_CODES = {
    "znb_NKY": ("日经225", "N225"),
    "znb_KOSPI": ("首尔综合", "KOSPI"),
}

_cache = {}
_TTL = 60  # refresh at most once per minute


def _tencent_quotes():
    codes = ",".join(TENCENT_CODES.keys())
    r = session.get("https://qt.gtimg.cn/q=" + codes, timeout=15)
    r.encoding = "gbk"
    out = []
    for line in r.text.strip().split(";"):
        if "=" not in line:
            continue
        key, payload = line.split("=", 1)
        f = payload.strip('"').split("~")
        if len(f) < 35:
            continue
        out.append({
            "name": f[1],
            "price": float(f[3]) if f[3] else None,
            "change": float(f[31]) if f[31] else None,
            "pct": float(f[32]) if f[32] else None,
            "time": f[30],
        })
    return out


def _sina_quotes():
    codes = ",".join(SINA_CODES.keys())
    r = session.get("https://hq.sinajs.cn/list=" + codes,
                    headers={"Referer": "https://finance.sina.com.cn",
                             "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    timeout=15)
    r.encoding = "gbk"
    out = []
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].replace("var hq_str_", "").strip()
        payload = line.split("=", 1)[1].strip().strip('"').strip(';').strip()
        f = payload.split(",")
        if len(f) < 5 or key not in SINA_CODES:
            continue
        try:
            out.append({
                "name": SINA_CODES[key][0],
                "price": float(f[1]),
                "change": float(f[2]),
                "pct": float(f[3]),
                "time": f[4],
            })
        except (ValueError, IndexError):
            continue
    return out


def get_global_markets(force=False) -> list:
    """Return all global indices with quotes. Cached 60s."""
    if not force and _cache and time.time() - _cache["ts"] < _TTL:
        return _cache["data"]
    data = _tencent_quotes() + _sina_quotes()
    _cache["ts"] = time.time()
    _cache["data"] = data
    return data
