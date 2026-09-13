"""舆情情感分析连接器。

两级方案：
  1) 词典规则引擎（离线可跑、确定性、零成本）——金融情感词典 + 否定词 + 程度副词 + 权重
  2) DeepSeek 语义打分（配置 API key 后启用）——对标题做 -1~+1 情感极性 + 事件类型判定

数据源：
  * 东方财富个股新闻 search-api-web.eastmoney.com
  * 东方财富公司公告 np-anotice-stock.eastmoney.com（fundamentals.get_news 复用）
  * 新浪财经要闻 feed.mix.sina.com.cn（市场级舆情）
"""
import re
import threading
import time

import requests

_S = requests.Session()
_S.trust_env = False
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://so.eastmoney.com/"}

_EM_NEWS = "https://search-api-web.eastmoney.com/search/jsonp"

_cache = {}
_lock = threading.Lock()

# ------------------------------------------------------------- 情感词典

POS = {
    "涨停": 3.0, "大涨": 2.5, "飙升": 2.5, "暴涨": 3.0, "新高": 2.5, "创新高": 2.5,
    "增长": 1.5, "增持": 2.0, "回购": 2.0, "中标": 2.0, "签约": 1.5, "订单": 1.5,
    "超预期": 2.5, "扭亏": 2.5, "盈利": 1.5, "净利增": 2.0, "营收增": 1.5, "放量": 1.0,
    "利好": 2.0, "突破": 1.8, "上调": 2.0, "买入": 2.0, "推荐": 1.5, "看好": 1.8,
    "提价": 1.5, "涨价": 1.5, "扩产": 1.3, "获批": 1.8, "获批上市": 2.0, "量产": 1.5,
    "合作": 1.2, "并购": 1.5, "重组": 1.5, "分红": 1.5, "派息": 1.3, "股权激励": 1.5,
    "复苏": 1.5, "回暖": 1.5, "景气": 1.5, "领先": 1.0, "龙头": 1.0, "国产替代": 1.5,
    "政策支持": 2.0, "补贴": 1.5, "降准": 2.0, "降息": 2.0, "减税": 1.5,
    "涨": 0.8, "升": 0.6, "强": 0.6, "优": 0.8, "增": 0.6, "赢": 0.8,
}

NEG = {
    "跌停": -3.0, "大跌": -2.5, "暴跌": -3.0, "重挫": -2.5, "新低": -2.5, "创新低": -2.5,
    "下降": -1.5, "减持": -2.0, "亏损": -2.5, "预亏": -2.5, "商誉减值": -3.0, "计提": -1.8,
    "违规": -2.5, "处罚": -2.5, "罚款": -2.0, "立案": -3.0, "调查": -2.2, "问询": -1.8,
    "退市": -3.5, "ST": -2.5, "*ST": -3.0, "暂停上市": -3.0, "诉讼": -1.8, "仲裁": -1.5,
    "质押": -1.5, "爆仓": -3.0, "违约": -3.0, "逾期": -2.5, "债务": -1.2, "裁员": -1.5,
    "下调": -2.0, "卖出": -2.0, "利空": -2.0, "不及预期": -2.5, "低于预期": -2.5,
    "解禁": -1.5, "减仓": -1.5, "风险": -1.0, "警示": -1.8, "监管": -1.0, "停产": -2.2,
    "召回": -2.0, "事故": -2.0, "火灾": -2.0, "失信": -2.5, "造假": -3.5, "财务造假": -3.5,
    "跌": -0.8, "降": -0.6, "弱": -0.6, "差": -0.8, "减": -0.6, "亏": -1.0,
}

NEGATION = ["不", "未", "没有", "无", "非", "难以", "尚未", "否认", "澄清"]
DEGREE = {"大幅": 1.6, "显著": 1.5, "明显": 1.3, "略微": 0.6, "小幅": 0.7, "略": 0.6,
          "超": 1.4, "创": 1.3, "持续": 1.2, "连续": 1.2}


def lexicon_score(text: str) -> dict:
    """词典情感打分，返回 score(-1~1) 与命中的正负词。"""
    t = (text or "")[:300]
    if not t:
        return {"score": 0.0, "pos": [], "neg": []}
    total = 0.0
    hits_pos, hits_neg = [], []
    for kw, w in list(POS.items()) + list(NEG.items()):
        if kw not in t:
            continue
        cnt = t.count(kw)
        mult = 1.0
        for d, dm in DEGREE.items():
            if d in t:
                mult = max(mult, dm)
        sign = 1.0
        for ng in NEGATION:
            idx = t.find(kw)
            if idx > 0 and ng in t[max(0, idx - 4):idx]:
                sign = -1.0
                break
        v = w * cnt * mult * sign
        total += v
        (hits_pos if v > 0 else hits_neg).append(kw)
    # 归一化到 -1 ~ 1
    score = max(-1.0, min(1.0, total / 6.0))
    return {"score": round(score, 3), "pos": hits_pos[:6], "neg": hits_neg[:6]}


# ------------------------------------------------------------- 新闻抓取

def stock_news(symbol: str, limit: int = 10, keyword: str = "") -> list:
    """个股新闻（腾讯财经，type=2 新闻 / type=1 研报），另叠加东财搜索兜底。"""
    sym = str(symbol).zfill(6)
    prefix = "sh" if sym[0] in "65" else ("bj" if sym[0] in "489" else "sz")
    rows = []
    try:
        r = _S.get("https://proxy.finance.qq.com/ifzqgtimg/appstock/news/info/search",
                   params={"symbol": prefix + sym, "page": "1", "n": str(max(4, limit)), "type": "2"},
                   headers={**_H, "Referer": "https://gu.qq.com/"}, timeout=12)
        data = (r.json().get("data") or {})
        for it in (data.get("data") if isinstance(data, dict) else []) or []:
            rows.append({
                "title": (it.get("title") or "").strip(),
                "date": (it.get("time") or "")[:16],
                "source": it.get("src") or "腾讯财经",
                "url": it.get("url") or "",
                "content": (it.get("summary") or "")[:120],
            })
    except Exception:
        pass
    if not rows:
        rows = _em_search_news(symbol, keyword, limit)
    return rows[:limit]


def _em_search_news(symbol: str, keyword: str = "", limit: int = 10) -> list:
    """东财搜索兜底（命中率较低，仅在腾讯源不可用时使用）。"""
    import json as _json
    sym = str(symbol).zfill(6)
    kw = keyword
    if not kw:
        try:
            import market
            kw = (market.resolve_symbol(sym) or {}).get("name") or sym
        except Exception:
            kw = sym
    param = {
        "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": limit,
                                       "preTag": "", "postTag": ""}},
    }
    try:
        r = _S.get(_EM_NEWS, params={"cb": "cb", "param": _json.dumps(param, ensure_ascii=False)},
                   headers=_H, timeout=12)
        txt = r.text
        data = _json.loads(txt[txt.find("(") + 1: txt.rfind(")")])
        items = (((data.get("result") or {}).get("cmsArticleWebOld")) or [])
        out = []
        for it in items:
            out.append({"title": (it.get("title") or "").replace("<em>", "").replace("</em>", ""),
                        "date": (it.get("date") or "")[:16],
                        "source": it.get("mediaName") or "",
                        "url": it.get("url") or "",
                        "content": (it.get("content") or "")[:120]})
        return out
    except Exception:
        return []


def research_news(symbol: str, limit: int = 8) -> list:
    """个股研报类资讯（腾讯 type=1）。"""
    sym = str(symbol).zfill(6)
    prefix = "sh" if sym[0] in "65" else ("bj" if sym[0] in "489" else "sz")
    try:
        r = _S.get("https://proxy.finance.qq.com/ifzqgtimg/appstock/news/info/search",
                   params={"symbol": prefix + sym, "page": "1", "n": str(limit), "type": "1"},
                   headers={**_H, "Referer": "https://gu.qq.com/"}, timeout=12)
        data = (r.json().get("data") or {})
        out = []
        for it in (data.get("data") if isinstance(data, dict) else []) or []:
            out.append({"title": (it.get("title") or "").strip(),
                        "date": (it.get("time") or "")[:10],
                        "source": it.get("src") or "", "url": it.get("url") or ""})
        return out
    except Exception:
        return []


def market_news(limit: int = 12) -> list:
    """市场级财经要闻（新浪）。"""
    try:
        r = _S.get("https://feed.mix.sina.com.cn/api/roll/get",
                   params={"pageid": "153", "lid": "2516", "num": str(limit), "page": "1"},
                   headers=_H, timeout=12)
        data = ((r.json().get("result") or {}).get("data")) or []
        return [{"title": d.get("title"), "date": d.get("ctime"), "url": d.get("url")} for d in data]
    except Exception:
        return []


# ------------------------------------------------------------- LLM 语义打分

_SENT_SYS = ("你是金融舆情分析师。对每条新闻标题给出情感极性与事件类型。"
             "严格输出 JSON 数组，每个元素："
             '{"i":序号,"score":-1到1的小数,"event":"事件类型","why":"8字以内依据"}。'
             "score：-1 极负面，0 中性，1 极正面。只输出 JSON，不要任何解释。")


def llm_score(titles: list) -> list:
    """用 DeepSeek 对标题批量打分。失败返回 []（上层回退词典）。"""
    import deepseek
    if not deepseek.available() or not titles:
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles[:20]))
    res = deepseek.chat_json_list(_SENT_SYS, f"标题列表：\n{numbered}", temperature=0.1,
                                  max_tokens=1200, tag="sentiment", use_cache=True)
    if not res:
        return []
    out = []
    for it in res:
        try:
            out.append({"i": int(it.get("i")) - 1,
                        "score": max(-1.0, min(1.0, float(it.get("score")))),
                        "event": str(it.get("event") or "")[:12],
                        "why": str(it.get("why") or "")[:20]})
        except Exception:
            continue
    return out


# ------------------------------------------------------------- 汇总

def analyze(symbol: str, use_llm: bool = True, limit: int = 10) -> dict:
    """个股舆情情感分析汇总。"""
    news = stock_news(symbol, limit=limit)
    anns = []
    try:
        import fundamentals
        anns = fundamentals.get_news(symbol, limit=6) or []
    except Exception:
        anns = []

    rows = []
    for n in news:
        ls = lexicon_score(n["title"])
        rows.append({**n, "kind": "新闻", "lex": ls["score"], "pos": ls["pos"], "neg": ls["neg"],
                     "llm": None, "event": ""})
    for a in anns:
        title = a.get("title") if isinstance(a, dict) else str(a)
        ls = lexicon_score(title)
        rows.append({"title": title, "date": (a.get("date") if isinstance(a, dict) else "") or "",
                     "source": "公司公告", "url": (a.get("url") if isinstance(a, dict) else "") or "",
                     "kind": "公告", "lex": ls["score"], "pos": ls["pos"], "neg": ls["neg"],
                     "llm": None, "event": ""})

    engine = "lexicon"
    if use_llm and rows:
        scored = llm_score([r["title"] for r in rows])
        if scored:
            engine = "deepseek"
            for s in scored:
                if 0 <= s["i"] < len(rows):
                    rows[s["i"]]["llm"] = round(s["score"], 3)
                    rows[s["i"]]["event"] = s["event"]
                    rows[s["i"]]["why"] = s["why"]

    for r in rows:
        r["final"] = r["llm"] if r["llm"] is not None else r["lex"]
        r["label"] = _label(r["final"])

    vals = [r["final"] for r in rows] or [0.0]
    avg = round(sum(vals) / len(vals), 3)
    pos = sum(1 for v in vals if v > 0.15)
    neg = sum(1 for v in vals if v < -0.15)
    neu = len(vals) - pos - neg
    return {
        "symbol": str(symbol).zfill(6),
        "engine": engine,
        "count": len(rows),
        "avg": avg,
        "label": _label(avg),
        "pos": pos, "neg": neg, "neutral": neu,
        "risk_level": "高" if avg < -0.35 or neg >= max(3, len(vals) * 0.4)
                      else ("中" if avg < -0.12 else "低"),
        "items": sorted(rows, key=lambda x: x["final"])[:limit],
    }


def _label(v: float) -> str:
    if v >= 0.45:
        return "强正面"
    if v >= 0.15:
        return "偏正面"
    if v <= -0.45:
        return "强负面"
    if v <= -0.15:
        return "偏负面"
    return "中性"


def llm_brief(result: dict) -> dict:
    """DeepSeek 解读舆情。"""
    import deepseek
    if not deepseek.available():
        return {"ok": False, "text": "", "error": "LLM 未配置"}
    lines = [f"- [{r['label']}] {r['title']}（{r['kind']}{'·' + r['event'] if r.get('event') else ''}）"
             for r in (result.get("items") or [])[:10]]
    sys_p = ("你是舆情分析师。基于真实新闻/公告标题，判断该标的当前舆情倾向与潜在风险。"
             "必须：① 只引用给定标题，不得编造；② 指出不确定性；③ 禁止保本/稳赚表述；"
             "④ 结尾声明不构成投资建议。180 字内。")
    user_p = (f"标的 {result.get('symbol')}，舆情均值 {result.get('avg')}（{result.get('label')}），"
              f"正面 {result.get('pos')} / 负面 {result.get('neg')} / 中性 {result.get('neutral')}。\n"
              f"标题：\n" + "\n".join(lines) + "\n\n请给出：1) 舆情倾向 2) 需重点关注的负面事件 3) 操作提示。")
    r = deepseek.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                      temperature=0.3, max_tokens=600, tag="sentiment_brief", use_cache=False)
    return {"ok": r["ok"], "text": r["text"], "error": r["error"]}
