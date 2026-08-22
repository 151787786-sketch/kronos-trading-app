"""Fundamental analysis module: company info, valuation, financials, news.

All requests bypass the Windows system proxy (Clash etc.) which blocks CN
finance sites. Data comes from eastmoney public endpoints.
"""
import json
import os
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

session = requests.Session()
session.trust_env = False
session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})

_cache = {}
_CACHE_TTL = 6 * 3600  # 6 hours


def _secu_code(symbol: str) -> str:
    # 600585 -> 600585.SH ; 000001 -> 000001.SZ
    if symbol.startswith(("6", "9", "5")):
        return f"{symbol}.SH"
    return f"{symbol}.SZ"


def _secid(symbol: str) -> str:
    # 1 = SH, 0 = SZ
    prefix = "1" if symbol.startswith(("6", "9", "5")) else "0"
    return f"{prefix}.{symbol}"


def _get(url, params=None, retries=4):
    key = (url, json.dumps(params or {}, sort_keys=True))
    if key in _cache:
        hit = _cache[key]
        if time.time() - hit["ts"] < _CACHE_TTL:
            return hit["data"]
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            _cache[key] = {"ts": time.time(), "data": data}
            return data
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def _num(v):
    if v in (None, "-", ""):
        return None
    try:
        return float(v)
    except Exception:
        return None


def get_valuation(symbol: str) -> dict:
    """Realtime valuation via Tencent quote (stable in this network):
    price, market caps, turnover. PE/PB are derived from financial data."""
    fs = f"{'sh' if symbol.startswith(('6','9','5')) else 'sz'}{symbol}"
    r = session.get("https://qt.gtimg.cn/q=" + fs, timeout=20)
    r.raise_for_status()
    r.encoding = "gbk"
    f = r.text.split("~")
    if len(f) < 50:
        raise RuntimeError(f"腾讯行情返回异常: {r.text[:100]}")

    price = _num(f[3])
    float_mv = _num(f[44])   # 流通市值(亿)
    total_mv = _num(f[45])   # 总市值(亿)
    turnover = _num(f[38])   # 换手率(%)
    amplitude = _num(f[43])  # 振幅(%)

    # PE/PB from financials: use the most recent ANNUAL report EPS as a TTM
    # proxy (quarterly EPS is cumulative within the year, not annualized).
    eps = bps = None
    try:
        fin = get_financials(symbol, periods=6)
        for x in fin:
            if x.get("report_date", "").endswith("12-31") and x.get("eps"):
                eps = x["eps"]   # latest annual EPS
                break
        if eps is None and fin and fin[0].get("eps"):
            eps = fin[0]["eps"]
        if fin and fin[0].get("bps"):
            bps = fin[0]["bps"]
    except Exception:
        pass

    pe_ttm = round(price / eps, 2) if price and eps else None
    pb = round(price / bps, 2) if price and bps else None

    return {
        "name": f[1],
        "symbol": symbol,
        "price": price,
        "total_mv": total_mv * 1e8 if total_mv else None,   # 元
        "float_mv": float_mv * 1e8 if float_mv else None,
        "pe_ttm": pe_ttm,
        "eps": eps,
        "pb": pb,
        "bvps": bps,
        "turnover": turnover,
        "amplitude": amplitude,
    }


def get_financials(symbol: str, periods: int = 4) -> list:
    """Recent quarterly financial summaries (revenue, net profit, ROE, margins)."""
    j = _get("https://datacenter-web.eastmoney.com/api/data/v1/get", {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": "ALL",
        "filter": f'(SECUCODE="{_secu_code(symbol)}")',
        "pageNumber": "1", "pageSize": str(periods),
        "sortTypes": "-1", "sortColumns": "REPORT_DATE",
    })
    rows = (j.get("result") or {}).get("data") or []
    out = []
    for x in rows:
        out.append({
            "report_date": (x.get("REPORT_DATE") or "")[:10],
            "revenue": _num(x.get("TOTAL_OPERATE_INCOME")),
            "revenue_yoy": _num(x.get("DJD_TOI_YOY") or x.get("OI_YOYRATIO_PK")),
            "net_profit": _num(x.get("PARENTNETPROFIT")),
            "net_profit_yoy": _num(x.get("PARENTNETPROFITTZ")),
            "roe": _num(x.get("ROEJQ")),
            "gross_margin": _num(x.get("XSMLL")),
            "net_margin": _num(x.get("XSJLL")),
            "eps": _num(x.get("EPSJB")),
            "bps": _num(x.get("BPS")),
        })
    return out


def get_company_info(symbol: str) -> dict:
    """Company profile from eastmoney F10 (industry, main business, etc.)."""
    j = _get("https://datacenter-web.eastmoney.com/api/data/v1/get", {
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": "ALL",
        "filter": f'(SECUCODE="{_secu_code(symbol)}")',
        "pageNumber": "1", "pageSize": "1",
    })
    rows = (j.get("result") or {}).get("data") or []
    if not rows:
        return {}
    x = rows[0]
    return {
        "name": x.get("ORG_NAME") or x.get("SECURITY_NAME_ABBR"),
        "industry": x.get("INDUSTRY"),
        "province": x.get("PROVINCE"),
        "reg_capital": x.get("REG_CAPITAL"),
        "employees": x.get("PEOPLE"),
        "main_business": (x.get("MAIN_BUSINESS") or "")[:500],
        "business_scope": (x.get("BUSINESS_SCOPE") or "")[:300],
        "introduction": (x.get("ORG_PROFILE") or "")[:800],
    }


def get_news(symbol: str, limit: int = 8) -> list:
    """Recent company announcements from eastmoney (precisely matched to symbol)."""
    try:
        j = _get("https://np-anotice-stock.eastmoney.com/api/security/ann", {
            "sr": "-1", "page_size": str(limit), "page_index": "1",
            "ann_type": "A", "client_source": "web", "page_number": "1",
            "stock_list": symbol, "f_node": "0", "s_node": "0",
        })
        items = (((j.get("data") or {}).get("list")) or [])
    except Exception:
        return []

    out = []
    for a in items[:limit]:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        cols = {c.get("column_name", "") for c in (a.get("columns") or [])}
        out.append({
            "date": (a.get("notice_date") or a.get("display_time") or "")[:10],
            "title": title,
            "media": "公司公告 · " + ("/".join(sorted(cols)) if cols else "其他"),
            "url": f"https://data.eastmoney.com/notices/detail/{symbol}/{a.get('art_code','')}.html",
        })
    return out


def analyze(symbol: str) -> dict:
    """Assemble a complete fundamental snapshot with simple judgments."""
    val = get_valuation(symbol)
    fin = get_financials(symbol, periods=4)
    info = get_company_info(symbol)
    news = get_news(symbol)

    # simple qualitative judgments based on the numbers
    judgments = []
    if fin:
        latest = fin[0]
        if latest.get("revenue_yoy") is not None:
            judgments.append(f"最新营收同比 {latest['revenue_yoy']:+.1f}%")
        if latest.get("net_profit_yoy") is not None:
            judgments.append(f"归母净利同比 {latest['net_profit_yoy']:+.1f}%")
        if latest.get("roe") is not None:
            judgments.append(f"ROE {latest['roe']:.1f}%")
        if latest.get("gross_margin") is not None:
            judgments.append(f"毛利率 {latest['gross_margin']:.1f}%")
        if (len(fin) >= 2 and fin[0].get("net_profit_yoy") is not None
                and fin[1].get("net_profit_yoy") is not None):
            trend = fin[0]["net_profit_yoy"] - fin[1]["net_profit_yoy"]
            judgments.append("净利同比增速改善" if trend > 0
                             else "净利同比增速放缓" if trend < 0 else "净利同比持平")
    if val.get("pe_ttm") and val["pe_ttm"] > 0:
        judgments.append(f"PE(TTM) {val['pe_ttm']:.1f}")
    if val.get("pb") and val["pb"] > 0:
        judgments.append(f"PB {val['pb']:.2f}")
    if val.get("total_mv"):
        mv = val["total_mv"]
        if mv >= 1e12:
            judgments.append(f"总市值 {mv/1e12:.2f} 万亿（超大盘）")
        elif mv >= 5e10:
            judgments.append(f"总市值 {mv/1e8:.0f} 亿（大盘）")
        elif mv >= 1e10:
            judgments.append(f"总市值 {mv/1e8:.0f} 亿（中盘）")
        else:
            judgments.append(f"总市值 {mv/1e8:.1f} 亿（小盘）")

    return {
        "symbol": symbol,
        "valuation": val,
        "financials": fin,
        "company": info,
        "news": news,
        "judgments": judgments,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
