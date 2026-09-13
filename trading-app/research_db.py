"""券商研报数据库连接器。

数据源：东方财富研报中心 reportapi.eastmoney.com
  * qType=0 → 个股研报
  * qType=1 → 行业研报
  * qType=4 → 策略报告
返回：标题 / 机构 / 分析师 / 评级 / 日期 / 关联股票 / 行业
"""
import json
import re
import threading
import time

import requests

_S = requests.Session()
_S.trust_env = False
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://data.eastmoney.com/report/"}
_API = "https://reportapi.eastmoney.com/report/list"

_cache = {}
_lock = threading.Lock()


def _cached(key, fn, ttl=1800):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _fetch(qtype: int, code: str = "", industry: str = "", days: int = 90, limit: int = 20) -> list:
    from datetime import datetime, timedelta
    end = datetime.now()
    begin = end - timedelta(days=days)
    params = {
        "cb": "cb", "pageSize": str(max(1, min(50, limit))), "pageNo": "1",
        "beginTime": begin.strftime("%Y-%m-%d"), "endTime": end.strftime("%Y-%m-%d"),
        "qType": str(qtype),
        "industryCode": industry or "*", "industry": industry or "*",
        "rating": "*", "ratingChange": "*",
        "fields": "", "hits": "",
    }
    if code:
        params["code"] = str(code).zfill(6)
    try:
        r = _S.get(_API, params=params, headers=_H, timeout=15)
        txt = r.text
        s, e = txt.find("("), txt.rfind(")")
        data = json.loads(txt[s + 1:e]) if s >= 0 and e > s else json.loads(txt)
    except Exception:
        return []
    out = []
    for d in (data.get("data") or []):
        out.append({
            "title": (d.get("title") or "").strip(),
            "org": d.get("orgSName") or d.get("orgName") or "",
            "author": ", ".join(d.get("author") or []) if isinstance(d.get("author"), list) else (d.get("author") or ""),
            "rating": d.get("emRatingName") or d.get("sRatingName") or "",
            "date": (d.get("publishDate") or "")[:10],
            "stock": d.get("stockName") or "",
            "stock_code": d.get("stockCode") or "",
            "industry": d.get("indvInduName") or d.get("industryName") or "",
            "url": f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={d.get('infoCode')}"
                   if d.get("infoCode") else "",
            "summary": _clean(d.get("summary") or ""),
        })
    return out


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t or "")).strip()[:200]


def stock_reports(symbol: str, days: int = 180, limit: int = 15) -> list:
    sym = str(symbol).zfill(6)
    return _cached(f"stk|{sym}|{days}|{limit}", lambda: _fetch(0, code=sym, days=days, limit=limit), 3600)


def industry_reports(industry: str = "", days: int = 60, limit: int = 15) -> list:
    return _cached(f"ind|{industry}|{days}|{limit}",
                   lambda: _fetch(1, industry=industry if industry not in ("", "*") else "", days=days, limit=limit), 3600)


def strategy_reports(days: int = 30, limit: int = 10) -> list:
    return _cached(f"strat|{days}|{limit}", lambda: _fetch(4, days=days, limit=limit), 3600)


def ratings_summary(symbol: str, days: int = 180) -> dict:
    """评级分布统计。"""
    rs = stock_reports(symbol, days=days, limit=50)
    dist = {}
    for r in rs:
        k = r.get("rating") or "未评级"
        dist[k] = dist.get(k, 0) + 1
    orgs = {}
    for r in rs:
        orgs[r["org"]] = orgs.get(r["org"], 0) + 1
    return {"total": len(rs), "dist": dist,
            "top_orgs": sorted(orgs.items(), key=lambda x: -x[1])[:5],
            "latest": rs[:3]}


def llm_brief(symbol: str = "", industry: str = "") -> dict:
    """DeepSeek 汇总研报观点。"""
    import deepseek
    if not deepseek.available():
        return {"ok": False, "text": "", "error": "LLM 未配置"}
    stk = stock_reports(symbol, limit=12) if symbol else []
    ind = industry_reports(industry, limit=10) if industry else []
    if not stk and not ind:
        return {"ok": False, "text": "", "error": "近 90 天无相关研报"}
    lines = []
    if stk:
        lines.append("【个股研报】")
        lines += [f"- {r['date']} {r['org']}｜{r['rating'] or '未评级'}｜{r['title']}" for r in stk[:10]]
    if ind:
        lines.append("【行业研报】")
        lines += [f"- {r['date']} {r['org']}｜{r['title']}" for r in ind[:8]]
    sys_p = ("你是卖方研报解读助手。基于券商研报标题与评级，汇总市场一致预期与主要分歧。"
             "必须：① 只引用给出的研报，不得编造结论或目标价；② 明确指出这是券商观点而非事实；"
             "③ 给出不确定性；④ 禁止保本/稳赚表述；⑤ 结尾声明不构成投资建议。240 字内，分点。")
    user_p = (f"标的：{symbol or '未指定'}  行业：{industry or '未指定'}\n\n" + "\n".join(lines) +
              "\n\n请给出：1) 一致预期方向 2) 机构关注焦点 3) 分歧与风险点。")
    r = deepseek.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                      temperature=0.3, max_tokens=800, tag="report_brief", use_cache=False)
    return {"ok": r["ok"], "text": r["text"], "error": r["error"]}
