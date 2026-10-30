"""预警快照 · 预警时间轴 · 日报统计 · 用户反馈闭环

需求清单 V3.0 / P2 模块。

设计要点
--------
1. **快照**：二级及以上预警触发时，自动把当时的 K 线窗口与指标落库（JSON），
   便于事后复盘「当时到底长什么样」。快照存**数据**而非图片——数据可以
   重新渲染、可以参与统计，图片不能。
2. **时间轴**：把当日所有预警按时间排序，前端在 K 线图下方独立呈现，点击跳转。
3. **日报**：收盘后按日聚合——次数、类型分布、方向分布、准确率回溯。
4. **反馈闭环**：每条预警可评价「有用 / 无用 / 晚了」，反馈参与后续校准数据源。

诚实声明
--------
* 日报里的「事后验证」需要预警之后的价格数据；若预警发生时间距数据末端太近，
  该项标为「待验证」而不是硬算一个数。
* 反馈统计只反映**用户主观评价**，不等于真实准确率，两者在输出中分列，
  不混为一谈。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "trade.db")

#: 预警级别（二级=警告级 起才存快照）
LEVEL_LABELS = {
    1: "关注级",
    2: "警告级",
    3: "危险/机会级",
}

#: 反馈选项
FEEDBACK_OPTIONS = {
    "useful": "有用",
    "useless": "无用",
    "late": "晚了",
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS alert_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        name TEXT,
        event_type TEXT NOT NULL,
        direction TEXT,
        level INTEGER NOT NULL,
        price REAL,
        score REAL,
        ts REAL NOT NULL,
        ts_label TEXT,
        reason TEXT,
        kline TEXT,        -- JSON: 快照时点的 K 线窗口
        indicators TEXT,   -- JSON: 关键指标
        evidence TEXT,     -- JSON: 依据卡片（因子/风险/溯源）
        summary TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_snap_ts ON alert_snapshots (ts DESC);
    CREATE INDEX IF NOT EXISTS idx_snap_symbol ON alert_snapshots (symbol, ts DESC);

    CREATE TABLE IF NOT EXISTS alert_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id INTEGER,
        symbol TEXT,
        event_type TEXT,
        verdict TEXT NOT NULL,      -- useful / useless / late
        note TEXT,
        ts REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_fb_snap ON alert_feedback (snapshot_id);
    CREATE INDEX IF NOT EXISTS idx_fb_ts ON alert_feedback (ts DESC);
    """)

    # 迁移：早期版本错误地加了 UNIQUE(snapshot_id, ts)，会让「同一秒内连点两个评价」
    # 直接抛 IntegrityError。用户快速连点是完全正常的操作，必须允许。
    # 这里检测到旧表约束就重建（保留数据）。
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_feedback'"
        ).fetchone()
        if row and "UNIQUE" in (row[0] or "").upper():
            conn.executescript("""
            ALTER TABLE alert_feedback RENAME TO alert_feedback_old;
            CREATE TABLE alert_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER,
                symbol TEXT,
                event_type TEXT,
                verdict TEXT NOT NULL,
                note TEXT,
                ts REAL NOT NULL
            );
            INSERT INTO alert_feedback (id, snapshot_id, symbol, event_type, verdict, note, ts)
                SELECT id, snapshot_id, symbol, event_type, verdict, note, ts
                FROM alert_feedback_old;
            DROP TABLE alert_feedback_old;
            CREATE INDEX IF NOT EXISTS idx_fb_snap ON alert_feedback (snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_fb_ts ON alert_feedback (ts DESC);
            """)
    except Exception:
        pass

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def _indicators_from_bars(bars: list) -> dict:
    """从 K 线窗口算一组关键指标（快照用，纯函数、可复算）。"""
    if not bars or len(bars) < 2:
        return {}
    closes = [float(b.get("close") or 0) for b in bars]
    highs = [float(b.get("high") or 0) for b in bars]
    lows = [float(b.get("low") or 0) for b in bars]
    vols = [float(b.get("volume") or 0) for b in bars]
    last = closes[-1]
    prev = closes[-2]
    out = {
        "close": round(last, 3),
        "chg_1bar_pct": round((last - prev) / prev * 100, 3) if prev else None,
        "chg_window_pct": round((last - closes[0]) / closes[0] * 100, 3) if closes[0] else None,
        "high": round(max(highs), 3),
        "low": round(min(lows), 3),
        "amp_window_pct": round((max(highs) - min(lows)) / min(lows) * 100, 3) if min(lows) else None,
        "vol_avg": round(sum(vols) / len(vols), 1) if vols else None,
        "vol_last": round(vols[-1], 1) if vols else None,
        "vol_ratio_last": round(vols[-1] / (sum(vols) / len(vols)), 2)
                           if vols and sum(vols) > 0 else None,
        "bars": len(bars),
    }
    return out


def save_snapshot(symbol, name, event_type, direction, level, price, ts,
                  reason="", score=None, bars=None, evidence=None,
                  window: int = 120, summary=None) -> dict | None:
    """保存一条预警快照。level < 2 的预警不存（需求：二级及以上才存）。

    返回 {"id":.., "saved":True} 或 None（级别不足）。
    """
    if int(level or 0) < 2:
        return None
    init_tables()

    kline = None
    indicators = None
    if bars:
        window_bars = bars[-window:]
        kline = json.dumps(window_bars, ensure_ascii=False)
        indicators = json.dumps(_indicators_from_bars(window_bars), ensure_ascii=False)

    ts_label = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else \
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = _conn()
    cur = conn.execute(
        """INSERT INTO alert_snapshots
           (symbol, name, event_type, direction, level, price, score, ts, ts_label,
            reason, kline, indicators, evidence, summary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (symbol, name, event_type, direction, int(level), price, score, ts, ts_label,
         reason, kline, indicators,
         json.dumps(evidence, ensure_ascii=False) if evidence else None,
         summary))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return {"id": sid, "saved": True}


def _row_to_snapshot(r, with_payload=True) -> dict:
    d = {
        "id": r["id"], "symbol": r["symbol"], "name": r["name"],
        "event_type": r["event_type"], "direction": r["direction"],
        "level": r["level"], "level_label": LEVEL_LABELS.get(r["level"], "未知"),
        "price": r["price"], "score": r["score"], "ts": r["ts"],
        "ts_label": r["ts_label"], "reason": r["reason"], "summary": r["summary"],
    }
    if with_payload:
        for key in ("kline", "indicators", "evidence"):
            raw = r[key]
            try:
                d[key] = json.loads(raw) if raw else None
            except Exception:
                d[key] = None
    return d


def list_snapshots(symbol=None, day=None, limit=50, with_payload=False) -> list:
    init_tables()
    conn = _conn()
    sql = "SELECT * FROM alert_snapshots WHERE 1=1"
    args = []
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol)
    if day:
        start = datetime.strptime(day, "%Y-%m-%d").timestamp()
        sql += " AND ts>=? AND ts<?"
        args += [start, start + 86400]
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [_row_to_snapshot(r, with_payload) for r in rows]


def get_snapshot(sid: int) -> dict | None:
    init_tables()
    conn = _conn()
    r = conn.execute("SELECT * FROM alert_snapshots WHERE id=?", (sid,)).fetchone()
    if not r:
        conn.close()
        return None
    out = _row_to_snapshot(r, with_payload=True)
    # 附带该快照的反馈
    fbs = conn.execute(
        "SELECT verdict, note, ts FROM alert_feedback WHERE snapshot_id=? ORDER BY ts DESC",
        (sid,)).fetchall()
    conn.close()
    out["feedback"] = [{"verdict": f["verdict"],
                        "verdict_label": FEEDBACK_OPTIONS.get(f["verdict"], f["verdict"]),
                        "note": f["note"], "ts": f["ts"]} for f in fbs]
    return out


def delete_snapshot(sid: int) -> None:
    init_tables()
    conn = _conn()
    conn.execute("DELETE FROM alert_snapshots WHERE id=?", (sid,))
    conn.execute("DELETE FROM alert_feedback WHERE snapshot_id=?", (sid,))
    conn.commit()
    conn.close()


def clear_snapshots() -> int:
    init_tables()
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM alert_snapshots").fetchone()[0]
    conn.execute("DELETE FROM alert_snapshots")
    conn.commit()
    conn.close()
    return n


# ---------------------------------------------------------------------------
# 反馈闭环
# ---------------------------------------------------------------------------

def add_feedback(snapshot_id, verdict, note="", symbol=None, event_type=None) -> dict:
    """记录一条用户反馈。verdict: useful / useless / late。"""
    if verdict not in FEEDBACK_OPTIONS:
        raise ValueError(f"verdict 必须是 {list(FEEDBACK_OPTIONS)} 之一，收到 {verdict!r}")
    init_tables()
    conn = _conn()
    if snapshot_id and (symbol is None or event_type is None):
        r = conn.execute("SELECT symbol, event_type FROM alert_snapshots WHERE id=?",
                         (snapshot_id,)).fetchone()
        if r:
            symbol = symbol or r["symbol"]
            event_type = event_type or r["event_type"]
    cur = conn.execute(
        "INSERT INTO alert_feedback (snapshot_id, symbol, event_type, verdict, note, ts) "
        "VALUES (?,?,?,?,?,?)",
        (snapshot_id, symbol, event_type, verdict, note, time.time()))
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return {"id": fid, "verdict": verdict, "verdict_label": FEEDBACK_OPTIONS[verdict]}


def list_feedback(limit=100) -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM alert_feedback ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    conn.close()
    return [{"id": r["id"], "snapshot_id": r["snapshot_id"], "symbol": r["symbol"],
             "event_type": r["event_type"], "verdict": r["verdict"],
             "verdict_label": FEEDBACK_OPTIONS.get(r["verdict"], r["verdict"]),
             "note": r["note"], "ts": r["ts"],
             "ts_label": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
             if r["ts"] else None} for r in rows]


def feedback_stats(days: int = 30) -> dict:
    """反馈汇总（供校准使用）。

    重要区分：本统计是**用户主观评价**，不是模型真实准确率。
    """
    init_tables()
    since = time.time() - days * 86400
    conn = _conn()
    rows = conn.execute(
        "SELECT verdict, COUNT(*) c FROM alert_feedback WHERE ts>=? GROUP BY verdict",
        (since,)).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) c FROM alert_feedback WHERE ts>=?", (since,)).fetchone()["c"]
    by = {r["verdict"]: r["c"] for r in rows}
    conn.close()
    useful = by.get("useful", 0)
    useless = by.get("useless", 0)
    late = by.get("late", 0)
    rated = useful + useless + late
    if rated == 0:
        suggestion = "暂无反馈样本，校准沿用既有参数"
    elif useful / rated >= 0.6:
        suggestion = "用户认可度较高，可维持当前阈值（甚至略降以提高灵敏度）"
    elif late / rated >= 0.4:
        suggestion = "「晚了」占比偏高，建议提前前置预警时间（调大 lead_minutes）"
    elif useless / rated >= 0.5:
        suggestion = "「无用」占比偏高，建议上调触发阈值或延长冷静期以减少噪声"
    else:
        suggestion = "反馈较为分散，建议继续积累样本"
    return {
        "days": days,
        "total": total,
        "useful": useful,
        "useless": useless,
        "late": late,
        "rated": rated,
        "useful_pct": round(useful / rated * 100, 1) if rated else None,
        "useless_pct": round(useless / rated * 100, 1) if rated else None,
        "late_pct": round(late / rated * 100, 1) if rated else None,
        "suggestion": suggestion,
        "disclaimer": "以上为**用户主观评价**统计，不等于模型真实准确率；"
                      "真实准确率见「预测准确率」与日报的走势回溯。",
    }


# ---------------------------------------------------------------------------
# 日报统计
# ---------------------------------------------------------------------------

def _verify_direction(bars: list, event_ts_label: str, direction: str,
                      horizon: int = 30) -> dict:
    """事后验证：预警方向在之后 horizon 根 K 线内是否兑现。

    找不到足够后验数据时返回 verified=False（「待验证」），不硬算。
    """
    if not bars or not event_ts_label:
        return {"verified": False, "note": "无 K 线可验证"}
    idx = None
    for i, b in enumerate(bars):
        if str(b.get("timestamps", ""))[:16] == event_ts_label[:16]:
            idx = i
            break
    if idx is None:
        return {"verified": False, "note": "预警时点不在当前数据窗口内"}
    if idx + horizon >= len(bars):
        return {"verified": False,
                "note": f"预警后仅有 {len(bars) - idx - 1} 根 K 线，不足 {horizon} 根，待验证"}
    p0 = float(bars[idx].get("close") or 0)
    p1 = float(bars[idx + horizon].get("close") or 0)
    if p0 <= 0:
        return {"verified": False, "note": "基准价无效"}
    chg = (p1 - p0) / p0 * 100
    hit = (direction == "up" and chg > 0) or (direction == "down" and chg < 0)
    return {"verified": True, "hit": bool(hit), "change_pct": round(chg, 2),
            "horizon_min": horizon}


def daily_report(day: str = None, with_verify: bool = True) -> dict:
    """生成某日的预警日报：次数、类型分布、方向分布、走势回溯。

    day 为 'YYYY-MM-DD'，默认今天。
    """
    init_tables()
    if not day:
        day = datetime.now().strftime("%Y-%m-%d")
    try:
        start_dt = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "error": "day 格式应为 YYYY-MM-DD"}
    start = start_dt.timestamp()
    end = start + 86400

    conn = _conn()
    snaps = conn.execute(
        "SELECT * FROM alert_snapshots WHERE ts>=? AND ts<? ORDER BY ts ASC",
        (start, end)).fetchall()
    # 当日全部事件（含级别<2 未存快照的）
    events = conn.execute(
        "SELECT * FROM alert_events WHERE ts>=? AND ts<? ORDER BY ts ASC",
        (start, end)).fetchall()
    fb = conn.execute(
        "SELECT verdict, COUNT(*) c FROM alert_feedback WHERE ts>=? AND ts<? GROUP BY verdict",
        (start, end)).fetchall()
    conn.close()

    by_type, by_symbol, by_direction, by_level = {}, {}, {}, {}
    for r in snaps:
        by_type[r["event_type"]] = by_type.get(r["event_type"], 0) + 1
        key = f"{r['name'] or r['symbol']}({r['symbol']})"
        by_symbol[key] = by_symbol.get(key, 0) + 1
        d = r["direction"] or "unknown"
        by_direction[d] = by_direction.get(d, 0) + 1
        lv = f"{r['level']}级 {LEVEL_LABELS.get(r['level'], '')}".strip()
        by_level[lv] = by_level.get(lv, 0) + 1

    # 走势回溯
    verified, hits = 0, 0
    verify_detail = []
    if with_verify:
        # 按 symbol 分组取 K 线（每组只拉一次）
        need = {}
        for r in snaps:
            need.setdefault(r["symbol"], []).append(r)
        for sym, rs in need.items():
            bars = _bars_for(sym, day)
            if not bars:
                continue
            for r in rs:
                v = _verify_direction(bars, r["ts_label"], r["direction"])
                verify_detail.append({
                    "id": r["id"], "ts_label": r["ts_label"],
                    "symbol": sym, "direction": r["direction"], **v})
                if v.get("verified"):
                    verified += 1
                    if v.get("hit"):
                        hits += 1

    hour_dist = {}
    for r in snaps:
        h = datetime.fromtimestamp(r["ts"]).strftime("%H:00")
        hour_dist[h] = hour_dist.get(h, 0) + 1

    fb_map = {r["verdict"]: r["c"] for r in fb}
    fb_total = sum(fb_map.values())

    return {
        "ok": True,
        "day": day,
        "snapshots": len(snaps),
        "events_total": len(events),
        "by_type": by_type,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda x: -x[1])),
        "by_direction": by_direction,
        "by_level": by_level,
        "hour_distribution": dict(sorted(hour_dist.items())),
        "verification": {
            "attempted": len(verify_detail),
            "verified": verified,
            "pending": len(verify_detail) - verified,
            "hits": hits,
            "accuracy_pct": round(hits / verified * 100, 1) if verified else None,
            "note": ("已验证样本不足，准确率待积累" if verified < 5
                     else f"基于 {verified} 条已验证预警的方向兑现率"),
        },
        "feedback": {
            "total": fb_total,
            "useful": fb_map.get("useful", 0),
            "useless": fb_map.get("useless", 0),
            "late": fb_map.get("late", 0),
        },
        "detail": verify_detail[:60],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _bars_for(symbol: str, day: str) -> list:
    """取某标的当日 1 分钟 K 线（供走势回溯用）。失败返回 []。"""
    try:
        import market
        df = market.fetch_kline(symbol, period="1m", bars=400)
        if df is None or len(df) == 0:
            return []
        df = df.copy()
        df["timestamps"] = df["timestamps"].astype(str)
        sub = df[df["timestamps"].str.startswith(day)]
        use = sub if len(sub) >= 10 else df.tail(240)
        return [{"timestamps": str(r["timestamps"])[:16],
                 "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r["volume"])}
                for _, r in use.iterrows()]
    except Exception:
        return []


def timeline(symbol: str, day: str = None, limit: int = 200) -> dict:
    """预警时间轴：某标的（默认当日）的全部预警点，按时间升序。"""
    init_tables()
    if not day:
        day = datetime.now().strftime("%Y-%m-%d")
    try:
        start = datetime.strptime(day, "%Y-%m-%d").timestamp()
    except ValueError:
        return {"ok": False, "error": "day 格式应为 YYYY-MM-DD"}

    conn = _conn()
    snaps = conn.execute(
        "SELECT id, symbol, name, event_type, direction, level, price, score, ts, "
        "ts_label, reason, summary FROM alert_snapshots "
        "WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts ASC LIMIT ?",
        (symbol, start, start + 86400, int(limit))).fetchall()
    conn.close()

    # 附反馈状态，便于时间轴上直接看到评价
    fb_by_snap = {}
    if snaps:
        conn = _conn()
        ids = [r["id"] for r in snaps]
        q = ",".join("?" * len(ids))
        for r in conn.execute(
                f"SELECT snapshot_id, verdict FROM alert_feedback WHERE snapshot_id IN ({q})",
                ids).fetchall():
            fb_by_snap[r["snapshot_id"]] = r["verdict"]
        conn.close()

    points = []
    for r in snaps:
        points.append({
            "id": r["id"], "ts": r["ts"], "ts_label": r["ts_label"],
            "level": r["level"], "level_label": LEVEL_LABELS.get(r["level"], ""),
            "direction": r["direction"], "price": r["price"], "score": r["score"],
            "event_type": r["event_type"], "reason": r["reason"],
            "summary": r["summary"],
            "feedback": fb_by_snap.get(r["id"]),
        })
    return {"ok": True, "symbol": symbol, "day": day, "count": len(points),
            "points": points}
