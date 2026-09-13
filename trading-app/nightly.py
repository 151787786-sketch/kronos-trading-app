"""夜间自迭代机制：用真实运行数据（预测准确率 / 回测 / 告警命中 / 自动交易）
让 DeepSeek 提出参数与角色提示词调整建议，经安全钳制后生效并全程留痕。

安全设计（避免模型把参数改坏）：
  * 每个可调参数都有 ALLOWED 范围，超出即钳制
  * 单次调整幅度上限 MAX_STEP
  * 全部建议无论是否生效都写库留痕（iteration_log）
  * 支持一键回滚到默认值

产物：
  * tuning.json   —— 当前生效的参数覆盖值
  * iteration_log 表 —— 每轮迭代的证据、建议、生效结果
"""
import json
import os
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "trade.db")
TUNING_PATH = os.path.join(ROOT, "tuning.json")

_lock = threading.RLock()

# 可调参数：路径 -> (默认值, 最小值, 最大值, 单次最大步长, 说明)
ALLOWED = {
    "alerts.surge_pct":        (3.0, 1.0, 9.0, 0.5, "异动提醒：涨幅阈值 %"),
    "alerts.volume_ratio":     (2.5, 1.2, 8.0, 0.3, "异动提醒：量比阈值"),
    "alerts.near_pct":         (1.5, 0.3, 5.0, 0.3, "接近买/卖点提前预警幅度 %"),
    "alerts.momentum_interval": (60, 30, 600, 30, "异动扫描间隔（秒）"),
    "risk.vol_high":           (45.0, 20.0, 120.0, 5.0, "风险扫描：年化波动率高风险线 %"),
    "risk.drawdown_high":      (-20.0, -60.0, -5.0, 3.0, "风险扫描：最大回撤高风险线 %"),
    "risk.pe_high":            (60.0, 20.0, 300.0, 10.0, "风险扫描：市盈率高风险线"),
    "risk.pb_high":            (8.0, 2.0, 40.0, 1.0, "风险扫描：市净率高风险线"),
    "risk.sentiment_high":     (-0.35, -1.0, -0.05, 0.05, "风险扫描：舆情均值高风险线"),
    "recommend.w_momentum":    (0.3, 0.05, 0.6, 0.05, "荐股权重：动量"),
    "recommend.w_technical":   (0.3, 0.05, 0.6, 0.05, "荐股权重：技术面"),
    "recommend.w_fundamental": (0.2, 0.05, 0.6, 0.05, "荐股权重：基本面"),
    "recommend.w_forecast":    (0.2, 0.05, 0.6, 0.05, "荐股权重：Kronos 预测"),
    "committee.debate_rounds": (16, 4, 40, 4, "投研会：辩论组数上限"),
}

# 角色提示词可迭代（role_id -> 默认补充说明）
ROLE_HINTS = {
    "cio": "", "pm": "", "researcher": "", "risk": "", "compliance": "",
    "trader": "", "industry": "", "macro": "", "quant": "",
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS iteration_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started REAL, finished REAL,
        engine TEXT, status TEXT,
        trigger TEXT,
        evidence TEXT, proposals TEXT, applied TEXT,
        summary TEXT
    );
    """)
    conn.commit()
    conn.close()


# ------------------------------------------------------------- tuning 读写

def _dotted_get(cfg: dict, path: str):
    cur = cfg
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _dotted_set(cfg: dict, path: str, value):
    parts = path.split(".")
    cur = cfg
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def load_tuning() -> dict:
    if os.path.exists(TUNING_PATH):
        try:
            with open(TUNING_PATH, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def save_tuning(cfg: dict) -> None:
    with open(TUNING_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_param(path: str):
    """取生效值（tuning.json 覆盖优先，否则默认值）。"""
    v = _dotted_get(load_tuning(), path)
    return v if v is not None else ALLOWED[path][0]


def clamp(path: str, value) -> float:
    """按允许范围 + 单步幅度钳制建议值。"""
    default, lo, hi, step, _ = ALLOWED[path]
    cur = get_param(path)
    try:
        v = float(value)
    except Exception:
        return cur
    # 单步限制
    if v > cur + step:
        v = cur + step
    if v < cur - step:
        v = cur - step
    v = max(lo, min(hi, v))
    return type(default)(round(v, 4)) if isinstance(default, int) else round(v, 4)


def reset_tuning() -> None:
    if os.path.exists(TUNING_PATH):
        os.remove(TUNING_PATH)


def role_hint(role_id: str) -> str:
    return (load_tuning().get("role_hints") or {}).get(role_id) or ""


# ------------------------------------------------------------- 证据收集

def collect_evidence(symbols=None) -> dict:
    """收集真实运行数据作为迭代依据。"""
    ev = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "items": {}}

    # 1) 预测准确率（滚动样本外）
    try:
        import accuracy
        import account
        syms = symbols or [w["symbol"] for w in account.get_watchlist()][:4]
        accs = []
        for s in syms[:4]:
            try:
                r = accuracy.evaluate(s)
                if r and not r.get("error"):
                    accs.append({"symbol": s,
                                 "mape_pct": r.get("mape_pct"),
                                 "dir_acc_pct": r.get("dir_acc_pct"),
                                 "mae": r.get("mae"),
                                 "rounds": r.get("rounds"),
                                 "pred_change_pct": r.get("avg_pred_change_pct"),
                                 "actual_change_pct": r.get("avg_actual_change_pct")})
            except Exception:
                continue
        ev["items"]["accuracy"] = accs
        valid = [a for a in accs if a.get("mape_pct") is not None]
        if valid:
            ev["items"]["accuracy_summary"] = {
                "avg_mape_pct": round(sum(a["mape_pct"] for a in valid) / len(valid), 2),
                "avg_dir_acc_pct": round(sum(a["dir_acc_pct"] or 0 for a in valid) / len(valid), 2),
                "n_symbols": len(valid),
                "verdict": ("预测误差偏大，应更依赖风控与低波动标的"
                            if sum(a["mape_pct"] for a in valid) / len(valid) > 15 else "预测误差处于可接受区间"),
            }
    except Exception as e:
        ev["items"]["accuracy"] = [{"error": str(e)[:80]}]

    # 2) 告警命中情况
    try:
        import alerts
        st = alerts.stats(days=30) if hasattr(alerts, "stats") else {}
        ev["items"]["alerts"] = st
    except Exception as e:
        ev["items"]["alerts"] = {"error": str(e)[:80]}

    # 3) 自动交易执行情况
    try:
        import auto_trade
        ev["items"]["auto_trade"] = auto_trade.stats(days=30) if hasattr(auto_trade, "stats") else {}
    except Exception as e:
        ev["items"]["auto_trade"] = {"error": str(e)[:80]}

    # 4) 历史迭代记录
    try:
        hist = list_iterations(limit=3)
        ev["items"]["history"] = [{"id": h["id"], "engine": h["engine"], "summary": h["summary"]}
                                  for h in hist]
    except Exception:
        ev["items"]["history"] = []

    # 5) 当前生效参数
    ev["items"]["current"] = {k: get_param(k) for k in ALLOWED}
    return ev


# ------------------------------------------------------------- 建议生成

_SYS = ("你是量化策略参数调优助手。基于真实运行数据（预测误差、告警统计、自动交易统计），"
        "提出参数调整建议，目标是提升信号质量、降低噪声。\n"
        "严格要求：\n"
        "1) 只输出 JSON，不要任何解释文字；\n"
        "2) 提 1~4 条建议，必须给出至少一条（除非所有指标都明显健康）；\n"
        "3) 每条格式：{\"param\":\"参数名\",\"value\":建议值,\"reason\":\"12字以内理由\",\"confidence\":0到1};"
        "\"param\" 必须来自给定清单，\"value\" 必须是数字；\n"
        "4) 不得编造数据，理由只能引用给出的统计；\n"
        "5) 调整幅度要克制（单参数一次只动一小步），并在理由里说明依据。")


def _advice_rules(evidence: dict) -> list:
    """规则兜底建议：即使 LLM 不给建议，也基于统计给出保守调整。"""
    out = []

    def add(param, value, reason, conf):
        out.append({"param": param, "label": ALLOWED[param][4], "from": get_param(param),
                    "to_raw": value, "reason": reason, "confidence": conf})

    al = evidence["items"].get("alerts") or {}
    acc = evidence["items"].get("accuracy_summary") or {}
    if isinstance(al, dict) and al.get("momentum_per_day") is not None:
        mpd = al["momentum_per_day"]
        if mpd == 0 and al.get("days", 0) >= 7:
            add("alerts.surge_pct", max(1.0, _val("alerts.surge_pct") - 0.5),
                "近30天无异动触发，阈值偏高", 0.6)
        elif mpd > 8:
            add("alerts.surge_pct", min(9.0, _val("alerts.surge_pct") + 0.5),
                "异动提醒过频，阈值偏低", 0.6)
    if acc.get("avg_mape_pct") is not None and acc["avg_mape_pct"] > 15:
        add("recommend.w_forecast", max(0.05, _val("recommend.w_forecast") - 0.05),
            f"预测MAPE {acc['avg_mape_pct']}%偏高", 0.65)
        add("recommend.w_technical", min(0.6, _val("recommend.w_technical") + 0.05),
            "降低预测权重，提高技术面权重", 0.6)
    if acc.get("avg_mape_pct") is not None and (acc.get("avg_dir_acc_pct") or 0) < 55:
        add("risk.vol_high", max(20.0, _val("risk.vol_high") - 5.0),
            f"方向准确率 {acc.get('avg_dir_acc_pct')}% 偏低，风控趋严", 0.55)
    return out[:4]


def _val(path):
    return float(get_param(path))


def propose(evidence: dict) -> list:
    import deepseek
    proposals = []
    if deepseek.available():
        proposals = _propose_llm(evidence)
    if not proposals:
        proposals = _advice_rules(evidence)
        for p in proposals:
            p["source"] = "rule"
        return proposals
    for p in proposals:
        p["source"] = "deepseek"
    # 补充规则建议（LLM 未覆盖但统计上明确的问题）
    have = {p["param"] for p in proposals}
    for p in _advice_rules(evidence):
        if p["param"] not in have and len(proposals) < 4:
            p["source"] = "rule"
            proposals.append(p)
    return proposals


def _propose_llm(evidence: dict) -> list:
    import deepseek
    lines = []
    for k, label in [(f"alerts.{x}", ALLOWED[f"alerts.{x}"][4]) for x in
                     ("surge_pct", "volume_ratio", "near_pct", "momentum_interval")]:
        lines.append(f"- {k}: {get_param(k)}  ({label})")
    for x in ("vol_high", "drawdown_high", "pe_high", "pb_high", "sentiment_high"):
        k = f"risk.{x}"
        lines.append(f"- {k}: {get_param(k)}  ({ALLOWED[k][4]})")
    for x in ("w_momentum", "w_technical", "w_fundamental", "w_forecast"):
        k = f"recommend.{x}"
        lines.append(f"- {k}: {get_param(k)}  ({ALLOWED[k][4]})")

    acc = evidence["items"].get("accuracy") or []
    acc_txt = "\n".join(
        f"  {a.get('symbol')}: MAPE {a.get('mape_pct')}% 方向准确率 {a.get('dir_acc_pct')}% "
        f"({a.get('rounds')} 轮样本外) 预测{a.get('pred_change_pct')}% vs 实际{a.get('actual_change_pct')}%"
        for a in acc if isinstance(a, dict) and a.get("mape_pct") is not None) or "  （暂无有效样本）"
    summ = evidence["items"].get("accuracy_summary") or {}
    if summ:
        acc_txt += (f"\n  汇总：平均 MAPE {summ.get('avg_mape_pct')}%，"
                    f"平均方向准确率 {summ.get('avg_dir_acc_pct')}%，判定：{summ.get('verdict')}")

    user = (f"【可调参数清单】\n" + "\n".join(lines) +
            f"\n\n【Kronos 预测样本外准确率】\n{acc_txt}"
            f"\n\n【告警统计（30天）】\n{json.dumps(evidence['items'].get('alerts'), ensure_ascii=False)[:600]}"
            f"\n\n【自动交易统计（30天）】\n{json.dumps(evidence['items'].get('auto_trade'), ensure_ascii=False)[:600]}"
            f"\n\n请给出参数调整建议 JSON 数组（至少 1 条）。")
    res = deepseek.chat_json_list(_SYS, user, temperature=0.2, max_tokens=1200,
                                  tag="nightly", use_cache=False)
    if not res:
        return []
    out = []
    for p in res[:4]:
        if not isinstance(p, dict):
            continue
        key = str(p.get("param") or "")
        if key not in ALLOWED:
            continue
        out.append({"param": key, "label": ALLOWED[key][4],
                    "from": get_param(key), "to_raw": p.get("value"),
                    "reason": str(p.get("reason") or "")[:30],
                    "confidence": p.get("confidence")})
    return out


# ------------------------------------------------------------- 执行迭代

def run_iteration(trigger: str = "manual", symbols=None, dry_run: bool = False) -> dict:
    """执行一次自迭代。返回完整结果（同时写库留痕）。"""
    with _lock:
        init_tables()
        import deepseek
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO iteration_log (started, engine, status, trigger) VALUES (?,?,?,?)",
            (time.time(), "deepseek" if deepseek.available() else "rule", "running", trigger))
        conn.commit()
        rid = cur.lastrowid
        conn.close()

        evidence = collect_evidence(symbols)
        proposals = propose(evidence)
        applied = []
        notes = []
        for p in proposals:
            newv = clamp(p["param"], p["to_raw"])
            if abs(float(newv) - float(p["from"])) < 1e-9:
                notes.append(f"{p['param']} 建议值 {p['to_raw']} 被范围/步长钳制后与原值一致，未改动")
                continue
            applied.append({**p, "to": newv,
                            "clamped": str(p["to_raw"]) != str(newv)})

        if applied and not dry_run:
            cfg = load_tuning()
            for a in applied:
                _dotted_set(cfg, a["param"], a["to"])
            cfg["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cfg["last_iteration"] = rid
            save_tuning(cfg)

        summary = (f"依据 {len(evidence['items'].get('accuracy') or [])} 只标的的样本外误差、"
                   f"告警与自动交易统计，提出 {len(proposals)} 条建议，"
                   f"生效 {len(applied)} 条" + ("（演练模式，未写入）" if dry_run else ""))
        if not deepseek.available():
            summary = "DeepSeek 未配置，本轮未生成建议（仅采集证据）"

        conn = _conn()
        conn.execute("UPDATE iteration_log SET finished=?, status=?, evidence=?, proposals=?, applied=?, summary=? WHERE id=?",
                     (time.time(), "done", json.dumps(evidence, ensure_ascii=False)[:20000],
                      json.dumps({"proposals": proposals, "notes": notes}, ensure_ascii=False)[:20000],
                      json.dumps(applied, ensure_ascii=False)[:10000], summary, rid))
        conn.commit()
        conn.close()
        return {"id": rid, "engine": "deepseek" if deepseek.available() else "rule",
                "evidence": evidence, "proposals": proposals, "applied": applied,
                "notes": notes, "summary": summary, "dry_run": dry_run}


def list_iterations(limit: int = 20) -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute("SELECT id, started, finished, engine, status, trigger, summary "
                        "FROM iteration_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_iteration(rid: int) -> dict:
    init_tables()
    conn = _conn()
    r = conn.execute("SELECT * FROM iteration_log WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not r:
        return {}
    d = dict(r)
    for k in ("evidence", "proposals", "applied"):
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except Exception:
            pass
    return d


def param_table() -> list:
    """全部可调参数当前值 + 默认值（页面展示用）。"""
    rows = []
    for k, (default, lo, hi, step, label) in ALLOWED.items():
        cur = get_param(k)
        rows.append({"param": k, "label": label, "default": default, "current": cur,
                     "min": lo, "max": hi, "step": step,
                     "changed": abs(float(cur) - float(default)) > 1e-9})
    return rows


# 定时线程由 app.py 启动（每天 02:00 执行一次）
def should_run_now(last_run_ts: float, hour: int = 2) -> bool:
    lt = time.localtime()
    if lt.tm_hour != hour:
        return False
    return (time.time() - (last_run_ts or 0)) > 20 * 3600
