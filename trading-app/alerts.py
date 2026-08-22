"""Buy/sell alert engine: persist plans per watchlist symbol, monitor prices,
record trigger history, and fire WeChat notifications.

DB tables:
  alert_plans (symbol PK, plan JSON, updated_at)
  alert_events (id, symbol, name, event_type, price, level, message, ts)
"""
import json
import os
import sqlite3
import time

import notify

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "trade.db")

EVENT_TYPES = {
    "entry": "进入买入区间",
    "stop": "触及止损价",
    "target": "达到目标价",
    "surge": "大幅拉升",
    "volume": "放量异动",
    "near_buy": "接近买点",
    "near_sell": "接近卖点",
    "near_stop": "接近止损",
    "near_target": "接近目标",
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS alert_plans (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        plan TEXT NOT NULL,
        updated_at REAL
    );
    CREATE TABLE IF NOT EXISTS alert_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        name TEXT,
        event_type TEXT NOT NULL,
        price REAL,
        level REAL,
        message TEXT,
        ts REAL
    );
    CREATE INDEX IF NOT EXISTS idx_alert_events_ts ON alert_events (ts DESC);
    """)
    conn.commit()
    conn.close()


def save_plan(symbol, name, plan: dict) -> None:
    init_tables()
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO alert_plans (symbol, name, plan, updated_at) VALUES (?,?,?,?)",
        (symbol, name, json.dumps(plan, ensure_ascii=False), time.time()),
    )
    conn.commit()
    conn.close()


def get_plans() -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute("SELECT * FROM alert_plans ORDER BY updated_at DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            plan = json.loads(r["plan"])
        except Exception:
            plan = {}
        out.append({"symbol": r["symbol"], "name": r["name"],
                    "updated_at": r["updated_at"], "plan": plan})
    return out


def delete_plan(symbol) -> None:
    init_tables()
    conn = _conn()
    conn.execute("DELETE FROM alert_plans WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()


def _last_event(symbol, event_type) -> dict:
    init_tables()
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM alert_events WHERE symbol=? AND event_type=? ORDER BY id DESC LIMIT 1",
        (symbol, event_type)).fetchone()
    conn.close()
    return dict(row) if row else None


def record_event(symbol, name, event_type, price, level, message) -> dict:
    init_tables()
    conn = _conn()
    ts = time.time()
    cur = conn.execute(
        "INSERT INTO alert_events (symbol, name, event_type, price, level, message, ts) VALUES (?,?,?,?,?,?,?)",
        (symbol, name, event_type, price, level, message, ts))
    conn.commit()
    conn.close()
    return {"id": cur.lastrowid, "ts": ts}


def get_events(limit=100) -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute("SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_events() -> None:
    init_tables()
    conn = _conn()
    conn.execute("DELETE FROM alert_events")
    conn.commit()
    conn.close()


def check_plan(symbol, name, plan: dict, price: float) -> dict:
    """Check one plan against the current price. Returns event dict if triggered,
    else None. Deduplicates: same event type for a symbol won't re-fire within
    30 minutes unless the price re-enters the trigger zone."""
    if price is None or price <= 0:
        return None

    now = time.time()
    event = None
    entry_low = plan.get("entry_zone", {}).get("low")
    entry_high = plan.get("entry_zone", {}).get("high")
    stop = plan.get("stop_loss")
    target = plan.get("target_up")

    last = _last_event(symbol, "entry")
    if (entry_low and entry_high and entry_low <= price <= entry_high
            and (not last or now - last["ts"] > 1800)):
        event = ("entry", price, entry_low,
                 f"{name} 现价 {price:.2f} 进入买入区间 {entry_low:.2f}~{entry_high:.2f}，可考虑分批建仓")

    last = _last_event(symbol, "stop")
    if stop and price <= stop and (not last or now - last["ts"] > 1800):
        event = ("stop", price, stop,
                 f"{name} 现价 {price:.2f} 跌破止损价 {stop:.2f}，建议减仓/离场")

    last = _last_event(symbol, "target")
    if target and price >= target and (not last or now - last["ts"] > 1800):
        event = ("target", price, target,
                 f"{name} 现价 {price:.2f} 达到目标价 {target:.2f}，可考虑止盈")

    if not event:
        return None

    etype, price, level, message = event
    ev = record_event(symbol, name, etype, price, level, message)
    ev.update({"event_type": etype, "message": message})
    return ev


def fire_notification(ev: dict) -> None:
    if not notify.is_configured():
        return
    title = f"📈 {ev['name']} {EVENT_TYPES.get(ev['event_type'], ev['event_type'])}"
    content = (f"{ev['message']}\n\n"
               f"股票: {ev['name']} ({ev['symbol']})\n"
               f"触发价: {ev['price']:.2f}\n"
               f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    notify.send(title, content)


# ---------------------------------------------------------------------------
# 盘中异动监控：自选股涨幅/量比异动触发（适合"景气赛道轮动扑击"玩法）
# ---------------------------------------------------------------------------

SURGE_PCT = 3.0       # 涨幅阈值 %
VOLUME_RATIO = 2.5    # 量比阈值
_DEDUP_WINDOW = 1800  # 同类型 30 分钟内不重复


def check_momentum(name_map: dict, quotes: list) -> list:
    """Check watchlist quotes for surge / volume-ratio momentum.
    name_map: {symbol: name}; quotes: list from market.fetch_realtime.
    Returns triggered events (recorded + ready to notify)."""
    events = []
    for q in quotes:
        symbol = q["symbol"]
        name = name_map.get(symbol, q.get("name") or symbol)
        pct = q.get("pct")
        vol_ratio = q.get("volume_ratio") or 0
        price = q.get("price")
        now = time.time()

        # 大幅拉升
        if pct is not None and pct >= SURGE_PCT:
            last = _last_event(symbol, "surge")
            if not last or now - last["ts"] > _DEDUP_WINDOW:
                msg = f"{name} 现价 {price:.2f}，涨幅 {pct:+.1f}%，量比 {vol_ratio:.1f}——可能启动，注意是否轮动到它"
                ev = record_event(symbol, name, "surge", price, pct, msg)
                ev.update({"event_type": "surge", "message": msg})
                events.append(ev)

        # 放量异动（量比高且有一定涨幅）
        if vol_ratio >= VOLUME_RATIO and pct is not None and pct >= 1.0:
            last = _last_event(symbol, "volume")
            if not last or now - last["ts"] > _DEDUP_WINDOW:
                msg = f"{name} 现价 {price:.2f}，量比 {vol_ratio:.1f}（涨幅 {pct:+.1f}%）——明显放量"
                ev = record_event(symbol, name, "volume", price, vol_ratio, msg)
                ev.update({"event_type": "volume", "message": msg})
                events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 买卖点提前预警：现价接近买点/止损/目标时提前提示（阈值 1.5%）
# ---------------------------------------------------------------------------

NEAR_PCT = 1.5          # 距关键价位 1.5% 内视为"接近"
_NEAR_DEDUP = 6 * 3600  # 同类提示 6 小时内不重复


def check_near_plans(plans: list, quotes: list) -> list:
    """plans: alert_plans rows (each has 'plan' JSON dict with entry_zone/
    stop_loss/target_up); quotes: list from market.fetch_realtime.
    Returns early-warning events."""
    events = []
    qmap = {q["symbol"]: q for q in quotes}
    for p in plans:
        try:
            plan = p["plan"] if isinstance(p["plan"], dict) else json.loads(p["plan"])
        except Exception:
            continue
        symbol = p["symbol"]
        name = p.get("name") or symbol
        q = qmap.get(symbol)
        if not q or not q.get("price"):
            continue
        price = q["price"]
        now = time.time()

        entry_zone = plan.get("entry_zone") or {}
        buy_low = entry_zone.get("low")
        buy_high = entry_zone.get("high")
        stop = plan.get("stop_loss")
        target = plan.get("target_up")

        # 接近买入区间（从上方接近上沿）
        if buy_high:
            dist = (price - buy_high) / buy_high * 100
            if 0 < dist <= NEAR_PCT:
                last = _last_event(symbol, "near_buy")
                if not last or now - last["ts"] > _NEAR_DEDUP:
                    msg = (f"{name} 现价 {price:.2f} 距买入区间上沿 {buy_high:.2f} "
                           f"仅 {dist:.1f}%，准备进入买点")
                    ev = record_event(symbol, name, "near_buy", price, buy_high, msg)
                    ev.update({"event_type": "near_buy", "message": msg})
                    events.append(ev)
            # 从下方接近下沿（回踩买点）
            elif buy_low and price > buy_low:
                dist = (price - buy_low) / buy_low * 100
                if 0 < dist <= NEAR_PCT:
                    last = _last_event(symbol, "near_buy")
                    if not last or now - last["ts"] > _NEAR_DEDUP:
                        msg = (f"{name} 现价 {price:.2f} 回踩至买入区间下沿 {buy_low:.2f} "
                               f"附近（距 {dist:.1f}%），低吸机会")
                        ev = record_event(symbol, name, "near_buy", price, buy_low, msg)
                        ev.update({"event_type": "near_buy", "message": msg})
                        events.append(ev)

        # 接近止损
        if stop and price > stop:
            dist = (price - stop) / stop * 100
            if dist <= NEAR_PCT:
                last = _last_event(symbol, "near_stop")
                if not last or now - last["ts"] > _NEAR_DEDUP:
                    msg = (f"{name} 现价 {price:.2f} 逼近止损价 {stop:.2f}（距 {dist:.1f}%），"
                           f"注意防守")
                    ev = record_event(symbol, name, "near_stop", price, stop, msg)
                    ev.update({"event_type": "near_stop", "message": msg})
                    events.append(ev)

        # 接近目标价
        if target and price < target:
            dist = (target - price) / target * 100
            if dist <= NEAR_PCT:
                last = _last_event(symbol, "near_target")
                if not last or now - last["ts"] > _NEAR_DEDUP:
                    msg = (f"{name} 现价 {price:.2f} 逼近目标价 {target:.2f}（距 {dist:.1f}%），"
                           f"考虑止盈")
                    ev = record_event(symbol, name, "near_target", price, target, msg)
                    ev.update({"event_type": "near_target", "message": msg})
                    events.append(ev)
    return events
