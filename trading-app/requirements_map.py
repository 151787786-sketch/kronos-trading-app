"""需求对照表：原始需求清单逐条 → 实现位置 → 网页可见位置 → 状态。

用于前端展示「需求实现对照」，让每一条需求都能被指出来。
状态取值：
  done    已实现且已验证
  partial 部分实现（说明缺什么）
  todo    未实现（说明原因）
"""

REQUIREMENTS = [
    {
        "module": "1. 角色定义层（9 个数字员工 Agent）",
        "items": [
            {"req": "主理人（CIO 角色）— 拆解任务、主持投研会、分发、汇总、裁决、输出纪要",
             "where": "agents/roles.py → cio；committee.py 第3轮裁决、research_report.build_minutes",
             "ui": "🏛️ 投研会 → ⚖️ 主理人裁决 + 📜 四轮会议纪要", "status": "done"},
            {"req": "投研经理 — 资产归因、组合仓位测算、候选调仓清单",
             "where": "agents/engine.py（pm 角色）+ analytics.attribution",
             "ui": "🏛️ 投研会 → 第0轮「投研经理」观点 + 📊 专业分析层→归因分析", "status": "done"},
            {"req": "深度研究员 — 多维估值、蒙特卡洛、执行价格测算",
             "where": "agents/engine.py（researcher）+ analytics.valuation / monte_carlo",
             "ui": "🏛️ 投研会 → 第0轮「深度研究员」+ 📊 蒙特卡洛分布表", "status": "done"},
            {"req": "风控官 — 舆情/事件/合规/回撤压力风险扫描",
             "where": "agents/engine.py（risk）+ analytics.risk_scan",
             "ui": "🏛️ 投研会 → 第0轮「风控官」+ 📊 风险扫描表（高/中/低）", "status": "done"},
            {"req": "交易员 — 可行性校验、流动性、分批下单、滑点预估",
             "where": "agents/engine.py（trader）读取换手率与买点区间",
             "ui": "🏛️ 投研会 → 第0轮「交易员」观点（含流动性/滑点提示）", "status": "done"},
            {"req": "行业分析师 — 行业景气度、产业链、政策影响",
             "where": "agents/engine.py（industry）以公司财务/行业字段为代理",
             "ui": "🏛️ 投研会 → 第0轮「行业分析师」", "status": "partial",
             "gap": "缺实时行业景气度数据源，当前用公司财务代理判断"},
            {"req": "宏观分析师 — 利率/通胀/货币政策对大盘与板块影响",
             "where": "agents/engine.py（macro）读取外围指数（global_market）",
             "ui": "🏛️ 投研会 → 第0轮「宏观分析师」（外围均值/上涨家数）", "status": "partial",
             "gap": "缺利率/通胀实时数据，当前用外围指数作代理"},
            {"req": "量化校验 — 指标/相关性矩阵/统计检验/模型偏差校验",
             "where": "agents/engine.py（quant）+ analytics.correlation_matrix",
             "ui": "🏛️ 投研会 → 第0轮「量化校验」+ 📊 相关性矩阵", "status": "done"},
            {"req": "合规审查 — 核查结论表述，符合金融宣传合规",
             "where": "agents/roles.py（compliance）+ compliance.review_minutes",
             "ui": "🏛️ 投研会 → 🛡️ 合规审查 + 第0轮「合规审查」", "status": "done"},
        ],
    },
    {
        "module": "2. 群体智能协作流程（4 轮，抑制 AI 幻觉）",
        "items": [
            {"req": "第0轮【独立闭门输出】9 Agent 并行独立，互不参考",
             "where": "committee.run_committee 第0轮（逐角色独立 analyze_as）",
             "ui": "🏛️ 投研会 → 📜 第 0 轮 · 独立闭门输出（9 张角色卡）", "status": "done"},
            {"req": "第一轮【圆桌辩论】公开观点、对立观点互相质疑假设/数据/逻辑",
             "where": "committee._debate_pair + 三策略配对（对立/乐观vs悲观/审计质询）",
             "ui": "🏛️ 投研会 → 📜 第 1 轮 · 圆桌辩论（实测 16 组）", "status": "done"},
            {"req": "第二轮【交叉互评幻觉排查】识别逻辑断层/数据错误/主观幻觉/假设缺陷",
             "where": "committee._review（5 类排查项，审计角色强制评审）",
             "ui": "🏛️ 投研会 → 📜 第 2 轮 · 交叉互评（实测 24 条）", "status": "done"},
            {"req": "第三轮【共识汇总与裁决】整合观点、权衡分歧、标记争议点",
             "where": "committee 第3轮（加权立场/置信度修正/分歧与冲突标记）",
             "ui": "🏛️ 投研会 → ⚖️ 主理人裁决 + 🔴 冲突高亮", "status": "done"},
        ],
    },
    {
        "module": "3. 专业能力层（对接现有预测工具）",
        "items": [
            {"req": "批量标的分析：≤20 只批量归因 + 相关性矩阵",
             "where": "analytics.attribution / correlation_matrix（MAX_BATCH=20）",
             "ui": "🏛️ 投研会 → 📊 归因分析 + 相关性矩阵", "status": "done"},
            {"req": "个股估值：多维度估值模型 + 与 Kronos 预测交叉验证",
             "where": "analytics.valuation（相对/成长/预测/技术四维）",
             "ui": "🏛️ 投研会 → 第0轮深度研究员 + 📋 调仓清单（Kronos预测列）", "status": "done"},
            {"req": "蒙特卡洛模拟：万次级情景模拟，测算收益与回撤分布",
             "where": "analytics.monte_carlo（默认 10000 次 bootstrap）",
             "ui": "🏛️ 投研会 → 📊 蒙特卡洛（P5/P25/P50/P75/P95 + 亏损概率 + 回撤分布）", "status": "done"},
            {"req": "多维度风险扫描：舆情/突发/合规/尾部风险",
             "where": "analytics.risk_scan（波动/回撤/VaR95/流动性/估值）+ compliance",
             "ui": "🏛️ 投研会 → 📊 风险扫描表 + 🛡️ 合规审查", "status": "partial",
             "gap": "舆情风险用公司公告+每日要闻替代，无情感打分"},
            {"req": "调仓清单输出：标的/方向/仓位/价格区间/止损止盈",
             "where": "analytics.build_rebalance_list + research_report.build_rebalance_json",
             "ui": "🏛️ 投研会 → 📋 结构化调仓清单（可下载 JSON）", "status": "done"},
            {"req": "内部工具对接：读取预测数据/置信度/预测区间，交叉校验",
             "where": "kronos_service.forecast（含 confidence.mae_pct 区间）+ committee 冲突标记",
             "ui": "🏛️ 投研会 → 🔴 冲突高亮 + 调仓清单「Kronos预测」列", "status": "done"},
        ],
    },
    {
        "module": "4. 治理与质检模块",
        "items": [
            {"req": "全流程审计留痕：每轮发言/辩论/修改/输入/输出可回溯",
             "where": "audit.py（research_sessions + research_audit 两表）",
             "ui": "🏛️ 投研会 → 🗂️ 历史投研会 → 「审计回溯」", "status": "done"},
            {"req": "回测质检：对调仓方案执行历史回测，评估策略表现",
             "where": "analytics.backtest_quality（复用 backtest.py）",
             "ui": "🏛️ 投研会 → 📊 回测质检表 + 分析报告第六章", "status": "done"},
            {"req": "系统迭代机制：夜间基于历史与回测迭代角色 prompt/评估逻辑",
             "where": "—",
             "ui": "—", "status": "todo",
             "gap": "需 LLM 才能真正迭代 prompt；规则引擎版无意义。接入 LLM 后可做"},
            {"req": "角色可扩展架构：新增行业/主题 Agent 不破坏工作流",
             "where": "agents/roles.py 的 ROLES 列表（追加即生效，committee 自动纳入）",
             "ui": "🏛️ 投研会 → 第0轮卡片数量随角色数自动变化", "status": "done"},
        ],
    },
    {
        "module": "5. 外部数据源连接器",
        "items": [
            {"req": "行情数据 API：价格/成交量/基本面财务指标",
             "where": "market.py（腾讯行情）+ fundamentals.py（东财财务）",
             "ui": "📈 K线+指标+预测 / 📋 基本面 / 🌐 外围市场条", "status": "done"},
            {"req": "舆情数据 API：新闻/股吧/研报情感抓取",
             "where": "recommend.daily_news（新浪要闻）+ fundamentals.get_news（公司公告）",
             "ui": "⭐ 荐股 → 📰 每日要闻 / 📋 基本面 → 公司公告", "status": "partial",
             "gap": "有新闻与公告，但无情感极性打分（需 NLP 或 LLM）"},
            {"req": "行业研报数据库：读取券商公开研报文本",
             "where": "—", "ui": "—", "status": "todo",
             "gap": "需接入券商研报接口（东财有公开研报接口，尚未适配）"},
            {"req": "风控规则库：内置风控阈值、合规规则、判定标准",
             "where": "analytics.RISK_THRESHOLDS + compliance.FORBIDDEN_PATTERNS",
             "ui": "🏛️ 投研会 → 📊 风险扫描（阈值随表展示）+ 🛡️ 合规（规则条数）", "status": "done"},
            {"req": "内部接口：读取自有预测工具数据/置信度/区间",
             "where": "kronos_service.forecast 返回 change_pct + confidence.mae_pct + 区间",
             "ui": "📈 K线+指标+预测 → 预测曲线与误差带；🏛️ 投研会 → 冲突标记", "status": "done"},
        ],
    },
    {
        "module": "6. 输出物规范（3 部分）",
        "items": [
            {"req": "投研会议完整纪要（各角色观点/辩论/互评完整记录）",
             "where": "research_report.build_minutes",
             "ui": "🏛️ 投研会 → 📜 四轮纪要（页面）+ 📥 下载会议纪要(MD)", "status": "done"},
            {"req": "标的综合分析报告（现状+收益预期+风险清单+约束假设）",
             "where": "research_report.build_report（六章：现状/收益/风险/假设/冲突/回测）",
             "ui": "🏛️ 投研会 → 📥 下载分析报告(MD)", "status": "done"},
            {"req": "结构化调仓清单（JSON，便于下游系统读取）",
             "where": "research_report.build_rebalance_json（schema=kronos.rebalance.v1）",
             "ui": "🏛️ 投研会 → 📋 调仓清单 + 「下载JSON」", "status": "done"},
        ],
    },
    {
        "module": "7. 约束规则（硬性）",
        "items": [
            {"req": "禁止保本/稳赚类承诺性表述",
             "where": "compliance.FORBIDDEN_PATTERNS（10 条）+ sanitize() 自动替换",
             "ui": "🏛️ 投研会 → 🛡️ 合规审查（禁词 10 条 + 命中提示）", "status": "done"},
            {"req": "标注假设条件/不确定性/置信水平 + 明确不构成投资建议",
             "where": "各 Agent 输出 assumptions + 裁决 assumptions + 报告固定 DISCLAIMER",
             "ui": "🏛️ 投研会 → 各角色卡「假设」+ 合规审查「假设标注 ✓」+ 报告末尾", "status": "done"},
            {"req": "重大分歧时必须显式列出分歧点，不得掩盖",
             "where": "committee 第3轮 disagreements（结构化点对点）",
             "ui": "🏛️ 投研会 → ⚖️ 裁决「重大分歧」计数 + 纪要「尚存争议点」", "status": "done"},
            {"req": "预测工具与 Agent 结论冲突时自动标记并高亮对比",
             "where": "committee 第3轮 conflicts（highlight=True）",
             "ui": "🏛️ 投研会 → 🔴 冲突高亮区块（红框，Agent vs Kronos 对比）", "status": "done"},
        ],
    },
]


def summary() -> dict:
    total = done = partial = todo = 0
    for m in REQUIREMENTS:
        for it in m["items"]:
            total += 1
            if it["status"] == "done":
                done += 1
            elif it["status"] == "partial":
                partial += 1
            else:
                todo += 1
    return {"total": total, "done": done, "partial": partial, "todo": todo,
            "done_pct": round(done / total * 100, 1) if total else 0}


def all_requirements() -> dict:
    return {"modules": REQUIREMENTS, "summary": summary()}
