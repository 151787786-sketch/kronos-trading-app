"""投研输出物生成：会议纪要 / 综合分析报告 / 结构化调仓清单(JSON)。

输出规范（需求第 6 条）：
  1. 投研会议完整纪要：各角色观点 + 辩论 + 互评完整记录
  2. 标的综合分析报告：现状分析 + 收益预期 + 风险清单 + 约束假设
  3. 结构化调仓清单（JSON，便于下游系统读取）

约束（需求第 7 条）：
  - 禁止保本/稳赚表述（compliance.sanitize）
  - 必须标注假设、不确定性、置信水平 + 不构成投资建议
  - 分歧显式列出
  - Kronos 预测冲突高亮
"""
import json
import time

import analytics
import compliance


DISCLAIMER = "本报告由 AI 数字员工团队自动生成，基于公开数据与量化规则，仅供研究参考，不构成投资建议。市场有风险，决策需谨慎。"


def build_minutes(minutes: dict) -> str:
    """生成投研会议完整纪要（Markdown）。"""
    r = minutes.get("rounds", {})
    lines = []
    lines.append(f"# 投研会议纪要｜{minutes.get('name') or '未命名会议'}")
    lines.append("")
    lines.append(f"- **会议编号**：#{minutes.get('session_id')}")
    lines.append(f"- **标的**：{', '.join(minutes.get('symbols', []))}")
    lines.append(f"- **时间**：{minutes.get('created')}")
    lines.append(f"- **用时**：{minutes.get('elapsed_s')} 秒")
    lines.append(f"- **参会数字员工**：{len(r.get('round0_independent', []))} 位")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 第 0 轮
    lines.append("## 第 0 轮 · 独立闭门输出（互不参考）")
    lines.append("")
    for o in r.get("round0_independent", []):
        lines.append(f"### {o['role_name']}（{o['title']}）— 立场：**{o['view']}**")
        lines.append(f"- 置信度：{o['confidence']}｜立场强度：{o['stance_score']}")
        if o.get("evidence"):
            lines.append("- **论据**：")
            for e in o["evidence"]:
                lines.append(f"  - {compliance.sanitize(e)}")
        if o.get("risks"):
            lines.append("- **风险点**：")
            for x in o["risks"]:
                lines.append(f"  - {compliance.sanitize(x)}")
        if o.get("assumptions"):
            lines.append("- **假设条件**：")
            for a in o["assumptions"]:
                lines.append(f"  - {a}")
        lines.append("")

    # 第 1 轮
    lines.append("## 第 1 轮 · 圆桌辩论")
    lines.append("")
    debates = r.get("round1_debate", [])
    if not debates:
        lines.append("_本轮无对立观点，未触发辩论。_")
    else:
        for i, d in enumerate(debates, 1):
            lines.append(f"### 辩论 {i}：{d['challenger']}（{d['challenger_view']}） → {d['respondent']}（{d['respondent_view']}）")
            lines.append("**质询：**")
            for c in d["challenges"]:
                lines.append(f"- {compliance.sanitize(c)}")
            lines.append("**回应：**")
            for x in d["defense"]:
                lines.append(f"- {compliance.sanitize(x)}")
            lines.append("")

    # 第 2 轮
    lines.append("## 第 2 轮 · 交叉互评（幻觉排查）")
    lines.append("")
    reviews = r.get("round2_review", [])
    if not reviews:
        lines.append("_本轮未发现需要修正的问题。_")
    else:
        for rv in reviews:
            lines.append(f"### {rv['reviewer']} 评审 {rv['target']} — {rv['verdict']}")
            for f in rv["findings"]:
                lines.append(f"- **[{f['type']}]** {compliance.sanitize(f['detail'])}")
            lines.append("")

    # 第 3 轮
    v = r.get("round3_verdict", {})
    lines.append("## 第 3 轮 · 共识汇总与裁决")
    lines.append("")
    lines.append(f"**主理人（{v.get('chair')}）裁决：{v.get('verdict')}**")
    lines.append("")
    lines.append(compliance.sanitize(v.get("text", "")))
    lines.append("")
    lines.append(f"- 加权立场：{v.get('avg_stance')}｜最终置信度：**{v.get('confidence')}**")
    lines.append("")

    if v.get("disagreements"):
        lines.append("### ⚠️ 尚存争议点（不得掩盖）")
        lines.append("")
        for d in v["disagreements"]:
            lines.append(f"- {d['point']}｜立场强度 A={d['a_score']} / B={d['b_score']}")
        lines.append("")

    if v.get("conflicts"):
        lines.append("### 🔴 与 Kronos 预测的冲突（高亮）")
        lines.append("")
        for c in v["conflicts"]:
            lines.append(f"- **{c['name']}（{c['symbol']}）**：多 Agent 结论「{c['agent_verdict']}」"
                         f" vs Kronos 预测「{c['kronos_prediction']}（{c['kronos_change_pct']:+.1f}%）」"
                         f" — {c['note']}")
        lines.append("")

    lines.append("### 约束假设")
    for a in v.get("assumptions", []):
        lines.append(f"- {a}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"> **免责声明**：{DISCLAIMER}")
    return "\n".join(lines)


def build_report(minutes: dict, analytics_data: dict = None) -> str:
    """生成标的综合分析报告（现状 + 收益预期 + 风险清单 + 约束假设）。"""
    a = analytics_data or {}
    v = (minutes.get("rounds") or {}).get("round3_verdict", {})
    symbols = minutes.get("symbols", [])
    lines = [f"# 标的综合分析报告｜{', '.join(symbols)}", ""]

    lines.append("## 一、现状分析")
    lines.append("")
    digest = minutes.get("context_digest", {})
    quotes = digest.get("quotes", {})
    for sym in symbols:
        q = quotes.get(sym, {})
        lines.append(f"- **{q.get('name', sym)}（{sym}）**：现价 {q.get('price')}，涨跌 {q.get('pct')}%")
    lines.append("")

    lines.append("## 二、收益预期（含不确定性）")
    lines.append("")
    mc = a.get("monte_carlo") or {}
    if mc and not mc.get("error"):
        rd = mc.get("return_dist", {})
        dd = mc.get("drawdown_dist", {})
        lines.append(f"基于 **{mc.get('n_sim')} 次蒙特卡洛模拟**（等权组合，视界 {mc.get('horizon_bars')} 根K线）：")
        lines.append("")
        lines.append("| 分位 | 组合收益 |")
        lines.append("|---|---|")
        for k, label in [("p5", "悲观 (P5)"), ("p25", "偏悲观 (P25)"), ("p50", "中位 (P50)"),
                         ("p75", "偏乐观 (P75)"), ("p95", "乐观 (P95)")]:
            lines.append(f"| {label} | {rd.get(k)}% |")
        lines.append("")
        lines.append(f"- 期望收益（均值）：**{rd.get('mean')}%**")
        lines.append(f"- 亏损概率：**{mc.get('prob_loss_pct')}%**")
        lines.append(f"- 回撤分布：中位 {dd.get('p50')}%，P95 {dd.get('p95')}%，最差 {dd.get('worst')}%")
        lines.append("")
        for x in mc.get("assumptions", []):
            lines.append(f"  - 假设：{x}")
    else:
        lines.append("_蒙特卡洛模拟数据不可用。_")
    lines.append("")

    lines.append("## 三、风险清单")
    lines.append("")
    rs = a.get("risk_scan") or {}
    if rs.get("items"):
        lines.append(f"风险概览：高 {rs['summary']['high']} / 中 {rs['summary']['medium']} / 低 {rs['summary']['low']}")
        lines.append("")
        lines.append("| 标的 | 风险等级 | 风险点 |")
        lines.append("|---|---|---|")
        for it in rs["items"]:
            risks = "；".join(it.get("risks") or []) or "未触发阈值"
            lines.append(f"| {it.get('name', it['symbol'])} | {it['level']} | {compliance.sanitize(risks)} |")
        lines.append("")
        for x in rs.get("assumptions", []):
            lines.append(f"  - 假设：{x}")
    else:
        lines.append("_风险扫描数据不可用。_")
    lines.append("")

    lines.append("## 四、约束假设与置信水平")
    lines.append("")
    lines.append(f"- 团队裁决：**{v.get('verdict')}**，置信度 **{v.get('confidence')}**")
    for x in v.get("assumptions", []):
        lines.append(f"- {x}")
    lines.append("")

    if v.get("conflicts"):
        lines.append("## 五、Kronos 预测冲突（高亮）")
        lines.append("")
        for c in v["conflicts"]:
            lines.append(f"- **{c['name']}**：Agent「{c['agent_verdict']}」 vs Kronos「{c['kronos_prediction']} {c['kronos_change_pct']:+.1f}%」")
        lines.append("")

    # 回测质检（治理模块）
    bq = a.get("backtest_quality") or {}
    if bq.get("items"):
        lines.append("## 六、回测质检（投研结论的历史验证）")
        lines.append("")
        lines.append(f"**结论**：{bq.get('verdict')}")
        lines.append("")
        s = bq.get("summary", {})
        lines.append(f"- 测试标的 {s.get('symbols_tested')} 只｜平均收益 {s.get('avg_total_return_pct')}%"
                     f"｜平均胜率 {s.get('avg_win_rate_pct')}%｜平均最大回撤 {s.get('avg_max_drawdown_pct')}%"
                     f"｜跑赢买入持有 {s.get('beats_buy_and_hold')} 只")
        lines.append("")
        lines.append("| 标的 | 策略收益 | 基准收益 | 胜率 | 最大回撤 | 交易数 |")
        lines.append("|---|---|---|---|---|---|")
        for it in bq["items"]:
            if "error" in it:
                lines.append(f"| {it['symbol']} | 回测失败 | - | - | - | - |")
            else:
                lines.append(f"| {it['name']} | {it['total_return_pct']}% | {it.get('benchmark_return_pct')}% "
                             f"| {it['win_rate_pct']}% | {it['max_drawdown_pct']}% | {it['trades']} |")
        lines.append("")
        for x in bq.get("assumptions", []):
            lines.append(f"  - 假设：{x}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"> **免责声明**：{DISCLAIMER}")
    return "\n".join(lines)


def build_rebalance_json(minutes: dict, rebalance: list) -> dict:
    """结构化调仓清单（下游系统可读）。"""
    v = (minutes.get("rounds") or {}).get("round3_verdict", {})
    return {
        "schema": "kronos.rebalance.v1",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": minutes.get("session_id"),
        "verdict": v.get("verdict"),
        "confidence": v.get("confidence"),
        "disagreements": v.get("disagreements", []),
        "conflicts_with_forecast": v.get("conflicts", []),
        "items": [
            {
                "symbol": it.get("symbol"),
                "name": it.get("name"),
                "direction": it.get("direction"),
                "weight_suggestion": "等权参考（见 assumptions）",
                "price_range": it.get("entry_zone"),
                "stop_loss": it.get("stop_loss"),
                "take_profit": it.get("take_profit"),
                "kronos_change_pct": it.get("kronos_change_pct"),
                "confidence_band_pct": it.get("confidence_band_pct"),
                "reason": compliance.sanitize(it.get("reason") or ""),
                "assumptions": it.get("assumptions", []),
            }
            for it in (rebalance or [])
        ],
        "assumptions": v.get("assumptions", []),
        "disclaimer": DISCLAIMER,
    }


def build_all(minutes: dict) -> dict:
    """一次性生成三类输出物（含专业分析层）。"""
    symbols = minutes.get("symbols", [])
    data = {}
    try:
        data["correlation"] = analytics.correlation_matrix(symbols)
    except Exception as e:
        data["correlation"] = {"error": str(e)}
    try:
        data["attribution"] = analytics.attribution(symbols)
    except Exception as e:
        data["attribution"] = {"error": str(e)}
    try:
        data["monte_carlo"] = analytics.monte_carlo(symbols, n_sim=10000, horizon=30)
    except Exception as e:
        data["monte_carlo"] = {"error": str(e)}
    try:
        data["risk_scan"] = analytics.risk_scan(symbols)
    except Exception as e:
        data["risk_scan"] = {"error": str(e)}
    try:
        rebalance = analytics.build_rebalance_list(symbols)
    except Exception as e:
        rebalance = []
        data["rebalance_error"] = str(e)
    try:
        data["backtest_quality"] = analytics.backtest_quality(symbols)
    except Exception as e:
        data["backtest_quality"] = {"error": str(e)}

    return {
        "minutes_md": build_minutes(minutes),
        "report_md": build_report(minutes, data),
        "rebalance": build_rebalance_json(minutes, rebalance),
        "analytics": data,
        "compliance": compliance.review_minutes(minutes),
    }
