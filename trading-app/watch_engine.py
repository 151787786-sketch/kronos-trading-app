"""多周期轮询引擎 · 红绿频闪预警规则

需求「交易舱 v1」P0 的核心。

周期模型（按你确认的口径）
--------------------------
* **请求节流 500ms**：底层 `tencent_quote.RateLimiter` 保证两次 HTTP 起点
  之间至少间隔 500 毫秒（跨线程共享），防止批量把接口打爆。
* **预警周期 1/3/5/10 分钟**：每只股票各自配置一个「注意力周期」，
  表示"多久检查一次该股是否触发买卖点预警"。
* 「默认 30 秒 / 实验档 1 秒」通过 `tick_interval_sec` 体现：引擎每秒
  tick 一次，判断哪些股票到了自己的检查时点。这样 1 分钟周期的股票
  不会因为 tick 密集而被过度请求（命中 5 秒行情缓存）。

掉线自动重连
------------
`tencent_quote.QuoteClient` 内部已做重试与退避；引擎层再叠加：
* 连续失败计数 → 指数退避（上限 60 秒）
* 恢复后自动回到正常节奏，并记录一次 `recovered` 事件
* 任何单次失败都不会让循环退出

红绿频闪（A 股惯例）
--------------------
* **买点 → 绿闪**（绿=买入/下跌到位）
* **卖点 → 红闪**（红=卖出/上涨到位）
* 预警必须在该股的**一个周期内**触发：引擎每 tick 都会重新评估，
  而 tick 间隔（1 秒）远小于最小周期（60 秒），故最坏延迟 ≈ 1 秒 + 行情缓存。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import tencent_quote as tq

#: 允许的预警周期（分钟）
ALLOWED_PERIODS = (1, 3, 5, 10)

#: 默认周期（分钟）
DEFAULT_PERIOD = 5

#: 引擎 tick 间隔（秒）—— 需求："默认 30 秒，实验档 1 秒"
DEFAULT_TICK_SEC = 1.0

#: 预警级别
LEVELS = {1: "关注级", 2: "警告级", 3: "危险级"}

#: 默认触发规则（可按股覆盖）
DEFAULT_RULES = {
    "pct_up": 3.0,        # 涨幅 >= 此值 → 卖点预警（红闪）
    "pct_down": -3.0,     # 跌幅 <= 此值 → 买点预警（绿闪）
    "vol_ratio": 2.5,     # 量比 >= 此值 → 放量确认
    "turnover_spike": 5.0,  # 换手率 >= 此值 → 活跃度异常
}


# ---------------------------------------------------------------------------
# 配置存储
# ---------------------------------------------------------------------------

class WatchConfig:
    """盯盘配置：每只股票的周期与规则，落盘到 JSON。"""

    def __init__(self, path: str = None):
        import os
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "watch_config.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        import json
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception:
            d = {}
        d.setdefault("tick_sec", DEFAULT_TICK_SEC)
        d.setdefault("enabled", True)
        d.setdefault("items", {})   # symbol -> {period_min, rules, name}
        return d

    def save(self) -> None:
        import json
        with self._lock:
            data = self._data
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def all(self) -> dict:
        with self._lock:
            return {"tick_sec": self._data["tick_sec"],
                    "enabled": self._data["enabled"],
                    "items": dict(self._data["items"])}

    def get(self, symbol: str) -> dict:
        code = tq.bare_code(symbol)
        with self._lock:
            return dict(self._data["items"].get(code, {}))

    def period(self, symbol: str) -> int:
        p = self.get(symbol).get("period_min", DEFAULT_PERIOD)
        return p if p in ALLOWED_PERIODS else DEFAULT_PERIOD

    def rules(self, symbol: str) -> dict:
        r = dict(DEFAULT_RULES)
        r.update(self.get(symbol).get("rules") or {})
        return r

    def set_period(self, symbol: str, period_min: int) -> dict:
        code = tq.bare_code(symbol)
        if int(period_min) not in ALLOWED_PERIODS:
            raise ValueError(f"period_min 必须是 {ALLOWED_PERIODS} 之一")
        with self._lock:
            item = self._data["items"].setdefault(code, {})
            item["period_min"] = int(period_min)
            item["updated_at"] = time.time()
        self.save()
        return {"symbol": code, "period_min": int(period_min)}

    def set_rules(self, symbol: str, rules: dict) -> dict:
        code = tq.bare_code(symbol)
        clean = {}
        for k, v in (rules or {}).items():
            if k in DEFAULT_RULES:
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    pass
        with self._lock:
            item = self._data["items"].setdefault(code, {})
            item.setdefault("rules", {}).update(clean)
            item["updated_at"] = time.time()
        self.save()
        return {"symbol": code, "rules": self.rules(code)}

    def remove(self, symbol: str) -> None:
        code = tq.bare_code(symbol)
        with self._lock:
            self._data["items"].pop(code, None)
        self.save()

    def set_tick(self, tick_sec: float) -> dict:
        t = max(0.5, min(30.0, float(tick_sec)))
        with self._lock:
            self._data["tick_sec"] = t
        self.save()
        return {"tick_sec": t}

    def set_enabled(self, flag: bool) -> dict:
        with self._lock:
            self._data["enabled"] = bool(flag)
        self.save()
        return {"enabled": bool(flag)}

    def prune(self, valid_symbols: list) -> int:
        """移除已不在自选里的股票的配置。"""
        keep = {tq.bare_code(s) for s in valid_symbols}
        n = 0
        with self._lock:
            for code in list(self._data["items"].keys()):
                if code not in keep:
                    self._data["items"].pop(code, None)
                    n += 1
        if n:
            self.save()
        return n


# ---------------------------------------------------------------------------
# 预警评估
# ---------------------------------------------------------------------------

def evaluate(quote: dict, rules: dict) -> dict | None:
    """对单只股票评估是否触发预警。

    返回 None 表示无预警；否则返回：
      {symbol, name, side: 'buy'|'sell', level, flash: 'green'|'red',
       reasons[], quote_time, price, pct, vol_ratio}
    """
    if not quote or not quote.get("ok"):
        return None
    pct = quote.get("pct")
    if pct is None:
        return None

    reasons = []
    side = None

    up_th = float(rules.get("pct_up", DEFAULT_RULES["pct_up"]))
    down_th = float(rules.get("pct_down", DEFAULT_RULES["pct_down"]))
    vr_th = float(rules.get("vol_ratio", DEFAULT_RULES["vol_ratio"]))
    to_th = float(rules.get("turnover_spike", DEFAULT_RULES["turnover_spike"]))

    vr = quote.get("volume_ratio") or 0
    to = quote.get("turnover") or 0

    if pct <= down_th:
        side = "buy"
        reasons.append(f"跌幅 {pct:.2f}% ≤ {down_th}%（到达买点区间）")
    elif pct >= up_th:
        side = "sell"
        reasons.append(f"涨幅 {pct:+.2f}% ≥ {up_th}%（到达卖点区间）")

    if side is None:
        # 放量但幅度未达标 → 关注级（不闪，仅记录）
        if vr >= vr_th:
            return {
                "symbol": quote["symbol"],
                "name": quote.get("name"),
                "side": "watch",
                "level": 1,
                "flash": None,
                "reasons": [f"量比 {vr:.2f} ≥ {vr_th}（放量，幅度未达标）"],
                "quote_time": quote.get("quote_time"),
                "price": quote.get("price"),
                "pct": pct,
                "vol_ratio": vr,
            }
        return None

    # 级别判定：幅度 + 放量/换手 共振 → 升级
    level = 2
    if vr >= vr_th:
        reasons.append(f"量比 {vr:.2f} ≥ {vr_th}（放量确认）")
        level = 3
    elif to >= to_th:
        reasons.append(f"换手 {to:.2f}% ≥ {to_th}%（活跃度异常）")
        level = 3

    return {
        "symbol": quote["symbol"],
        "name": quote.get("name"),
        "side": side,
        "level": level,
        "level_label": LEVELS.get(level, ""),
        "flash": "green" if side == "buy" else "red",   # A股：买绿卖红
        "reasons": reasons,
        "quote_time": quote.get("quote_time"),          # 时效以 fields[30] 为准
        "price": quote.get("price"),
        "pct": pct,
        "vol_ratio": vr,
        "turnover": to,
    }


# ---------------------------------------------------------------------------
# 轮询引擎
# ---------------------------------------------------------------------------

class WatchEngine:
    """后台轮询线程：按每股周期评估，触发预警时入队等待前端拉取。"""

    def __init__(self, config: WatchConfig = None, client=None,
                 symbols_provider=None):
        self.config = config or WatchConfig()
        self.client = client or tq.get_client()
        self.symbols_provider = symbols_provider or (lambda: [])

        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._events = []          # 待前端消费的预警
        self._next_due = {}        # symbol -> 下次检查的 monotonic 时间
        self._state = {
            "running": False,
            "tick_count": 0,
            "consecutive_failures": 0,
            "backoff_until": 0.0,
            "last_tick_at": None,
            "last_quote_time": None,
            "last_error": None,
            "recovered_count": 0,
            "alert_count": 0,
            "per_symbol": {},       # symbol -> {last_check, period, last_pct}
        }

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "already": True}
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                           name="watch-engine")
            self._thread.start()
            self._state["running"] = True
        return {"ok": True, "started": True}

    def stop(self) -> dict:
        self._stop.set()
        with self._lock:
            self._state["running"] = False
        return {"ok": True, "stopped": True}

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- 主循环 ----------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:      # 任何异常都不许退出循环
                with self._lock:
                    self._state["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                    self._state["consecutive_failures"] += 1
            # tick 间隔（默认 1 秒，可配 0.5~30）
            interval = float(self.config.all().get("tick_sec") or DEFAULT_TICK_SEC)
            self._stop.wait(interval)

    def _tick(self):
        """一次 tick：挑出到期的股票 → 批量抓行情 → 评估 → 入队预警。"""
        now = time.monotonic()

        with self._lock:
            st = self._state
            st["tick_count"] += 1
            st["last_tick_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            backoff_until = st["backoff_until"]

        # 退避中
        if now < backoff_until:
            return

        if not self.config.all().get("enabled", True):
            return

        try:
            all_syms = list(self.symbols_provider() or [])
        except Exception:
            all_syms = []
        if not all_syms:
            return

        # 清理已移除的配置
        self.config.prune(all_syms)

        # 找出到期（到了自己周期）的股票
        due = []
        for s in all_syms:
            code = tq.bare_code(s)
            period_sec = self.config.period(s) * 60
            next_at = self._next_due.get(code, 0)
            if now >= next_at:
                due.append(s)
                self._next_due[code] = now + period_sec

        if not due:
            return

        quotes = self.client.fetch(due)
        any_ok = any(q.get("ok") for q in quotes)

        alerts = []
        for q in quotes:
            rules = self.config.rules(q.get("symbol") or "")
            ev = evaluate(q, rules)
            if ev:
                alerts.append(ev)

        with self._lock:
            st = self._state
            if any_ok:
                if st["consecutive_failures"] > 0:
                    st["recovered_count"] += 1
                st["consecutive_failures"] = 0
                st["backoff_until"] = 0.0
                st["last_error"] = None
                qts = [q.get("quote_time") for q in quotes if q.get("quote_time")]
                if qts:
                    st["last_quote_time"] = max(qts)
            else:
                st["consecutive_failures"] += 1
                # 指数退避：1,2,4,8,16,32,60 秒封顶
                backoff = min(60.0, 2 ** max(0, st["consecutive_failures"] - 1))
                st["backoff_until"] = time.monotonic() + backoff
                st["last_error"] = (quotes[0].get("error") if quotes else "无行情返回")

            for q in quotes:
                code = tq.bare_code(q.get("symbol") or "")
                ps = st["per_symbol"].setdefault(code, {})
                ps["last_check"] = st["last_tick_at"]
                ps["period_min"] = self.config.period(code)
                if q.get("ok"):
                    ps["last_pct"] = q.get("pct")
                    ps["last_price"] = q.get("price")
                    ps["quote_time"] = q.get("quote_time")

            for ev in alerts:
                ev["detected_at"] = st["last_tick_at"]
                ev["id"] = f"{ev['symbol']}-{ev['quote_time']}-{ev['side']}"
            if alerts:
                self._events.extend(alerts)
                st["alert_count"] += len(alerts)
                # 只保留最近 200 条，避免无限增长
                if len(self._events) > 200:
                    self._events = self._events[-200:]

    # -- 事件读取 --------------------------------------------------------
    def drain_events(self, limit: int = 50) -> list:
        """取出并清空预警队列（前端拉取用）。"""
        with self._lock:
            if not self._events:
                return []
            out = self._events[:limit]
            self._events = self._events[limit:]
            return out

    def peek_events(self, limit: int = 50) -> list:
        with self._lock:
            return list(self._events[:limit])

    def clear_events(self) -> int:
        with self._lock:
            n = len(self._events)
            self._events = []
            return n

    def status(self) -> dict:
        with self._lock:
            st = dict(self._state)
            st["per_symbol"] = {k: dict(v) for k, v in self._state["per_symbol"].items()}
            st["pending_events"] = len(self._events)
            st["running"] = self.is_running()
            st["backoff_remaining"] = max(
                0.0, round(st["backoff_until"] - time.monotonic(), 1))
            st.pop("backoff_until", None)
        st["client"] = self.client.stats_snapshot()
        st["config"] = self.config.all()
        st["session"] = tq.trading_session()
        return st

    def check_now(self, symbols: list = None) -> list:
        """立即检查一次（用于手动触发 / 测试），不改变周期节奏。"""
        syms = symbols or list(self.symbols_provider() or [])
        if not syms:
            return []
        quotes = self.client.fetch(syms, use_cache=False)
        out = []
        for q in quotes:
            ev = evaluate(q, self.config.rules(q.get("symbol") or ""))
            if ev:
                ev["detected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ev["id"] = f"{ev['symbol']}-{ev['quote_time']}-{ev['side']}"
                out.append(ev)
        return out


#: 进程内单例
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine(symbols_provider=None) -> WatchEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = WatchEngine(symbols_provider=symbols_provider)
        elif symbols_provider is not None and _ENGINE.symbols_provider is None:
            _ENGINE.symbols_provider = symbols_provider
        return _ENGINE
