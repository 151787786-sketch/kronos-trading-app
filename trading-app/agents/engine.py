"""Agent 引擎：规则引擎产出结构化事实 + DeepSeek 生成自然语言观点。

设计原则（防止 LLM 幻觉污染结论）：
  * **立场分（stance_score）由规则引擎计算**，LLM 无权修改 —— 保证可复现、可回溯
  * LLM 只负责把结构化事实写成自然语言（观点/论据/风险/假设），并补充它自己的风险提示
  * 所有事实都必须来自真实数据源，prompt 中明确禁止编造数字
  * LLM 不可用时自动回退纯规则引擎，功能不降级为「不可用」
"""
import json
import time

import analytics
import fundamentals
import indicators as ind
import kronos_service
import market
from agents.roles import ROLES, Role, get_role


# ---------------------------------------------------------------------------
# 数据上下文（9 个 Agent 共享的输入，只抓一次）
# ---------------------------------------------------------------------------

def build_context(symbols: list, with_forecast: bool = True) -> dict:
    """Gather all data the agents need. symbols: list of 6-digit codes."""
    ctx = {"symbols": symbols, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "quotes": {}, "kline_meta": {}, "forecasts": {}, "buysell": {},
           "valuation": {}, "fundamentals": {}, "industry": {}, "sentiment": {},
           "reports": {}, "macro": {}, "errors": []}

    # 宏观（一次即可，全局共享）
    try:
        import macro
        ctx["macro"] = macro.snapshot()
    except Exception as e:
        ctx["errors"].append(f"macro: {e}")

    # 舆情/行业/研报按标的数量限流：最多前 5 只做重数据，其余只做行业
    heavy = symbols[:5]

    for sym in symbols:
        try:
            q = market.fetch_realtime_one(sym)
            if q:
                ctx["quotes"][sym] = q
        except Exception as e:
            ctx["errors"].append(f"quote {sym}: {e}")

        try:
            df = market.fetch_daily(sym, bars=600)
            dfe = ind.all_indicators(df)
            last = dfe.iloc[-1]
            closes = df["close"]
            ctx["kline_meta"][sym] = {
                "last_close": float(closes.iloc[-1]),
                "ret_20d_pct": round(float(closes.iloc[-1] / closes.iloc[-21] - 1) * 100, 2) if len(closes) > 21 else None,
                "ret_60d_pct": round(float(closes.iloc[-1] / closes.iloc[-61] - 1) * 100, 2) if len(closes) > 61 else None,
                "ma20": round(float(last["ma20"]), 2),
                "ma60": round(float(last["ma60"]), 2),
                "rsi": round(float(last["rsi"]), 1),
                "macd": round(float(last["macd"]), 3),
                "dif": round(float(last["dif"]), 3),
                "dea": round(float(last["dea"]), 3),
                "kdj_k": round(float(last["kdj_k"]), 1),
                "kdj_d": round(float(last["kdj_d"]), 1),
                "boll_upper": round(float(last["boll_upper"]), 2),
                "boll_lower": round(float(last["boll_lower"]), 2),
                "vol_annual_pct": round(float(closes.pct_change().std() * (252 ** 0.5) * 100), 2),
            }
        except Exception as e:
            ctx["errors"].append(f"kline {sym}: {e}")

        if with_forecast:
            try:
                df = market.fetch_daily(sym, bars=600)
                ctx["forecasts"][sym] = kronos_service.forecast(sym, df, lookback=400, pred_len=30)
            except Exception as e:
                ctx["errors"].append(f"forecast {sym}: {e}")

        try:
            import buysell
            df = market.fetch_daily(sym, bars=600)
            fc = ctx["forecasts"].get(sym)
            ctx["buysell"][sym] = buysell.buy_sell_plan(df, forecast=fc)
        except Exception as e:
            ctx["errors"].append(f"buysell {sym}: {e}")

        try:
            ctx["fundamentals"][sym] = fundamentals.analyze(sym)
        except Exception as e:
            ctx["errors"].append(f"fundamentals {sym}: {e}")

        # 行业景气度（真实板块数据）
        try:
            import industry
            ctx["industry"][sym] = industry.industry_for(sym)
        except Exception as e:
            ctx["errors"].append(f"industry {sym}: {e}")

        if sym in heavy:
            try:
                import sentiment
                ctx["sentiment"][sym] = sentiment.analyze(sym, use_llm=True, limit=8)
            except Exception as e:
                ctx["errors"].append(f"sentiment {sym}: {e}")
            try:
                import research_db
                ctx["reports"][sym] = research_db.ratings_summary(sym, days=180)
            except Exception as e:
                ctx["errors"].append(f"reports {sym}: {e}")

    return ctx


def _view(sym, ctx):
    q = ctx["quotes"].get(sym, {})
    km = ctx["kline_meta"].get(sym, {})
    fc = ctx["forecasts"].get(sym, {})
    bs = ctx["buysell"].get(sym, {})
    fu = ctx["fundamentals"].get(sym, {})
    name = q.get("name") or fu.get("company", {}).get("name") or sym
    return q, km, fc, bs, fu, name


# ---------------------------------------------------------------------------
# 规则引擎：按角色生成结构化事实 + 立场分
# ---------------------------------------------------------------------------

def rule_opinion(role: Role, symbols: list, ctx: dict) -> dict:
    """纯规则引擎：产出可复现的结构化观点（立场分由这里决定）。"""
    rid = role.id
    per_symbol = []
    evidence_all, risks_all, assumptions_all = [], [], []
    stance_score = 0.0

    for sym in symbols:
        q, km, fc, bs, fu, name = _view(sym, ctx)
        ev, rk, asm = [], [], []
        contribution = 0.0

        if rid == "cio":
            ev.append(f"标的池 {len(symbols)} 只，任务：投研分析与调仓建议")
            asm.append("会议结论基于各专家独立观点汇总，存在信息时滞")

        elif rid == "pm":
            ret20 = km.get("ret_20d_pct")
            if ret20 is not None:
                ev.append(f"{name} 近20日收益 {ret20:+.1f}%")
                contribution += max(-2, min(2, ret20 / 10))
            ret60 = km.get("ret_60d_pct")
            if ret60 is not None:
                ev.append(f"{name} 近60日收益 {ret60:+.1f}%")
            asm.append("等权组合假设，未考虑税费与冲击成本")

        elif rid == "researcher":
            rel = (fu.get("valuation") or {})
            if rel.get("pe_ttm"):
                ev.append(f"{name} PE(TTM) {rel['pe_ttm']:.1f}，PB {rel.get('pb') or 0:.2f}")
            if fc:
                ev.append(f"Kronos 30日预测 {fc.get('change_pct'):+.1f}%（区间 ±{((fc.get('confidence') or {}).get('mae_pct',0.15))*100:.0f}%）")
                contribution += max(-2, min(2, fc.get("change_pct", 0) / 5))
            rep = ctx.get("reports", {}).get(sym) or {}
            if rep.get("total"):
                ev.append(f"近半年 {rep['total']} 篇券商研报，评级分布 {rep.get('dist')}")
            asm.append("估值与预测均基于公开数据，未含未公开信息")

        elif rid == "risk":
            # 波动/回撤
            vol = km.get("vol_annual_pct")
            try:
                import nightly
                vol_high = nightly.get_param("risk.vol_high")
                dd_high = nightly.get_param("risk.drawdown_high")
                sent_high = nightly.get_param("risk.sentiment_high")
            except Exception:
                vol_high, dd_high, sent_high = 45.0, -20.0, -0.35
            if vol:
                ev.append(f"{name} 年化波动 {vol:.1f}%")
                if vol > vol_high:
                    rk.append(f"高波动（{vol:.1f}% > {vol_high:.0f}%），需控制仓位")
                    contribution -= 1.0
            ret60 = km.get("ret_60d_pct")
            if ret60 is not None and ret60 < dd_high:
                rk.append(f"近60日下跌 {ret60:.1f}%，处于弱势（阈值 {dd_high:.0f}%）")
                contribution -= 1.0
            # 舆情风险维度（真实新闻情感）
            sent = ctx.get("sentiment", {}).get(sym) or {}
            if sent.get("count"):
                ev.append(f"{name} 舆情 {sent['count']} 条，均值 {sent['avg']}（{sent['label']}），"
                          f"正面 {sent['pos']} / 负面 {sent['neg']}")
                if (sent.get("avg") or 0) < sent_high:
                    rk.append(f"舆情偏负面（均值 {sent['avg']} < {sent_high}），存在事件性风险")
                    contribution -= 1.0
                elif (sent.get("avg") or 0) > 0.25:
                    contribution += 0.4
            if not rk:
                ev.append(f"{name} 未触发内置风险阈值")
            asm.append("风险扫描基于量化阈值 + 新闻舆情，不含未公开信息与突发政策")

        elif rid == "compliance":
            ev.append(f"审查 {name} 相关表述：禁止保本/稳赚；须标注假设与置信度")
            asm.append("合规判断基于内置规则库（10 条禁词），非法律意见")

        elif rid == "trader":
            turnover = q.get("turnover")
            if turnover is not None:
                ev.append(f"{name} 换手率 {turnover:.2f}%")
                if turnover < 0.5:
                    rk.append("换手偏低，大额建仓可能产生滑点")
            entry = (bs.get("entry_zone") or {})
            if entry.get("low"):
                ev.append(f"技术买点区间 {entry.get('low')}~{entry.get('high')}")
            if bs.get("stop_loss"):
                ev.append(f"止损 {bs.get('stop_loss')}，目标 {bs.get('target_up')}")
            asm.append("滑点按历史波动估算，实际成交受盘口影响")

        elif rid == "industry":
            info = ctx.get("industry", {}).get(sym) or {}
            b = info.get("board") or {}
            if b:
                ev.append(f"{name} 所属行业 {info.get('industry')}：今日 {b.get('pct'):+.2f}%，"
                          f"5日 {b.get('pct5'):+.2f}%，20日 {b.get('pct20'):+.2f}%")
                ev.append(f"行业换手 {b.get('turnover_rate')}%，量比 {b.get('volume_ratio')}，"
                          f"主力净流入 {b.get('main_inflow')} 万，上涨 {b.get('up_count')} / 下跌 {b.get('down_count')} 家")
                ev.append(f"行业景气度 {b.get('prosperity')}/100，排名 {info.get('rank')}/{info.get('total')}")
                contribution += max(-1.5, min(1.5, (b.get("pct5") or 0) / 6))
            else:
                fu_c = fu.get("company") or {}
                if fu_c.get("industry"):
                    ev.append(f"{name} 所属行业：{fu_c['industry']}（板块数据暂缺）")
            asm.append("行业景气度基于板块行情与资金流计算，非行业基本面调研")

        elif rid == "macro":
            snap = ctx.get("macro") or {}
            for c in (snap.get("cards") or [])[:6]:
                ev.append(f"{c['name']} {c['value']}（{c['sub']}）")
            tone = snap.get("tone")
            score = snap.get("score")
            if tone:
                ev.append(f"宏观综合打分 {score}（-3~+3），倾向：{tone}")
                contribution += max(-1.2, min(1.2, (score or 0) * 0.4))
            try:
                import global_market
                g = global_market.get_global_markets()
                up = sum(1 for m in g if (m.get("pct") or 0) > 0)
                if g:
                    avg = sum((m.get("pct") or 0) for m in g) / len(g)
                    ev.append(f"外围 {len(g)} 个指数中 {up} 个上涨，均值 {avg:+.2f}%")
                    contribution += max(-0.6, min(0.6, avg * 0.5))
            except Exception:
                ev.append("外围数据暂不可用")
            asm.append("宏观数据含 CPI/PPI/PMI/GDP/存准率 + GC001 资金利率，均为公开数据，存在发布时滞")

        elif rid == "quant":
            dif, dea, macd = km.get("dif"), km.get("dea"), km.get("macd")
            if dif is not None:
                if dif > dea and macd > 0:
                    ev.append(f"{name} MACD 多头（DIF {dif} > DEA {dea}）")
                    contribution += 0.6
                elif dif < dea:
                    ev.append(f"{name} MACD 空头（DIF {dif} < DEA {dea}）")
                    contribution -= 0.6
            rsi = km.get("rsi")
            if rsi is not None:
                ev.append(f"{name} RSI {rsi}")
                if rsi > 75:
                    rk.append(f"RSI {rsi} 超买，短线回调概率上升")
                    contribution -= 0.4
                elif rsi < 30:
                    rk.append(f"RSI {rsi} 超卖，留意反弹")
                    contribution += 0.4
            ret20 = km.get("ret_20d_pct")
            if fc and ret20 is not None:
                diff = fc.get("change_pct", 0) - ret20
                ev.append(f"Kronos 预测 {fc.get('change_pct', 0):+.1f}% vs 近20日实际动量 {ret20:+.1f}%")
                if abs(diff) > 10:
                    rk.append(f"预测方向与近期动量偏离 {diff:+.1f}%，需警惕模型偏差")
            # 研报一致预期校验
            rep = ctx.get("reports", {}).get(sym) or {}
            if rep.get("dist"):
                buy = sum(v for k, v in rep["dist"].items() if k in ("买入", "增持", "推荐", "强烈推荐"))
                ev.append(f"券商评级中偏正面 {buy}/{rep.get('total')} 篇")
            asm.append("量化校验基于指标与历史统计，不保证未来有效")

        per_symbol.append({
            "symbol": sym, "name": name,
            "evidence": ev, "risks": rk,
            "contribution": round(contribution, 2),
        })
        evidence_all.extend([f"[{name}] {e}" for e in ev])
        risks_all.extend([f"[{name}] {r}" for r in rk])
        stance_score += contribution
        assumptions_all.extend(asm)

    if stance_score > 0.8:
        view = "偏多"
    elif stance_score < -0.8:
        view = "偏空"
    else:
        view = "中性"

    conf = min(0.85, 0.45 + 0.1 * len(evidence_all)) if evidence_all else 0.3

    return {
        "role_id": role.id, "role_name": role.name, "title": role.title,
        "view": view, "stance_score": round(stance_score, 2),
        "evidence": evidence_all, "risks": risks_all,
        "confidence": round(conf, 2),
        "assumptions": sorted(set(assumptions_all)),
        "per_symbol": per_symbol, "engine": "rule",
        "narrative": "", "key_points": [],
    }


# ---------------------------------------------------------------------------
# DeepSeek 自然语言层
# ---------------------------------------------------------------------------

_LLM_SYS = (
    "你是{u_name}（{u_title}），正在参加一场 A 股投研圆桌会议。\n"
    "【你的职责】{u_focus}\n"
    "【你的立场倾向】{u_stance}\n\n"
    "【硬性要求】\n"
    "1) 只能引用用户提供的【事实清单】中的数字与结论，绝对禁止编造任何数据；\n"
    "2) 必须明确写出你结论所依赖的假设条件；\n"
    "3) 必须指出不确定性与你判断可能出错的情形；\n"
    "4) 严禁出现「保本」「稳赚」「必涨」「无风险」等承诺性表述；\n"
    "5) 不要写免责声明（系统会统一附加）；\n"
    "6) 只输出 JSON，格式：\n"
    '{{"narrative":"你的发言，140~220字，第一人称，直接给结论与理由",'
    '"key_points":["要点1","要点2","要点3"],'
    '"risks":["你额外识别的风险1","风险2"],'
    '"assumptions":["假设1","假设2"]}}\n'
)


def llm_opinion(role: Role, symbols: list, ctx: dict, rule_out: dict,
                extra_hint: str = "") -> dict:
    """让 DeepSeek 基于规则引擎给出的事实写一段发言。返回 {} 表示失败。"""
    import deepseek
    if not deepseek.available():
        return {}
    facts = []
    for ps in rule_out["per_symbol"]:
        facts.append(f"【{ps['name']}（{ps['symbol']}）】")
        facts += [f"  · {e}" for e in ps["evidence"]]
        facts += [f"  ! 风险：{r}" for r in ps["risks"]]
        facts.append(f"  规则引擎量化打分：{ps['contribution']:+.2f}")
    macro_lines = []
    for c in ((ctx.get("macro") or {}).get("cards") or [])[:8]:
        macro_lines.append(f"  · {c['name']}：{c['value']}（{c['sub']}）")

    user = (
        f"【会议标的】{', '.join(symbols)}\n"
        f"【规则引擎已算出的立场】{rule_out['view']}（打分 {rule_out['stance_score']:+.2f}）\n\n"
        f"【事实清单】\n" + "\n".join(facts[:80]) +
        (("\n\n【宏观数据】\n" + "\n".join(macro_lines)) if macro_lines else "") +
        (f"\n\n【额外要求】{extra_hint}" if extra_hint else "") +
        "\n\n请以你的角色身份发言。只输出 JSON。"
    )
    sys_p = _LLM_SYS.format(u_name=role.name, u_title=role.title,
                            u_focus=role.focus, u_stance=role.stance)
    data = deepseek.chat_json(sys_p, user, temperature=0.4, max_tokens=1100,
                              tag=f"agent_{role.id}", use_cache=False)
    if not isinstance(data, dict):
        return {}
    narrative = str(data.get("narrative") or "").strip()
    if not narrative:
        return {}
    return {
        "narrative": narrative[:900],
        "key_points": [str(x)[:80] for x in (data.get("key_points") or [])][:5],
        "risks": [str(x)[:120] for x in (data.get("risks") or [])][:4],
        "assumptions": [str(x)[:120] for x in (data.get("assumptions") or [])][:4],
    }


def analyze_as(role: Role, symbols: list, ctx: dict, use_llm: bool = True,
               extra_hint: str = "") -> dict:
    """单个角色的完整观点：规则事实 + （可选）DeepSeek 发言。"""
    out = rule_opinion(role, symbols, ctx)
    if not use_llm:
        return out
    extra_hint = extra_hint or _role_hint(role.id)
    llm = llm_opinion(role, symbols, ctx, out, extra_hint=extra_hint)
    if not llm:
        return out
    out["narrative"] = llm["narrative"]
    out["key_points"] = llm["key_points"]
    if llm["risks"]:
        out["risks"] = out["risks"] + [f"[{role.name}·LLM] {r}" for r in llm["risks"]]
    if llm["assumptions"]:
        out["assumptions"] = sorted(set(out["assumptions"] + llm["assumptions"]))
    out["engine"] = "deepseek"
    return out


def _role_hint(role_id: str) -> str:
    """夜间自迭代写入的角色提示词补充。"""
    try:
        import nightly
        return nightly.role_hint(role_id)
    except Exception:
        return ""


def analyze_parallel(roles: list, symbols: list, ctx: dict, use_llm: bool = True) -> list:
    """并发跑多个角色（DeepSeek 并发闸门内部限流）。第0轮使用。"""
    if not use_llm:
        return [analyze_as(r, symbols, ctx, use_llm=False) for r in roles]
    import deepseek
    if not deepseek.available():
        return [analyze_as(r, symbols, ctx, use_llm=False) for r in roles]
    from concurrent.futures import ThreadPoolExecutor
    n = min(len(roles), deepseek.load_config().get("concurrency", 4) * 2)

    def run(r):
        try:
            return analyze_as(r, symbols, ctx, use_llm=True)
        except Exception:
            return analyze_as(r, symbols, ctx, use_llm=False)

    with ThreadPoolExecutor(max_workers=max(1, n)) as ex:
        return list(ex.map(run, roles))
