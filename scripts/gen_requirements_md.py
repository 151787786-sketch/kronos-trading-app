"""从 requirements_map.py 生成 需求对照表.md（保证文档与代码永不脱节）。

用法：python scripts/gen_requirements_md.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "trading-app"))

import requirements_map  # noqa: E402

BADGE = {"done": "✅ 已完成", "partial": "⚠️ 部分实现", "todo": "❌ 未实现"}


def main():
    data = requirements_map.all_requirements()
    s = data["summary"]
    out = []
    out.append("# 需求实现对照表（普惠金融数字员工团队 · 投研智能体）")
    out.append("")
    out.append("> 本表由 `trading-app/requirements_map.py` **自动生成**（`python scripts/gen_requirements_md.py`），"
               "与网页内的「📋 需求实现对照」面板同源，不会出现文档与实现不一致。")
    out.append(">")
    out.append("> **网页内也能看**：打开交易 APP → 顶部「🏛️ 投研会」标签 → 「📋 需求实现对照」→ 点「展开 / 收起」。")
    out.append("> 深链接直达：`http://localhost:7071/?tab=research&req=1`（加 `&sources=1` 会同时刷新外部数据源面板）")
    out.append("")
    out.append(f"**统计：共 {s['total']} 项 · ✅ 已完成 {s['done']} 项（{s['done_pct']}%） · "
               f"⚠️ 部分实现 {s['partial']} 项 · ❌ 未实现 {s['todo']} 项**")
    out.append("")

    for m in data["modules"]:
        out.append(f"## {m['module']}")
        out.append("")
        out.append("| 需求原文 | 实现位置（代码） | 网页可见位置 | 状态 |")
        out.append("| --- | --- | --- | --- |")
        for it in m["items"]:
            cell = BADGE[it["status"]]
            if it.get("gap"):
                cell += f"<br>{it['gap']}"
            out.append(f"| {it['req']} | `{it['where']}` | {it['ui']} | {cell} |")
        out.append("")

    gaps = [(m["module"], it) for m in data["modules"] for it in m["items"]
            if it["status"] != "done"]
    out.append("---")
    out.append("")
    if gaps:
        out.append("## 待补齐清单")
        out.append("")
        out.append("| 所属模块 | 需求 | 说明 |")
        out.append("| --- | --- | --- |")
        for mod, it in gaps:
            out.append(f"| {mod} | {it['req']} | {it.get('gap') or BADGE[it['status']]} |")
        out.append("")
    else:
        out.append("## 待补齐清单")
        out.append("")
        out.append("**无。需求清单全部条目已实现。**")
        out.append("")
        out.append("> 说明：规则引擎与 DeepSeek 两条链路并存——未配置 API key 时全部功能自动回退到"
                   "确定性规则引擎，功能仍可用，只是叙述由模板生成。")
        out.append("")

    out.append("---")
    out.append("")
    out.append("## 附：本次新增的外部数据源")
    out.append("")
    out.append("| 数据源 | 端点 | 用途 |")
    out.append("| --- | --- | --- |")
    out.append("| 东方财富数据中心 | `datacenter-web.eastmoney.com` | CPI / PPI / PMI / GDP / 存款准备金率、F10 财务与行业分类 |")
    out.append("| 中国货币网 | `chinamoney.com.cn` | Shibor 利率曲线 |")
    out.append("| 腾讯行情 | `qt.gtimg.cn` | 个股/指数实时行情、GC001/GC007 交易所资金利率 |")
    out.append("| 腾讯行业板块 | `proxy.finance.qq.com` | 行业涨跌幅/换手/量比/主力资金/涨跌家数 → 景气度 |")
    out.append("| 腾讯财经新闻 | `proxy.finance.qq.com/.../news/info/search` | 个股新闻与研报资讯 |")
    out.append("| 东方财富研报中心 | `reportapi.eastmoney.com` | 券商个股研报 / 行业研报 / 策略报告 |")
    out.append("| 东方财富公告 | `np-anotice-stock.eastmoney.com` | 公司公告 |")
    out.append("| 新浪财经 | `feed.mix.sina.com.cn`、`hq.sinajs.cn` | 财经要闻、外围指数 |")
    out.append("| DeepSeek | `api.deepseek.com` | 9 位数字员工发言/辩论/互评/裁决、报告摘要、点评与解读、夜间自迭代 |")
    out.append("")

    path = os.path.join(ROOT, "需求对照表.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"written: {path}  ({len(out)} lines, {s['total']} items, {s['done']} done)")


if __name__ == "__main__":
    main()
