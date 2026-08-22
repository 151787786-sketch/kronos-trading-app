"""Auto-trading takeover engine: rules that bind a simulated account and
automatically place orders when conditions trigger.

Rule model (SQLite table auto_rules):
  id, account_id, symbol, name,
  trigger_type: 'above_pct' | 'below_pct' | 'signal_buy' | 'signal_sell' | 'volume_surge',
  trigger_value (e.g. +5 for above_pct),
  action: 'BUY' | 'SELL',
  amount_or_shares: shares to trade (0 = use amount_value),
  amount_value: cash amount to use when shares=0 (only for BUY),
  enabled: 0/1, created_at

Trigger log (auto_logs): id, rule_id, account_id, symbol, action, price, shares,
  reason, ts.
"""
import json
import os
import sqlite3
import time

import account

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "trade.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS auto_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL DEFAULT 1,
        symbol TEXT NOT NULL,
        name TEXT DEFAULT '',
        trigger_type TEXT NOT NULL,
        trigger_value REAL DEFAULT 0,
        action TEXT NOT NULL,
        shares REAL DEFAULT 0,
        amount_value REAL DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        created_at REAL
    );
    CREATE TABLE IF NOT EXISTS auto_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id INTEGER,
        account_id INTEGER,
        symbol TEXT,
        action TEXT,
        price REAL,
        shares REAL,
        reason TEXT,
        ok INTEGER DEFAULT 1,
        message TEXT,
        ts REAL
    );
    """)
    conn.commit()
    conn.close()


def add_rule(account_id, symbol, name, trigger_type, trigger_value, action,
             shares=0, amount_value=0) -> dict:
    init_tables()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO auto_rules (account_id, symbol, name, trigger_type, trigger_value, action, shares, amount_value, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,?)",
        (account_id, symbol, name, trigger_type, trigger_value, action, shares, amount_value, time.time()))
    conn.commit()
    conn.close()
    return {"ok": True, "id": cur.lastrowid,
            "message": f"规则已创建：{symbol} {trigger_type}={trigger_value} → {action}"}


def list_rules() -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute("SELECT * FROM auto_rules ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_rule(rule_id, enabled=None) -> None:
    init_tables()
    conn = _conn()
    if enabled is not None:
        conn.execute("UPDATE auto_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))
    conn.commit()
    conn.close()


def delete_rule(rule_id) -> None:
    init_tables()
    conn = _conn()
    conn.execute("DELETE FROM auto_rules WHERE id=?", (rule_id,))
    conn.commit()
    conn.close()


def get_logs(limit=100) -> list:
    init_tables()
    conn = _conn()
    rows = conn.execute("SELECT * FROM auto_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _log(rule, symbol, action, price, shares, reason, ok, message):
    conn = _conn()
    conn.execute(
        "INSERT INTO auto_logs (rule_id, account_id, symbol, action, price, shares, reason, ok, message, ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rule.get("id"), rule.get("account_id"), symbol, action, price, shares,
         reason, 1 if ok else 0, message, time.time()))
    conn.commit()
    conn.close()


def _calc_shares(rule, price, cash):
    """Determine order shares from rule."""
    if rule.get("shares") and rule["shares"] > 0:
        return rule["shares"]
    amount = rule.get("amount_value") or 0
    if amount > 0:
        # round down to lots of 100 within the budget
        shares = int(amount / price / 100) * 100
        return shares
    # default: use 10% of available cash, rounded to lots
    budget = cash * 0.10
    shares = int(budget / price / 100) * 100
    return shares


def check_and_execute(quotes: dict, name_map: dict) -> list:
    """Evaluate all enabled rules against current quotes. Executes orders via the
    rule's account and returns the list of executed events."""
    init_tables()
    conn = _conn()
    rules = [dict(r) for r in conn.execute(
        "SELECT * FROM auto_rules WHERE enabled=1").fetchall()]
    conn.close()

    executed = []
    for rule in rules:
        q = quotes.get(rule["symbol"])
        if not q:
            continue
        price = q.get("price")
        pct = q.get("pct")
        vol_ratio = q.get("volume_ratio") or 0
        if not price or price <= 0:
            continue

        trigger = rule["trigger_type"]
        value = rule.get("trigger_value") or 0
        action = rule["action"]
        symbol = rule["symbol"]
        name = rule.get("name") or name_map.get(symbol, symbol)
        reason = None

        if trigger == "above_pct" and pct is not None and pct >= value:
            reason = f"涨幅 {pct:+.1f}% ≥ {value:+.1f}%"
        elif trigger == "below_pct" and pct is not None and pct <= value:
            reason = f"跌幅 {pct:+.1f}% ≤ {value:+.1f}%"
        elif trigger == "volume_surge" and vol_ratio >= value:
            reason = f"量比 {vol_ratio:.1f} ≥ {value:.1f}"
        elif trigger == "signal_buy" and pct is not None and pct > 0 and vol_ratio >= 1.5:
            reason = f"Kronos/量价信号偏多（涨幅 {pct:+.1f}%，量比 {vol_ratio:.1f}）"
        elif trigger == "signal_sell" and pct is not None and pct < -3:
            reason = f"走弱信号（跌幅 {pct:+.1f}%）"

        if not reason:
            continue

        # execute on the rule's account
        prev = account.current_account_id()
        account.set_current_account(rule["account_id"])
        try:
            if action == "BUY":
                acc = account.get_account()
                shares = _calc_shares(rule, price, acc.get("cash", 0))
                if shares <= 0:
                    _log(rule, symbol, action, price, 0, reason, False, "可买股数不足一手")
                    continue
                res = account.place_order(symbol, name, "BUY", price, shares)
            else:
                pos = account.get_positions()
                held = next((p for p in pos if p["symbol"] == symbol), None)
                if not held:
                    continue
                shares = rule.get("shares") or held["shares"]
                if shares > held["shares"]:
                    shares = held["shares"]
                if shares <= 0:
                    continue
                res = account.place_order(symbol, name, "SELL", price, shares)
        finally:
            account.set_current_account(prev)

        _log(rule, symbol, action, price, shares, reason, res.get("ok", False), res.get("message", ""))
        if res.get("ok"):
            executed.append({"symbol": symbol, "action": action, "price": price,
                             "shares": shares, "reason": reason, "message": res.get("message")})
    return executed
