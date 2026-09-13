"""行业景气度连接器。

数据源：
  * 腾讯行业板块排行 proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy
    → 行业涨跌幅、5/20/60日涨跌幅、换手率、量比、主力净流入、总市值、涨跌家数、领涨股
  * 东方财富 F10 RPT_F10_BASIC_ORGINFO → 个股所属行业（EM2016 三级 / 证监会行业）
"""
import threading
import time

import requests

_S = requests.Session()
_S.trust_env = False
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://gu.qq.com/"}
_RANK = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_cache = {}
_lock = threading.Lock()


def _cached(key, fn, ttl):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _f(v, d=None):
    try:
        return float(v)
    except Exception:
        return d


# ------------------------------------------------------------- 行业板块

def boards(limit: int = 200) -> list:
    """全部行业板块实时数据。"""
    def _f_():
        try:
            r = _S.get(_RANK, params={"board_type": "hy", "sort_type": "price",
                                      "direct": "down", "offset": "0", "count": str(limit)},
                       headers=_H, timeout=15)
            j = r.json()
            raw = ((j.get("data") or {}).get("rank_list")) or []
        except Exception:
            raw = []
        out = []
        for b in raw:
            zgb = b.get("zgb") or ""
            up = down = None
            if "/" in zgb:
                try:
                    up, down = [int(x) for x in zgb.split("/")[:2]]
                except Exception:
                    pass
            lzg = b.get("lzg") or {}
            out.append({
                "code": b.get("code"), "name": b.get("name"),
                "pct": _f(b.get("zdf"), 0.0),
                "pct5": _f(b.get("zdf_d5")), "pct20": _f(b.get("zdf_d20")),
                "pct60": _f(b.get("zdf_d60")), "pct_year": _f(b.get("zdf_y")),
                "turnover_rate": _f(b.get("hsl")), "volume_ratio": _f(b.get("lb")),
                "main_inflow": _f(b.get("zljlr")), "main_inflow5": _f(b.get("zljlr_d5")),
                "main_inflow20": _f(b.get("zljlr_d20")),
                "market_cap": _f(b.get("zsz")), "float_cap": _f(b.get("ltsz")),
                "up_count": up, "down_count": down,
                "leader": {"name": lzg.get("name"), "code": lzg.get("code"),
                           "pct": _f(lzg.get("zdf"))},
            })
        return out
    return _cached("boards", _f_, 120)


def _prosperity(b: dict) -> float:
    """景气度打分 0~100：短中期动量 + 资金 + 人气。"""
    s = 50.0
    s += max(-18, min(18, (b.get("pct") or 0) * 3.0))
    s += max(-10, min(10, (b.get("pct5") or 0) * 1.2))
    s += max(-10, min(10, (b.get("pct20") or 0) * 0.6))
    s += max(-6, min(6, (b.get("pct60") or 0) * 0.25))
    mi = b.get("main_inflow")
    if mi is not None:
        s += max(-8, min(8, mi / 100000.0))
    vr = b.get("volume_ratio")
    if vr:
        s += max(-4, min(4, (vr - 1.0) * 4))
    to = b.get("turnover_rate")
    if to:
        s += max(-3, min(3, (to - 2.0) * 0.8))
    return round(max(0.0, min(100.0, s)), 1)


def ranking(limit: int = 30) -> list:
    """按景气度排序的行业榜。"""
    bs = boards()
    for b in bs:
        b["prosperity"] = _prosperity(b)
    return sorted(bs, key=lambda x: -x["prosperity"])[:limit]


# ------------------------------------------------------------- 个股 → 行业

_IND_CACHE = {}


def stock_industry(symbol: str) -> dict:
    """个股所属行业（东财 F10），带内存缓存。"""
    symbol = str(symbol).zfill(6)
    if symbol in _IND_CACHE:
        return _IND_CACHE[symbol]
    out = {"symbol": symbol, "em": "", "csrc": "", "level1": ""}
    try:
        suffix = "SH" if symbol[0] in "65" else ("BJ" if symbol[0] in "489" else "SZ")
        r = _S.get(_DC, params={"reportName": "RPT_F10_BASIC_ORGINFO", "columns": "ALL",
                                "filter": f'(SECUCODE="{symbol}.{suffix}")'},
                   headers={**_H, "Referer": "https://emweb.securities.eastmoney.com/"}, timeout=12)
        data = ((r.json().get("result") or {}).get("data")) or []
        if data:
            em = data[0].get("EM2016") or ""
            out["em"] = em
            out["csrc"] = data[0].get("INDUSTRYCSRC1") or ""
            out["level1"] = em.split("-")[0] if em else ""
    except Exception:
        pass
    _IND_CACHE[symbol] = out
    return out


def _norm(name: str) -> str:
    return (name or "").replace(" ", "").replace("行业", "").strip()


def industry_for(symbol: str) -> dict:
    """个股行业景气度：行业信息 + 板块数据 + 排名。"""
    ind = stock_industry(symbol)
    target = _norm(ind.get("level1"))
    if not target:
        return {"industry": "", "board": None, "rank": None, "total": 0,
                "prosperity": None, "note": "未取到行业分类"}
    rk = ranking(limit=500)
    board = None
    for i, b in enumerate(rk):
        n = _norm(b["name"])
        if n and (n == target or n in target or target in n):
            board = b
            board["rank"] = i + 1
            board["total"] = len(rk)
            break
    if not board:
        return {"industry": ind.get("level1"), "board": None, "rank": None, "total": len(rk),
                "prosperity": None, "note": "未匹配到对应行业板块"}
    return {"industry": board["name"], "board": board, "rank": board["rank"],
            "total": board["total"], "prosperity": board["prosperity"], "note": ""}


# ------------------------------------------------------------- LLM 解读

def llm_brief(symbol: str = "", industry: str = "") -> dict:
    """DeepSeek 解读行业景气度与个股所处位置。"""
    import deepseek
    if not deepseek.available():
        return {"ok": False, "text": "", "error": "LLM 未配置"}
    info = industry_for(symbol) if symbol else {}
    b = info.get("board") or {}
    if not b:
        top = ranking(8)
        lines = [f"- {x['name']}: 今日{x['pct']:+.2f}%，5日{x['pct5']:+.2f}%，景气度{x['prosperity']}"
                 for x in top]
        ctx = "【当前全市场景气度前 8 行业】\n" + "\n".join(lines)
        head = "未指定个股"
    else:
        ctx = (f"【个股 {symbol} 所属行业】{info['industry']}（景气度排名 {info['rank']}/{info['total']}）\n"
               f"- 今日 {b.get('pct'):+.2f}%  5日 {b.get('pct5'):+.2f}%  20日 {b.get('pct20'):+.2f}%  60日 {b.get('pct60'):+.2f}%\n"
               f"- 换手 {b.get('turnover_rate')}%  量比 {b.get('volume_ratio')}  主力净流入 {b.get('main_inflow')} 万\n"
               f"- 上涨 {b.get('up_count')} 家 / 下跌 {b.get('down_count')} 家  领涨股 {b.get('leader', {}).get('name')}\n"
               f"- 景气度评分 {b.get('prosperity')}/100")
        head = f"个股 {symbol}"
    sys_p = ("你是行业分析师。基于真实行业板块数据分析行业景气度与产业链位置。"
             "必须：① 只引用提供的数据；② 给出假设与不确定性；③ 禁止保本/稳赚/必涨等表述；"
             "④ 结尾声明不构成投资建议。220 字以内，分点。")
    user_p = f"{ctx}\n\n【分析对象】{head}\n【可选行业】{industry or info.get('industry') or '未指定'}\n\n请给出：1) 行业景气度判断 2) 驱动因素与持续性 3) 主要风险。"
    r = deepseek.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                      temperature=0.3, max_tokens=700, tag="industry_brief", use_cache=False)
    return {"ok": r["ok"], "text": r["text"], "error": r["error"], "info": info}
