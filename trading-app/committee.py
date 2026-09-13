"""群智协作引擎：4 轮投研会议（抑制幻觉）。

第 0 轮 独立闭门输出：9 Agent 并行独立生成观点（互不参考）
第 1 轮 圆桌辩论：公开观点，对立立场互相质疑（假设/数据/逻辑）
第 2 轮 交叉互评：互评报告，排查逻辑断层/数据错误/主观幻觉/参数假设缺陷
第 3 轮 共识裁决：主理人整合、权衡分歧、输出结论与调仓方案，标记争议点

全程写入审计表（audit.py），可回溯。
"""
import time

import audit
import deepseek
import market
from agents.engine import analyze_as, analyze_parallel, build_context
from agents.roles import ROLES, chair, get_role

USE_LLM = True  # DeepSeek 可用时自动使用；不可用自动回退规则引擎

# LLM 调用预算：避免 20 只标的的会议产生上百次请求（耗时与费用失控）
MAX_LLM_DEBATES = 8
MAX_LLM_REVIEWS = 12
_budget = {"debate": 0, "review": 0}


def _take(kind: str, limit: int) -> bool:
    """占用一次 LLM 预算，超出返回 False（该条改用规则引擎）。"""
    with _budget_lock:
        if _budget[kind] >= limit:
            return False
        _budget[kind] += 1
        return True


import threading as _threading
_budget_lock = _threading.Lock()


def _opposed(a: dict, b: dict) -> bool:
    """Two roles hold opposing views."""
    views = {a["view"], b["view"]}
    return views == {"偏多", "偏空"}


_DEBATE_SYS = (
    "你正在主持一场 A 股投研圆桌辩论。下面是两位分析师的发言摘要。\n"
    "请生成 2~3 条**有实质内容的质询**（不是客套），必须针对对方的：具体数据、逻辑跳跃、或隐含假设。\n"
    "禁止编造对方没说过的数据。只输出 JSON：\n"
    '{"challenges":["质询1","质询2"],"defense":["被质询方的合理回应1","回应2"],'
    '"key_conflict":"一句话概括双方的真正分歧"}'
)


def _debate_pair(a: dict, b: dict) -> dict:
    """Generate structured debate content between two opposing views."""
    challenges = []
    for ev in b["evidence"][:2]:
        challenges.append(f"针对「{ev}」，请给出数据来源与样本区间；单一维度不足以支撑结论。")
    for asm in b["assumptions"][:1]:
        challenges.append(f"你假设「{asm}」——若该假设不成立，结论如何变化？")
    defense = [
        f"我的依据来自公开数据：{'; '.join(a['evidence'][:2]) or '（数据有限）'}",
        f"已标注不确定性：置信度 {a['confidence']}，并列出假设 {a['assumptions'][:1]}",
    ]
    key_conflict = f"{a['role_name']}({a['view']}) 与 {b['role_name']}({b['view']}) 的立场相反"

    if USE_LLM and deepseek.available() and _take("debate", MAX_LLM_DEBATES):
        user = (
            f"【{a['role_name']}（{a['view']}，打分 {a['stance_score']:+.2f}）发言】\n"
            f"{(a.get('narrative') or '；'.join(a['evidence'][:4]))}\n"
            f"其依据：{'; '.join(a['evidence'][:4]) or '（无）'}\n\n"
            f"【{b['role_name']}（{b['view']}，打分 {b['stance_score']:+.2f}）发言】\n"
            f"{(b.get('narrative') or '；'.join(b['evidence'][:4]))}\n"
            f"其依据：{'; '.join(b['evidence'][:4]) or '（无）'}\n\n"
            f"请输出针对 {b['role_name']} 的质询。"
        )
        data = deepseek.chat_json(_DEBATE_SYS, user, temperature=0.5, max_tokens=800,
                                  tag="debate", use_cache=False)
        if isinstance(data, dict) and data.get("challenges"):
            return {
                "challenger": a["role_name"], "challenger_view": a["view"],
                "respondent": b["role_name"], "respondent_view": b["view"],
                "challenges": [str(x)[:200] for x in data["challenges"]][:3],
                "defense": [str(x)[:200] for x in (data.get("defense") or [])][:2],
                "key_conflict": str(data.get("key_conflict") or key_conflict)[:120],
                "engine": "deepseek",
            }
    return {
        "challenger": a["role_name"], "challenger_view": a["view"],
        "respondent": b["role_name"], "respondent_view": b["view"],
        "challenges": challenges,
        "defense": defense,
        "key_conflict": key_conflict,
        "engine": "rule",
    }


_REVIEW_SYS = (
    "你是投研会的交叉审稿人，任务是**排查 AI 幻觉与逻辑缺陷**。\n"
    "对给定分析师的发言，逐条找出以下 5 类问题（没有则不留）：\n"
    "1) 逻辑断层：结论与依据不匹配\n"
    "2) 数据错误：数字自相矛盾或与事实清单不符\n"
    "3) 主观臆断：用词过度确定、无依据的预测\n"
    "4) 假设缺陷：隐含假设不成立会推翻结论\n"
    "5) 合规问题：承诺性表述（保本/稳赚/必涨）\n"
    "只输出 JSON：{\"findings\":[{\"type\":\"类型\",\"detail\":\"具体问题\",\"severity\":\"高/中/低\"}],"
    "\"verdict\":\"通过 或 需修正\"}"
)


def _review(reviewer: dict, target: dict) -> dict:
    """Cross-review: hunt for logic gaps / data errors / hallucination / bad assumptions."""
    findings = []
    if target["view"] != "中性" and len(target["evidence"]) == 0:
        findings.append({"type": "逻辑断层", "detail": f"{target['role_name']} 给出「{target['view']}」但无支撑证据", "severity": "高"})
    if len(target["risks"]) > len(target["evidence"]):
        findings.append({"type": "数据不足", "detail": f"{target['role_name']} 风险点({len(target['risks'])})多于证据({len(target['evidence'])})，论证偏弱", "severity": "中"})
    if target["confidence"] > 0.75 and len(target["evidence"]) < 2:
        findings.append({"type": "疑似主观臆断", "detail": f"{target['role_name']} 置信度 {target['confidence']} 但证据仅 {len(target['evidence'])} 条，存在过度自信", "severity": "高"})
    if not target["assumptions"]:
        findings.append({"type": "参数假设缺失", "detail": f"{target['role_name']} 未声明任何假设条件", "severity": "中"})
    if abs(target["stance_score"]) > 2 and len(target["evidence"]) < 3:
        findings.append({"type": "立场与证据不匹配", "detail": f"{target['role_name']} 立场强度 {target['stance_score']} 而证据仅 {len(target['evidence'])} 条", "severity": "中"})

    engine = "rule"
    if (USE_LLM and deepseek.available() and target.get("narrative")
            and _take("review", MAX_LLM_REVIEWS)):
        user = (
            f"【被审对象】{target['role_name']}（立场 {target['view']}，量化打分 {target['stance_score']:+.2f}，"
            f"自评置信度 {target['confidence']}）\n"
            f"【其发言】{target['narrative']}\n"
            f"【其引用的事实】{'; '.join(target['evidence'][:8]) or '（无）'}\n"
            f"【其声明的假设】{'; '.join(target['assumptions'][:4]) or '（无）'}\n\n"
            f"请以 {reviewer['role_name']} 的身份审稿，只输出 JSON。"
        )
        data = deepseek.chat_json(_REVIEW_SYS, user, temperature=0.2, max_tokens=800,
                                  tag="review", use_cache=False)
        if isinstance(data, dict):
            findings_raw = data.get("findings")
            if not isinstance(findings_raw, list):
                findings_raw = deepseek.unwrap_list(data)
            llm_findings = []
            for f in (findings_raw or [])[:4]:
                if isinstance(f, dict) and f.get("detail"):
                    llm_findings.append({"type": str(f.get("type") or "问题")[:12],
                                         "detail": str(f["detail"])[:200],
                                         "severity": str(f.get("severity") or "中")[:2],
                                         "by": "deepseek"})
            if llm_findings:
                findings = findings + llm_findings
                engine = "deepseek"

    return {
        "reviewer": reviewer["role_name"],
        "target": target["role_name"],
        "target_view": target["view"],
        "findings": findings,
        "verdict": "通过" if not findings else f"需修正（{len(findings)} 项）",
        "engine": engine,
    }


def run_committee(symbols: list, session_name: str = None, with_forecast: bool = True) -> dict:
    """Run a full 4-round research committee. Returns the complete minutes."""
    t0 = time.time()
    with _budget_lock:
        _budget["debate"] = 0
        _budget["review"] = 0
    sid = audit.create_session(session_name or f"投研会-{time.strftime('%Y%m%d-%H%M%S')}", symbols)

    # 共享数据上下文
    ctx = build_context(symbols, with_forecast=with_forecast)
    audit.log(sid, "system", "system", "数据准备",
              f"标的 {len(symbols)} 只，错误 {len(ctx['errors'])} 条", {"errors": ctx["errors"]})

    # ---------- 第 0 轮：独立闭门输出 ----------
    round0 = analyze_parallel(list(ROLES), symbols, ctx, use_llm=USE_LLM)
    for role, op in zip(ROLES, round0):
        audit.log(sid, "round0", role.id, role.name, op["view"], op)
    llm_agents = sum(1 for o in round0 if o.get("engine") == "deepseek")
    audit.log_round(sid, 0, "独立闭门输出",
                    f"{len(round0)} 位 Agent 完成独立观点（其中 {llm_agents} 位由 DeepSeek 生成发言）")

    # ---------- 第 1 轮：圆桌辩论 ----------
    # 三种配对策略（保证单边行情下也有交锋）：
    #   1) 立场对立（偏多 vs 偏空）优先配对
    #   2) 若无对立：最乐观 vs 最悲观 配对（立场分差 ≥ 0.5）
    #   3) 再若无：审计类角色（风控/量化/合规）主动质询其余各方
    debates = []
    AUDIT_ROLES = {"risk", "quant", "compliance"}
    participants = [o for o in round0 if not get_role(o["role_id"]).is_chair]
    bullish = [o for o in participants if o["view"] == "偏多"]
    bearish = [o for o in participants if o["view"] == "偏空"]

    if bullish and bearish:
        for a in bullish:
            for b in bearish:
                d = _debate_pair(a, b)
                d["mode"] = "对立立场辩论"
                debates.append(d)
    else:
        # 策略 2：最乐观 vs 最悲观
        ranked = sorted(participants, key=lambda x: x["stance_score"], reverse=True)
        if len(ranked) >= 2 and (ranked[0]["stance_score"] - ranked[-1]["stance_score"]) >= 0.5:
            d = _debate_pair(ranked[0], ranked[-1])
            d["mode"] = "乐观方 vs 悲观方（无严格对立）"
            debates.append(d)
        # 策略 3：审计角色质询全体
        auditors = [o for o in participants if o["role_id"] in AUDIT_ROLES]
        others = [o for o in participants if o["role_id"] not in AUDIT_ROLES]
        for aud in auditors:
            for tgt in others:
                d = _debate_pair(aud, tgt)
                d["mode"] = f"{aud['role_name']}主动质询（审计职责）"
                debates.append(d)

    for d in debates:
        audit.log(sid, "round1", "debate", f"{d['challenger']}→{d['respondent']}",
                  f"{d.get('mode','辩论')}", d)
    audit.log_round(sid, 1, "圆桌辩论", f"{len(debates)} 组观点交锋（含审计质询）")

    # ---------- 第 2 轮：交叉互评（幻觉排查）----------
    # 审计类角色必须评审全部其他角色（保证互评机制始终运行）；
    # 其他角色之间仅在发现问题时记录。
    reviews = []
    for reviewer in round0:
        is_auditor = reviewer["role_id"] in AUDIT_ROLES
        for target in round0:
            if reviewer["role_id"] == target["role_id"]:
                continue
            rv = _review(reviewer, target)
            if rv["findings"] or is_auditor:
                rv["mandatory"] = bool(is_auditor)
                reviews.append(rv)
                audit.log(sid, "round2", reviewer["role_id"], reviewer["role_name"],
                          f"评审 {target['role_name']}：{rv['verdict']}", rv)
    audit.log_round(sid, 2, "交叉互评",
                    f"{len(reviews)} 条评审意见（其中审计强制评审 {len([x for x in reviews if x.get('mandatory')])} 条）")

    # ---------- 第 3 轮：共识汇总与裁决 ----------
    # 分歧点：立场相反的 Agent 对
    disagreements = []
    for a in round0:
        for b in round0:
            if a["role_id"] < b["role_id"] and _opposed(a, b):
                disagreements.append({
                    "a": a["role_name"], "a_view": a["view"], "a_score": a["stance_score"],
                    "b": b["role_name"], "b_view": b["view"], "b_score": b["stance_score"],
                    "point": f"{a['role_name']}（{a['view']}） vs {b['role_name']}（{b['view']}）",
                })

    # 加权立场（主理人裁决）
    scored = [o for o in round0 if not get_role(o["role_id"]).is_chair]
    avg_stance = sum(o["stance_score"] for o in scored) / len(scored) if scored else 0
    if avg_stance > 0.5:
        verdict = "偏多"
    elif avg_stance < -0.5:
        verdict = "偏空"
    else:
        verdict = "中性/观望"

    # 与 Kronos 预测交叉校验（冲突标记）
    conflicts = []
    for sym in symbols:
        fc = ctx["forecasts"].get(sym)
        if not fc:
            continue
        pred_chg = fc.get("change_pct", 0)
        pred_dir = "偏多" if pred_chg > 2 else ("偏空" if pred_chg < -2 else "中性/观望")
        if verdict != "中性/观望" and pred_dir != "中性/观望" and pred_dir != verdict:
            conflicts.append({
                "symbol": sym,
                "name": ctx["quotes"].get(sym, {}).get("name", sym),
                "agent_verdict": verdict,
                "kronos_prediction": pred_dir,
                "kronos_change_pct": pred_chg,
                "note": "多 Agent 结论与 Kronos 预测方向冲突，建议降低仓位或等待确认",
                "highlight": True,
            })

    # 置信度（证据量与分歧反向修正）
    base_conf = sum(o["confidence"] for o in scored) / len(scored) if scored else 0.4
    penalty = min(0.2, 0.05 * len(disagreements))
    final_conf = max(0.2, base_conf - penalty)

    chair_role = chair()
    verdict_text = (
        f"经 {len(round0)} 位数字员工独立分析、{len(debates)} 组观点辩论、"
        f"{len(reviews)} 条交叉评审后，主理人裁决：**{verdict}**"
        f"（加权立场 {avg_stance:+.2f}，最终置信度 {final_conf:.2f}）。"
    )
    if disagreements:
        verdict_text += f" 存在 {len(disagreements)} 组重大分歧，已在报告中显式列出。"
    if conflicts:
        verdict_text += f" 发现 {len(conflicts)} 项与 Kronos 预测的方向冲突，已高亮标记。"

    # DeepSeek 主理人总结陈词（方向仍由规则加权立场决定，LLM 只负责表述）
    chair_engine = "rule"
    if USE_LLM and deepseek.available():
        brief = []
        for o in scored:
            line = f"- {o['role_name']}（{o['view']}，{o['stance_score']:+.2f}）："
            line += (o.get("narrative") or "；".join(o["evidence"][:3]) or "（无发言）")[:200]
            brief.append(line)
        dis_txt = "\n".join(f"- {d['point']}（{d['a_score']:+.2f} vs {d['b_score']:+.2f}）"
                            for d in disagreements[:6]) or "（无严格对立分歧）"
        conf_txt = "\n".join(f"- {c['name']}：Agent 判 {c['agent_verdict']}，Kronos 预测 "
                             f"{c['kronos_prediction']}（{c['kronos_change_pct']:+.1f}%）"
                             for c in conflicts[:5]) or "（无）"
        review_txt = "\n".join(f"- {r['reviewer']}→{r['target']}：{r['verdict']}"
                               for r in reviews[:10]) or "（无）"
        user = (
            f"【量化裁决结果（不可更改）】{verdict}，加权立场 {avg_stance:+.2f}，置信度 {final_conf:.2f}\n\n"
            f"【各角色观点摘要】\n" + "\n".join(brief) +
            f"\n\n【重大分歧】\n{dis_txt}\n\n【与 Kronos 预测的冲突】\n{conf_txt}"
            f"\n\n【交叉互评结论】\n{review_txt}\n"
            f"\n【会议标的】{', '.join(symbols)}\n\n"
            "请以主理人身份输出 JSON："
            '{"text":"裁决陈词，200~320字，必须明确写出结论、主要理由、分歧如何处理、'
            '以及需要监控的关键条件","monitor":["需要盯的观察点1","观察点2","观察点3"]}'
        )
        data = deepseek.chat_json(
            "你是投研会主理人（CIO）。你要基于各分析师的观点做最终裁决陈述。"
            "必须：① 不得改变给定的量化裁决方向；② 显式说明分歧如何处理；"
            "③ 列出需要持续监控的条件；④ 禁止保本/稳赚/必涨等表述；⑤ 只输出 JSON。",
            user, temperature=0.4, max_tokens=1200, tag="verdict", use_cache=False)
        if isinstance(data, dict) and data.get("text"):
            verdict_text = str(data["text"])[:1500]
            chair_engine = "deepseek"
            monitor_points = [str(x)[:80] for x in (data.get("monitor") or [])][:5]
        else:
            monitor_points = []
    else:
        monitor_points = []

    verdict_obj = {
        "verdict": verdict,
        "avg_stance": round(avg_stance, 2),
        "confidence": round(final_conf, 2),
        "text": verdict_text,
        "quant_line": (
            f"经 {len(round0)} 位数字员工独立分析、{len(debates)} 组观点辩论、"
            f"{len(reviews)} 条交叉评审后，主理人裁决：**{verdict}**"
            f"（加权立场 {avg_stance:+.2f}，最终置信度 {final_conf:.2f}）。"
            + (f" 存在 {len(disagreements)} 组重大分歧。" if disagreements else "")
            + (f" 发现 {len(conflicts)} 项与 Kronos 预测的方向冲突。" if conflicts else "")
        ),
        "monitor": monitor_points,
        "engine": chair_engine,
        "disagreements": disagreements,
        "conflicts": conflicts,
        "chair": chair_role.name,
        "stages": {
            "agents": len(round0),
            "agents_llm": sum(1 for o in round0 if o.get("engine") == "deepseek"),
            "debates": len(debates),
            "debates_llm": sum(1 for d in debates if d.get("engine") == "deepseek"),
            "reviews": len(reviews),
            "reviews_llm": sum(1 for r in reviews if r.get("engine") == "deepseek"),
        },
        "assumptions": [
            "结论基于公开数据与量化规则，不含未公开信息",
            "市场存在不确定性，历史规律不保证未来有效",
            "本结论不构成投资建议",
        ],
    }
    audit.log(sid, "round3", chair_role.id, chair_role.name, verdict, verdict_obj)
    audit.log_round(sid, 3, "共识裁决", f"裁决 {verdict}，分歧 {len(disagreements)} 组，冲突 {len(conflicts)} 项")

    audit.finish_session(sid, {"verdict": verdict, "confidence": round(final_conf, 2)})

    return {
        "session_id": sid,
        "name": session_name,
        "symbols": symbols,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "rounds": {
            "round0_independent": round0,
            "round1_debate": debates,
            "round2_review": reviews,
            "round3_verdict": verdict_obj,
        },
        "context_digest": {
            "quotes": _quote_digest(ctx, symbols),
            "forecasts": {k: {"change_pct": v.get("change_pct"),
                              "band_pct": round(((v.get("confidence") or {}).get("mae_pct", 0.15)) * 100, 1),
                              "model": v.get("model")}
                          for k, v in ctx["forecasts"].items()},
            "macro": {"tone": (ctx.get("macro") or {}).get("tone"),
                      "score": (ctx.get("macro") or {}).get("score"),
                      "cards": (ctx.get("macro") or {}).get("cards") or []},
            "industry": {k: {"industry": v.get("industry"), "rank": v.get("rank"),
                             "total": v.get("total"), "prosperity": v.get("prosperity")}
                         for k, v in (ctx.get("industry") or {}).items()},
            "sentiment": {k: {"avg": v.get("avg"), "label": v.get("label"),
                              "count": v.get("count"), "risk_level": v.get("risk_level"),
                              "pos": v.get("pos"), "neg": v.get("neg")}
                          for k, v in (ctx.get("sentiment") or {}).items()},
            "reports": {k: {"total": v.get("total"), "dist": v.get("dist")}
                        for k, v in (ctx.get("reports") or {}).items()},
            "engine": "deepseek" if deepseek.available() else "rule",
            "errors": ctx["errors"],
        },
    }


def _quote_digest(ctx: dict, symbols: list) -> dict:
    """Build the quote digest with a fallback to the last K-line close when the
    realtime endpoint returns nothing (e.g. outside trading hours)."""
    out = {}
    for sym in symbols:
        q = ctx.get("quotes", {}).get(sym) or {}
        km = ctx.get("kline_meta", {}).get(sym) or {}
        price = q.get("price")
        if price is None:
            price = km.get("last_close")
        pct = q.get("pct")
        if pct is None:
            # derive from the last K-line vs previous close
            try:
                df = market.fetch_kline(sym, period="day", bars=3)
                if len(df) >= 2:
                    pct = round((float(df["close"].iloc[-1]) / float(df["close"].iloc[-2]) - 1) * 100, 2)
                    if price is None:
                        price = float(df["close"].iloc[-1])
            except Exception:
                pass
        out[sym] = {"name": q.get("name") or km.get("name") or sym,
                    "price": price, "pct": pct}
    return out
