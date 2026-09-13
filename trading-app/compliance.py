"""合规审查：禁词检测 + 假设/置信度标注校验 + 分歧显式校验。

约束规则（来自需求）：
  1. 禁止输出保本、稳赚类承诺性金融表述
  2. 所有结论必须标注假设条件、不确定性、预测置信水平，并提示"不构成投资建议"
  3. 重大分歧必须显式列出（不得掩盖）
  4. Kronos 预测与 Agent 结论冲突时自动标记并在报告高亮
"""
import re

# 承诺性/绝对化表述（禁词表）
FORBIDDEN_PATTERNS = [
    (r"保本", "承诺保本"),
    (r"稳赚", "承诺稳赚"),
    (r"包赚", "承诺包赚"),
    (r"必涨|必定上涨|肯定涨", "绝对化上涨表述"),
    (r"必跌|必定下跌|肯定跌", "绝对化下跌表述"),
    (r"零风险|无风险|没有风险", "风险绝对化表述"),
    (r"保证收益|确保收益|稳赚不赔", "保证收益表述"),
    (r"翻倍|暴富|一夜", "诱导性收益表述"),
    (r"内幕|内幕消息", "违规信息表述"),
    (r"荐股必赢|必赢", "诱导性表述"),
]

REQUIRED_DISCLAIMER = "不构成投资建议"
REQUIRED_ITEMS = ["假设", "置信", "不确定"]

# 否定语境前缀：出现这些词时，紧随其后的禁词属于「规则声明」而非承诺
NEGATION_PREFIXES = [
    "禁止", "不得", "严禁", "不可", "不能", "杜绝", "避免", "拒绝",
    "不承诺", "不保证", "不构成", "无", "勿", "切莫", "切勿",
    "检测到", "已删除", "违规", "排查", "核查",
]


def _is_negated(text: str, start: int, window: int = 8) -> bool:
    """Check whether the match at `start` is inside a negation/rule-declaration context."""
    ctx = text[max(0, start - window):start]
    return any(p in ctx for p in NEGATION_PREFIXES)


def scan_text(text: str, skip_negated: bool = True) -> dict:
    """Scan a text for compliance violations.

    skip_negated=True: ignore forbidden words that appear in a negation/rule
    context (e.g. "禁止保本", "不得承诺稳赚") — these are declarations, not promises.
    """
    hits = []
    for pat, desc in FORBIDDEN_PATTERNS:
        for m in re.finditer(pat, text or ""):
            if skip_negated and _is_negated(text, m.start()):
                continue
            hits.append({"pattern": pat, "desc": desc, "match": m.group(0),
                         "context": (text[max(0, m.start() - 20):m.end() + 20])})
    return {"ok": not hits, "hits": hits}


def scan_payload(payload) -> dict:
    """Recursively scan strings in a payload."""
    texts = []

    def walk(o):
        if isinstance(o, str):
            texts.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(payload)
    all_hits = []
    for t in texts:
        r = scan_text(t)
        all_hits.extend(r["hits"])
    return {"ok": not all_hits, "hits": all_hits, "scanned_texts": len(texts)}


def review_minutes(minutes: dict) -> dict:
    """Full compliance review over a committee result."""
    issues = []

    # 1) 禁词扫描（全部轮次）
    scan = scan_payload(minutes)
    if not scan["ok"]:
        for h in scan["hits"][:20]:
            issues.append({
                "level": "严重",
                "type": "违规表述",
                "detail": f"检测到「{h['match']}」（{h['desc']}）：…{h['context']}…",
            })

    verdict = (minutes.get("rounds") or {}).get("round3_verdict") or {}

    # 2) 假设与置信度标注
    if not verdict.get("assumptions"):
        issues.append({"level": "严重", "type": "缺少假设标注",
                       "detail": "裁决结论未标注假设条件"})
    if verdict.get("confidence") is None:
        issues.append({"level": "严重", "type": "缺少置信度",
                       "detail": "裁决结论未给出置信水平"})

    # 3) 免责声明
    vtext = verdict.get("text") or ""
    if REQUIRED_DISCLAIMER not in vtext and REQUIRED_DISCLAIMER not in str(verdict.get("assumptions")):
        issues.append({"level": "中等", "type": "缺少免责声明",
                       "detail": f"报告需明确提示「{REQUIRED_DISCLAIMER}」（生成报告时会自动补全）"})

    # 4) 分歧显式化
    disagreements = verdict.get("disagreements") or []
    if disagreements:
        listed = all("point" in d for d in disagreements)
        if not listed:
            issues.append({"level": "严重", "type": "分歧未显式列出",
                           "detail": f"存在 {len(disagreements)} 组分歧但未结构化列出"})

    # 5) 冲突标记
    conflicts = verdict.get("conflicts") or []
    if conflicts:
        if not all(c.get("highlight") for c in conflicts):
            issues.append({"level": "中等", "type": "冲突未高亮",
                           "detail": f"{len(conflicts)} 项与 Kronos 预测冲突，需高亮标记"})

    # 6) 各 Agent 已声明假设
    round0 = (minutes.get("rounds") or {}).get("round0_independent") or []
    no_assumption = [o["role_name"] for o in round0 if not o.get("assumptions")]
    if no_assumption:
        issues.append({"level": "轻微", "type": "个别角色缺假设声明",
                       "detail": f"{'、'.join(no_assumption)} 未声明假设条件"})

    ok = not any(i["level"] == "严重" for i in issues)
    return {
        "ok": ok,
        "issues": issues,
        "summary": (
            "合规审查通过" if ok
            else f"合规审查发现 {len([i for i in issues if i['level']=='严重'])} 项严重问题，需修订"
        ),
        "checked": {
            "forbidden_patterns": len(FORBIDDEN_PATTERNS),
            "scanned_texts": scan.get("scanned_texts", 0),
            "has_assumptions": bool(verdict.get("assumptions")),
            "has_confidence": verdict.get("confidence") is not None,
            "disagreements": len(disagreements),
            "conflicts": len(conflicts),
        },
    }


def sanitize(text: str) -> str:
    """Replace forbidden wording with compliant alternatives."""
    if not text:
        return text
    out = text
    replacements = [
        (r"保本", "（不得承诺保本，已删除）"),
        (r"稳赚不赔|稳赚|包赚", "（不得承诺收益，已删除）"),
        (r"必涨|必定上涨|肯定涨", "可能上行"),
        (r"必跌|必定下跌|肯定跌", "可能下行"),
        (r"零风险|无风险|没有风险", "风险相对可控（仍存在风险）"),
        (r"保证收益|确保收益", "（不得保证收益，已删除）"),
    ]
    for pat, rep in replacements:
        out = re.sub(pat, rep, out)
    return out
