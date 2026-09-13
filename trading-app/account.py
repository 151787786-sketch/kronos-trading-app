"""Simulated trading account: multi-account support, cash, positions, orders, PnL.

Multiple independent accounts are supported (each with its own cash/positions/
orders). A per-request "current account" is selected by the Flask layer via
account.set_current_account(); default is account id 1 (legacy).
"""
import json
import os
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "trade.db")

INITIAL_CASH = 1000000.0  # 100万模拟资金
COMMISSION = 0.0003        # 佣金 万三
MIN_COMMISSION = 5.0       # 最低佣金 5 元
STAMP_TAX = 0.0005         # 印花税 万五（卖出）

# per-thread current account selection (fallback to persisted global setting)
_tls = threading.local()


def _persisted_account_id() -> int:
    """Read the persisted current account from the DB (shared across requests)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key='current_account_id'").fetchone()
        conn.close()
        if row:
            return int(row["value"])
    except Exception:
        pass
    return 1


def _save_persisted_account_id(aid: int) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_account_id', ?)",
                     (str(aid),))
        conn.commit()
        conn.close()
    except Exception:
        pass


def set_current_account(account_id: int) -> None:
    _tls.account_id = int(account_id)
    _save_persisted_account_id(int(account_id))


def current_account_id() -> int:
    # per-thread (HTTP request) wins if set explicitly via X-Account-Id;
    # otherwise use the persisted global selection.
    if hasattr(_tls, "account_id"):
        return _tls.account_id
    return _persisted_account_id()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        added_at REAL,
        grp TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT DEFAULT '默认账户',
        cash REAL NOT NULL,
        created_at REAL
    );
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL DEFAULT 1,
        symbol TEXT NOT NULL,
        name TEXT,
        shares REAL NOT NULL,
        avg_cost REAL NOT NULL,
        updated_at REAL,
        UNIQUE (account_id, symbol)
    );
    CREATE TABLE IF NOT EXISTS position_meta (
        symbol TEXT PRIMARY KEY,
        price REAL,
        market_value REAL,
        pnl REAL,
        updated_at REAL
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL DEFAULT 1,
        symbol TEXT NOT NULL,
        name TEXT,
        side TEXT NOT NULL,           -- BUY / SELL
        price REAL NOT NULL,
        shares REAL NOT NULL,
        amount REAL NOT NULL,
        fee REAL NOT NULL,
        ts REAL NOT NULL
    );
    """)
    # migrate legacy single-account tables
    cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()]
    if "grp" not in cols:
        conn.execute("ALTER TABLE watchlist ADD COLUMN grp TEXT DEFAULT ''")
    acols = [r[1] for r in conn.execute("PRAGMA table_info(account)").fetchall()]
    if "name" not in acols:
        conn.execute("ALTER TABLE account ADD COLUMN name TEXT DEFAULT '默认账户'")
        # drop the legacy single-row CHECK constraint via table rebuild
        conn.execute("ALTER TABLE account RENAME TO account_old")
        conn.execute("""
            CREATE TABLE account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '默认账户',
                cash REAL NOT NULL,
                created_at REAL
            )
        """)
        conn.execute("INSERT INTO account (id, name, cash, created_at) SELECT id, COALESCE(name,'默认账户'), cash, created_at FROM account_old")
        conn.execute("DROP TABLE account_old")
    pcols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
    has_account = "account_id" in pcols
    # clean up any leftover rename target from an interrupted migration
    conn.execute("DROP TABLE IF EXISTS positions_old")
    # Only the absence of account_id marks a legacy table. (Checking the autoindex
    # name is unreliable: the new UNIQUE(account_id, symbol) creates the same
    # sqlite_autoindex_positions_1 name, which would re-trigger a rebuild forever.)
    if not has_account:
        # rebuild positions table to get UNIQUE(account_id, symbol)
        conn.execute("ALTER TABLE positions RENAME TO positions_old")
        conn.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL DEFAULT 1,
                symbol TEXT NOT NULL,
                name TEXT,
                shares REAL NOT NULL,
                avg_cost REAL NOT NULL,
                updated_at REAL,
                UNIQUE (account_id, symbol)
            )
        """)
        conn.execute("""
            INSERT INTO positions (account_id, symbol, name, shares, avg_cost, updated_at)
            SELECT 1, symbol, name, shares, avg_cost, updated_at FROM positions_old
        """)
        conn.execute("DROP TABLE positions_old")
    ocols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "account_id" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")
    # positions uniqueness: drop legacy unique(symbol) index if present
    try:
        conn.execute("DROP INDEX IF EXISTS sqlite_autoindex_positions_1")
    except Exception:
        pass
    # ensure default account exists
    row = conn.execute("SELECT id FROM account WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO account (id, name, cash, created_at) VALUES (1, '默认账户', ?, ?)",
                     (INITIAL_CASH, time.time()))
    conn.commit()
    conn.close()


# ---- account management ---------------------------------------------------------


def list_accounts() -> list:
    init_db()
    conn = _conn()
    rows = conn.execute("SELECT id, name, cash, created_at FROM account ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_account(name="新账户", initial_cash=None) -> dict:
    init_db()
    conn = _conn()
    cash = initial_cash if initial_cash is not None else INITIAL_CASH
    cur = conn.execute("INSERT INTO account (name, cash, created_at) VALUES (?,?,?)",
                       (name, cash, time.time()))
    conn.commit()
    acc = conn.execute("SELECT * FROM account WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(acc)


def delete_account(account_id: int) -> dict:
    init_db()
    conn = _conn()
    if account_id == 1:
        conn.close()
        return {"ok": False, "message": "默认账户不可删除"}
    conn.execute("DELETE FROM account WHERE id=?", (account_id,))
    conn.execute("DELETE FROM positions WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM orders WHERE account_id=?", (account_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "message": f"账户 {account_id} 已删除"}


def rename_account(account_id: int, name: str) -> dict:
    init_db()
    conn = _conn()
    conn.execute("UPDATE account SET name=? WHERE id=?", (name, account_id))
    conn.commit()
    conn.close()
    return {"ok": True, "message": f"已重命名为「{name}」"}


def reset_account(account_id: int = None) -> None:
    init_db()
    aid = account_id or current_account_id()
    conn = _conn()
    conn.execute("UPDATE account SET cash=? WHERE id=?", (INITIAL_CASH, aid))
    conn.execute("DELETE FROM positions WHERE account_id=?", (aid,))
    conn.execute("DELETE FROM orders WHERE account_id=?", (aid,))
    conn.commit()
    conn.close()


# ---- account state ---------------------------------------------------------------


def get_account() -> dict:
    init_db()
    aid = current_account_id()
    conn = _conn()
    acc = conn.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone()
    positions = conn.execute("SELECT * FROM positions WHERE account_id=?", (aid,)).fetchall()
    conn.close()
    if not acc:
        return {"cash": 0, "market_value": 0, "total_asset": 0, "total_pnl": 0,
                "positions": [], "account_id": aid, "name": "?"}

    cash = acc["cash"]
    pos_list = []
    market_value = 0.0
    for p in positions:
        pos_list.append({
            "symbol": p["symbol"], "name": p["name"], "shares": p["shares"],
            "avg_cost": p["avg_cost"], "market_price": p["avg_cost"],  # filled later by caller
            "market_value": 0.0, "pnl": 0.0, "pnl_pct": 0.0,
        })
        market_value += p["shares"] * p["avg_cost"]
    return {
        "account_id": aid,
        "name": acc["name"],
        "cash": cash,
        "market_value": market_value,
        "total_asset": cash + market_value,
        "initial_cash": INITIAL_CASH,
        "total_pnl": cash + market_value - INITIAL_CASH,
        "positions": pos_list,
    }


def _fee(amount, is_sell):
    fee = max(amount * COMMISSION, MIN_COMMISSION)
    if is_sell:
        fee += amount * STAMP_TAX
    return round(fee, 2)


def place_order(symbol, name, side, price, shares) -> dict:
    """Place a simulated market order for the current account."""
    init_db()
    aid = current_account_id()
    conn = _conn()
    try:
        acc = conn.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone()
        cash = acc["cash"]
        amount = round(price * shares, 2)
        fee = _fee(amount, is_sell=(side == "SELL"))

        if side == "BUY":
            total = amount + fee
            if total > cash:
                return {"ok": False, "message": f"资金不足：需要 {total:,.2f}，可用 {cash:,.2f}"}
            conn.execute("UPDATE account SET cash=cash-? WHERE id=?", (total, aid))
            pos = conn.execute("SELECT * FROM positions WHERE account_id=? AND symbol=?",
                               (aid, symbol)).fetchone()
            if pos:
                new_shares = pos["shares"] + shares
                new_cost = (pos["avg_cost"] * pos["shares"] + amount + fee) / new_shares
                conn.execute("UPDATE positions SET shares=?, avg_cost=?, updated_at=? WHERE account_id=? AND symbol=?",
                             (new_shares, new_cost, time.time(), aid, symbol))
            else:
                conn.execute("INSERT INTO positions (account_id, symbol, name, shares, avg_cost, updated_at) VALUES (?,?,?,?,?,?)",
                             (aid, symbol, name, shares, (amount + fee) / shares, time.time()))
        else:  # SELL
            pos = conn.execute("SELECT * FROM positions WHERE account_id=? AND symbol=?",
                               (aid, symbol)).fetchone()
            if not pos or pos["shares"] < shares:
                return {"ok": False, "message": f"持仓不足：当前 {pos['shares'] if pos else 0} 股"}
            conn.execute("UPDATE account SET cash=cash+? WHERE id=?", (amount - fee, aid))
            remain = pos["shares"] - shares
            if remain <= 1e-9:
                conn.execute("DELETE FROM positions WHERE account_id=? AND symbol=?", (aid, symbol))
            else:
                conn.execute("UPDATE positions SET shares=?, updated_at=? WHERE account_id=? AND symbol=?",
                             (remain, time.time(), aid, symbol))

        conn.execute("INSERT INTO orders (account_id, symbol, name, side, price, shares, amount, fee, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, symbol, name, side, price, shares, amount, fee, time.time()))
        conn.commit()
        return {"ok": True, "message": f"{'买入' if side=='BUY' else '卖出'} {name} {shares} 股 @ {price:.2f}，手续费 {fee:.2f}"}
    finally:
        conn.close()


def get_orders(limit=100) -> list:
    init_db()
    aid = current_account_id()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT ?", (aid, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_positions() -> list:
    init_db()
    aid = current_account_id()
    conn = _conn()
    rows = conn.execute("SELECT * FROM positions WHERE account_id=?", (aid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_watch(symbol, name, grp="") -> None:
    init_db()
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO watchlist (symbol, name, added_at, grp) VALUES (?,?,?,?)",
                 (symbol, name, time.time(), grp))
    conn.commit()
    conn.close()


def set_watch_group(symbol, grp) -> None:
    init_db()
    conn = _conn()
    conn.execute("UPDATE watchlist SET grp=? WHERE symbol=?", (grp, symbol))
    conn.commit()
    conn.close()


def remove_watch(symbol) -> None:
    init_db()
    conn = _conn()
    conn.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()


def get_watchlist() -> list:
    init_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]
