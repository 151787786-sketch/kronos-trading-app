"""Auto stock recommendation: full A-share market scan (leaderboard style).

Candidate pool comes from the Sina full-market gainers list (by change%),
then scored on momentum / technicals / fundamentals / optional Kronos
forecast. Also provides daily top finance headlines.
"""
import threading
import time

import indicators as ind
import market
import requests
from signals import indicator_signals

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 10 * 60

session = requests.Session()
session.trust_env = False
session.headers.update({"User-Agent": "Mozilla/5.0",
                        "Referer": "https://finance.sina.com.cn"})

GAINER_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodeData")
NEWS_URL = "https://feed.mix.sina.com.cn/api/roll/get"


def _get_gainers(page=1, num=60) -> list:
    """Fetch full-market A-share gainers (sorted by change % desc)."""
    r = session.get(GAINER_URL, params={
        "page": str(page), "num": str(num),
        "sort": "changepercent", "asc": "0", "node": "hs_a", "_s_r_a": "page",
    }, timeout=20)
    r.raise_for_status()
    data = r.json()
    out = []
    for it in data:
        symbol = it.get("code", "")
        if not symbol:
            continue
        # normalize to 6-digit code
        sym = symbol[-6:] if len(symbol) >= 6 else symbol
        try:
            out.append({
                "symbol": sym,
                "name": it.get("name", ""),
                "price": float(it.get("trade") or 0),
                "pct": float(it.get("changepercent") or 0),
                "change": float(it.get("pricechange") or 0),
                "volume": float(it.get("volume") or 0),
                "amount": float(it.get("amount") or 0),
                "turnover": float(it.get("turnoverratio") or 0),
                "pe": float(it.get("per") or 0) if it.get("per") else None,
                "pb": float(it.get("pb") or 0) if it.get("pb") else None,
                "mktcap": float(it.get("mktcap") or 0) * 1e4,   # 万元 -> 元
            })
        except (TypeError, ValueError):
            continue
    return out


def _volume_ratio(symbol: str) -> float:
    """Best-effort volume ratio via tencent quote."""
    try:
        q = market.fetch_realtime_one(symbol)
        return q.get("volume_ratio") or 0.0
    except Exception:
        return 0.0


def _score_momentum(it, vr) -> float:
    pct = it["pct"]
    s = max(-3, min(3, pct / 3.0))
    if pct > 0:
        if vr >= 3:
            s += 1.5
        elif vr >= 1.5:
            s += 0.8
    return s


def _score_technicals(sym) -> tuple:
    try:
        df = market.fetch_daily(sym, bars=250)
        dfe = ind.all_indicators(df)
        sig = indicator_signals(dfe).iloc[-1]
        last = dfe.iloc[-1]
        s, reasons = 0.0, []
        if last["ma5"] > last["ma20"] > last["ma60"]:
            s += 1.2; reasons.append("均线多头")
        elif last["ma5"] < last["ma20"] < last["ma60"]:
            s -= 1.2; reasons.append("均线空头")
        if last["dif"] > last["dea"] and last["macd"] > 0:
            s += 0.8; reasons.append("MACD金叉")
        elif last["dif"] < last["dea"]:
            s -= 0.5; reasons.append("MACD死叉")
        rsi = last["rsi"]
        if 40 <= rsi <= 65:
            s += 0.5; reasons.append(f"RSI强势({rsi:.0f})")
        elif rsi < 30:
            s += 0.6; reasons.append(f"RSI超卖({rsi:.0f})")
        elif rsi > 75:
            s -= 0.6; reasons.append(f"RSI超买({rsi:.0f})")
        if last["kdj_k"] > last["kdj_d"]:
            s += 0.4; reasons.append("KDJ金叉")
        if sig["signal"] == "BUY":
            s += 0.8; reasons.append(sig["reason"])
        elif sig["signal"] == "SELL":
            s -= 0.8; reasons.append(sig["reason"])
        return s, reasons
    except Exception:
        return 0.0, []


def _score_fundamentals(it) -> tuple:
    s, reasons = 0.0, []
    pe = it.get("pe")
    if pe and 0 < pe < 25:
        s += 0.6; reasons.append(f"PE合理({pe:.1f})")
    elif pe and pe > 60:
        s -= 0.4; reasons.append(f"PE偏高({pe:.0f})")
    pb = it.get("pb")
    if pb and 0 < pb < 1.5:
        s += 0.4; reasons.append(f"低PB({pb:.2f})")
    return s, reasons


def _score_forecast(sym) -> tuple:
    try:
        import kronos_service
        df = market.fetch_daily(sym, bars=600)
        fc = kronos_service.forecast(sym, df, lookback=400, pred_len=30,
                                     T=1.0, top_p=0.9, sample_count=1)
        chg = fc.get("change_pct", 0)
        s = 0.0
        if chg > 5: s += 1.5
        elif chg > 2: s += 0.8
        elif chg < -5: s -= 1.5
        elif chg < -2: s -= 0.8
        return s, [f"Kronos30日{chg:+.1f}%"]
    except Exception:
        return 0.0, ["Kronos不可用"]


def recommend(top_n: int = 5, use_forecast: bool = True, force: bool = False,
              pool_size: int = 40) -> dict:
    """Full-market leaderboard recommendation."""
    key = f"full:{top_n}:{use_forecast}:{pool_size}"
    with _cache_lock:
        if not force and key in _cache and time.time() - _cache[key]["ts"] < _CACHE_TTL:
            return _cache[key]["data"]

    gainers = _get_gainers(num=pool_size)
    if not gainers:
        return {"ok": False, "message": "无法获取全市场行情，请稍后重试", "recommendations": []}

    scored = []
    for it in gainers:
        sym = it["symbol"]
        vr = _volume_ratio(sym)
        ms = _score_momentum(it, vr)
        ts, treasons = _score_technicals(sym)
        fs, freasons = _score_fundamentals(it)
        ps = 0.0
        prs = []
        if use_forecast:
            ps, prs = _score_forecast(sym)

        total = ms + ts + fs + ps
        mrs = []
        if it["pct"] > 0 and vr >= 1.5:
            mrs.append(f"放量上涨(涨幅{it['pct']:+.1f}%量比{vr:.1f})")
        elif it["pct"] > 0:
            mrs.append(f"上涨(涨幅{it['pct']:+.1f}%)")
        reasons = mrs + treasons + freasons + prs

        scored.append({
            "symbol": sym,
            "name": it["name"],
            "price": it["price"],
            "pct": it["pct"],
            "turnover": it.get("turnover"),
            "pe": it.get("pe"),
            "pb": it.get("pb"),
            "mktcap": it.get("mktcap"),
            "volume_ratio": round(vr, 1),
            "score": round(total, 2),
            "breakdown": {
                "momentum": round(ms, 2), "technical": round(ts, 2),
                "fundamental": round(fs, 2), "forecast": round(ps, 2),
            },
            "reasons": reasons,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_n]

    for i, r in enumerate(top, 1):
        r["rank"] = i
        mv = r.get("mktcap")
        mv_s = f"市值{mv/1e8:.0f}亿" if mv and mv >= 1e8 else ""
        r["summary"] = (f"第{i}名 {r['name']}：现涨{r['pct']:+.1f}% 量比{r['volume_ratio']:.1f} {mv_s}，"
                        f"评分{r['score']:.1f}，理由：{'；'.join(r['reasons'][:3]) or '暂无'}")

    data = {"ok": True, "recommendations": top,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pool_size": len(gainers), "source": "全市场涨幅榜"}
    with _cache_lock:
        _cache[key] = {"ts": time.time(), "data": data}
    return data


# ---------------------------------------------------------------------------
# 每日重要新闻
# ---------------------------------------------------------------------------

_NEWS_CACHE = {}


def daily_news(limit: int = 5, force: bool = False) -> list:
    """Top daily finance headlines from Sina."""
    global _NEWS_CACHE
    if not force and _NEWS_CACHE and time.time() - _NEWS_CACHE["ts"] < 60 * 30:
        return _NEWS_CACHE["data"]
    try:
        r = session.get(NEWS_URL, params={
            "pageid": "153", "lid": "2516", "num": str(limit + 3),
            "page": "1", "r": "0.5",
        }, timeout=20)
        j = r.json()
        items = ((j.get("result") or {}).get("data") or [])
    except Exception:
        return []

    out = []
    seen = set()
    for it in items:
        title = (it.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        ts = it.get("ctime") or it.get("intime") or ""
        out.append({
            "title": title,
            "media": it.get("media_name") or it.get("source") or "",
            "time": str(ts)[:16] if ts else "",
            "url": it.get("url") or "",
        })
        if len(out) >= limit:
            break
    _NEWS_CACHE = {"ts": time.time(), "data": out}
    return out
