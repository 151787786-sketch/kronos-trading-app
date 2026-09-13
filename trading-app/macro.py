"""宏观数据连接器：通胀（CPI/PPI）、利率（资金价格）、货币政策（存准率）、经济（GDP/PMI）。

数据源：
  * 东方财富数据中心 datacenter-web.eastmoney.com（CPI/PPI/GDP/PMI/存款准备金率）
  * 腾讯行情 qt.gtimg.cn（GC001/GC007 国债逆回购＝交易所资金利率，实时）
全部直连、绕过系统代理（session.trust_env = False）。
"""
import threading
import time

import requests

_S = requests.Session()
_S.trust_env = False
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://data.eastmoney.com/"}
_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_cache = {}
_lock = threading.Lock()
TTL = 3600  # 宏观数据更新频率低，缓存 1 小时


def _cached(key, fn, ttl=TTL):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _report(report_name: str, size: int = 6, sort: str = "REPORT_DATE") -> list:
    try:
        r = _S.get(_DC, params={"reportName": report_name, "columns": "ALL",
                                "pageSize": str(size), "sortColumns": sort,
                                "sortTypes": "-1"}, headers=_H, timeout=15)
        j = r.json()
        return ((j.get("result") or {}).get("data")) or []
    except Exception:
        return []


# ------------------------------------------------------------------ 通胀

def inflation() -> dict:
    def _f():
        cpi = _report("RPT_ECONOMY_CPI", 6)
        ppi = _report("RPT_ECONOMY_PPI", 6)
        out = {"cpi": [], "ppi": []}
        for row in cpi:
            out["cpi"].append({
                "time": row.get("TIME"),
                "yoy": row.get("NATIONAL_SAME"),
                "mom": row.get("NATIONAL_SEQUENTIAL"),
                "accumulate": row.get("NATIONAL_ACCUMULATE"),
            })
        for row in ppi:
            out["ppi"].append({
                "time": row.get("TIME"),
                "yoy": row.get("BASE_SAME"),
                "accumulate": row.get("BASE_ACCUMULATE"),
            })
        return out
    return _cached("inflation", _f)


# ------------------------------------------------------------------ 货币/经济

def economy() -> dict:
    def _f():
        pmi = _report("RPT_ECONOMY_PMI", 6)
        gdp = _report("RPT_ECONOMY_GDP", 6)
        rr = _report("RPT_ECONOMY_DEPOSIT_RESERVE", 6, sort="PUBLISH_DATE")
        return {
            "pmi": [{"time": r.get("TIME"), "manufacturing": r.get("MAKE_INDEX"),
                     "non_manufacturing": r.get("NMAKE_INDEX")} for r in pmi],
            "gdp": [{"time": r.get("TIME"), "total": r.get("DOMESTICL_PRODUCT_BASE"),
                     "yoy": r.get("SUM_SAME"), "first": r.get("FIRST_PRODUCT_BASE"),
                     "second": r.get("SECOND_PRODUCT_BASE"), "third": r.get("THIRD_PRODUCT_BASE")}
                    for r in gdp],
            "reserve": [{"date": (r.get("PUBLISH_DATE") or "")[:10],
                         "big_bank": r.get("INTEREST_RATE_BB"), "small_bank": r.get("INTEREST_RATE_BA"),
                         "change": r.get("CHANGE_RATE_B")} for r in rr],
        }
    return _cached("economy", _f)


# ------------------------------------------------------------------ 利率

def _tx_quote(codes):
    """腾讯行情批量取价。返回 {code: fields}"""
    out = {}
    try:
        r = _S.get("https://qt.gtimg.cn/q=" + ",".join(codes), headers=_H, timeout=10)
        r.encoding = "gbk"
        for line in r.text.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            name, val = line.split("=", 1)
            code = name.replace("v_", "").strip()
            f = val.strip().strip('"').split("~")
            if len(f) > 5:
                out[code] = f
    except Exception:
        pass
    return out


def rates() -> dict:
    """资金利率：国债逆回购 GC001/GC007（交易所实时）+ 国债现券。"""
    def _f():
        q = _tx_quote(["sh204001", "sh204007", "sh204014", "sh019547", "sh019693"])
        rows = []

        def add(code, label, unit="%"):
            f = q.get(code)
            if not f:
                return
            try:
                price = float(f[3])
                prev = float(f[4])
            except Exception:
                return
            rows.append({"label": label, "code": code, "value": price,
                         "prev": prev, "change": round(price - prev, 4), "unit": unit})

        add("sh204001", "GC001（1天逆回购）")
        add("sh204007", "GC007（7天逆回购）")
        add("sh204014", "GC014（14天逆回购）")
        # 国债现券用净价，作为债市温度参考
        for code, label in (("sh019547", "16国债19（10年国债）"), ("sh019693", "19国债08")):
            f = q.get(code)
            if f:
                try:
                    rows.append({"label": label, "code": code, "value": float(f[3]),
                                 "prev": float(f[4]),
                                 "change": round(float(f[3]) - float(f[4]), 3), "unit": "元"})
                except Exception:
                    pass
        return {"rows": rows}
    return _cached("rates", _f, ttl=300)


# ------------------------------------------------------------------ 汇总

def snapshot() -> dict:
    """宏观快照：最新值 + 同比变化 + 方向判断。"""
    inf = inflation()
    eco = economy()
    rt = rates()

    def latest(seq):
        return seq[0] if seq else {}

    cpi = latest(inf.get("cpi") or [])
    ppi = latest(inf.get("ppi") or [])
    pmi = latest(eco.get("pmi") or [])
    gdp = latest(eco.get("gdp") or [])
    rr = latest(eco.get("reserve") or [])

    cards = []
    if cpi:
        cards.append({"name": "CPI 同比", "value": f"{cpi.get('yoy')}%", "sub": cpi.get("time"),
                      "tone": "neg" if (cpi.get("yoy") or 0) < 0 else "pos"})
    if ppi:
        cards.append({"name": "PPI 同比", "value": f"{ppi.get('yoy')}%", "sub": ppi.get("time"),
                      "tone": "neg" if (ppi.get("yoy") or 0) < 0 else "pos"})
    if pmi:
        m = pmi.get("manufacturing")
        cards.append({"name": "制造业 PMI", "value": str(m), "sub": pmi.get("time"),
                      "tone": "pos" if (m or 0) >= 50 else "neg"})
        cards.append({"name": "非制造业 PMI", "value": str(pmi.get("non_manufacturing")),
                      "sub": pmi.get("time"),
                      "tone": "pos" if (pmi.get("non_manufacturing") or 0) >= 50 else "neg"})
    if gdp:
        cards.append({"name": "GDP 同比", "value": f"{gdp.get('yoy')}%", "sub": gdp.get("time"),
                      "tone": "pos" if (gdp.get("yoy") or 0) > 0 else "neg"})
    if rr:
        cards.append({"name": "大型银行存准率", "value": f"{rr.get('big_bank')}%", "sub": rr.get("date"),
                      "tone": "pos" if (rr.get("change") or 0) < 0 else "muted"})
    for r in (rt.get("rows") or [])[:2]:
        cards.append({"name": r["label"], "value": f"{r['value']}{r['unit']}",
                      "sub": f"较昨 {r['change']:+}", "tone": "pos" if r["change"] > 0 else "neg"})

    # 简易宏观打分（-3 ~ +3）
    score = 0
    if pmi.get("manufacturing") is not None:
        score += 1 if pmi["manufacturing"] >= 50 else -1
    if ppi.get("yoy") is not None:
        score += 1 if ppi["yoy"] > 0 else -1
    if cpi.get("yoy") is not None:
        # 温和通胀（0.5~3%）最利于股市
        score += 1 if 0.5 <= cpi["yoy"] <= 3 else -1
    if rr.get("change") is not None:
        score += 1 if rr["change"] < 0 else (-1 if rr["change"] > 0 else 0)
    tone = "偏多" if score >= 2 else ("偏空" if score <= -2 else "中性")
    return {"cards": cards, "score": score, "tone": tone,
            "inflation": inf, "economy": eco, "rates": rt}


# ------------------------------------------------------------------ LLM 解读

def llm_brief(symbol: str = "", industry: str = "") -> dict:
    """用 DeepSeek 解读宏观环境对大盘/板块/个股的影响。返回 {'ok','text','error'}。"""
    import deepseek
    if not deepseek.available():
        return {"ok": False, "text": "", "error": "LLM 未配置"}
    snap = snapshot()
    lines = [f"- {c['name']}: {c['value']}（{c['sub']}）" for c in snap["cards"]]
    key = "macro|" + symbol + "|" + industry + "|" + "|".join(lines)
    sys_p = ("你是一位宏观分析师。基于给出的真实宏观数据，分析对 A 股大盘、指定行业与个股的影响。"
             "必须：① 只引用提供的数据，不得编造数字；② 给出假设条件与不确定性；"
             "③ 不得出现保本、稳赚、必涨等承诺性表述；④ 结尾声明不构成投资建议。"
             "输出 220 字以内中文，分点。")
    user_p = (f"【宏观数据】\n" + "\n".join(lines) +
              f"\n\n【当前标的】{symbol or '未指定'}    【所属行业】{industry or '未指定'}"
              f"\n\n请分析：1) 对大盘的方向影响 2) 对上述行业的影响 3) 需要警惕的风险。")
    r = deepseek.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                      temperature=0.3, max_tokens=700, tag="macro_brief", use_cache=False)
    return {"ok": r["ok"], "text": r["text"], "error": r["error"], "key": key}
