"""腾讯实时行情接入：解析 · 时效校验 · 批量限流

需求「交易舱 v1」P0 的数据底座。

接口事实（已实测确认）
----------------------
* URL: ``https://qt.gtimg.cn/q=sh600126,sz000657``（市场前缀 + 代码，逗号批量）
* 编码: **gb18030**
* 格式: ``v_sh600126="字段~字段~...";`` 每行一只，共 88 个字段
* 关键字段位（实测，与需求给出的示例一致）::

      [1]  名称
      [2]  代码
      [3]  最新价
      [4]  昨收
      [5]  今开
      [30] 行情时间  YYYYMMDDHHMMSS   ← 时效判定唯一依据
      [31] 涨跌额
      [32] 涨跌幅 %
      [33] 最高
      [34] 最低
      [36] 成交量（手）
      [37] 成交额（万元）
      [38] 换手率 %
      [43] 振幅 %
      [44] 流通市值（亿）
      [45] 总市值（亿）
      [46] 量比

时效判定
--------
需求明确要求：**以 fields[30] 的行情时间作为时效依据**，不能用 DSA 的
`update_time`。本模块只暴露 `quote_time`（来自 fields[30]）与由它派生的
`age_sec`（距今秒数），任何"新鲜度"判断都以此为准。

限流
----
* 单批 ≤ 500 只（超出自动分批）
* 请求间隔 ≥ 500ms（全局节流，跨线程共享）
* 行情缓存 5 秒（同一代码 5 秒内不重复打接口）
* 并发 ≤ 3（信号量）
* 429 / 5xx 触发指数退避
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

import requests

#: 腾讯行情端点
QT_URL = "https://qt.gtimg.cn/q="

#: 单批上限（需求：约 500 只）
MAX_BATCH = 500

#: 默认请求间隔（毫秒）
DEFAULT_INTERVAL_MS = 500

#: 行情缓存有效期（秒）
DEFAULT_CACHE_TTL = 5

#: 并发上限
DEFAULT_CONCURRENCY = 3

#: 字段位常量（集中定义，避免魔数散落）
F_NAME = 1
F_CODE = 2
F_PRICE = 3
F_PREV_CLOSE = 4
F_OPEN = 5
F_TIME = 30
F_CHANGE = 31
F_PCT = 32
F_HIGH = 33
F_LOW = 34
F_VOLUME = 36
F_AMOUNT = 37
F_TURNOVER = 38
F_AMPLITUDE = 43
F_FLOAT_MV = 44
F_TOTAL_MV = 45
F_VOL_RATIO = 46

_LINE_RE = re.compile(r'v_(\w+)="([^"]*)";')


# ---------------------------------------------------------------------------
# 代码 → 市场前缀
# ---------------------------------------------------------------------------

def market_prefix(symbol: str) -> str:
    """6 位代码 → 市场前缀。

    与项目既有 market.market_prefix 保持一致的判断：
    6/9/5 开头 → sh（沪市、沪市 ETF、沪市 B 股）
    4/8 开头   → bj（北交所）
    其余       → sz（深市）
    """
    s = str(symbol).strip()
    if s[:2].lower() in ("sh", "sz", "bj"):
        return s[:2].lower()
    s = s.zfill(6)
    if s[0] in "69 5".replace(" ", ""):
        return "sh"
    if s[0] in "48":
        return "bj"
    return "sz"


def full_symbol(symbol: str) -> str:
    """补齐市场前缀，如 600126 → sh600126。已带前缀则原样返回。"""
    s = str(symbol).strip()
    if s[:2].lower() in ("sh", "sz", "bj"):
        return s
    return market_prefix(s) + s.zfill(6)


def bare_code(symbol: str) -> str:
    """去掉市场前缀，只留 6 位代码。"""
    s = str(symbol).strip()
    if s[:2].lower() in ("sh", "sz", "bj"):
        return s[2:]
    return s.zfill(6)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _f(v, default=None):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return default


def parse_quote_text(text: str) -> list:
    """解析腾讯返回文本 → 行情列表。纯函数，无网络，便于单测。"""
    out = []
    for sym, body in _LINE_RE.findall(text or ""):
        f = body.split("~")
        if len(f) < 35:
            # 字段不足：多半该代码无行情（停牌/退市/代码错误）
            out.append({
                "full_symbol": sym,
                "symbol": bare_code(sym),
                "ok": False,
                "error": f"字段不足（{len(f)}），该代码可能无行情",
            })
            continue
        quote_time = (f[F_TIME] or "").strip()
        out.append({
            "full_symbol": sym,
            "symbol": f[F_CODE] or bare_code(sym),
            "name": f[F_NAME],
            "price": _f(f[F_PRICE]),
            "prev_close": _f(f[F_PREV_CLOSE]),
            "open": _f(f[F_OPEN]),
            "quote_time": quote_time,          # ← fields[30]，时效唯一依据
            "change": _f(f[F_CHANGE]),
            "pct": _f(f[F_PCT]),
            "high": _f(f[F_HIGH]),
            "low": _f(f[F_LOW]),
            "volume": _f(f[F_VOLUME]),
            "amount": _f(f[F_AMOUNT]),
            "turnover": _f(f[F_TURNOVER]),
            "amplitude": _f(f[F_AMPLITUDE]) if len(f) > F_AMPLITUDE else None,
            "float_mv": _f(f[F_FLOAT_MV]) if len(f) > F_FLOAT_MV else None,
            "total_mv": _f(f[F_TOTAL_MV]) if len(f) > F_TOTAL_MV else None,
            "volume_ratio": _f(f[F_VOL_RATIO]) if len(f) > F_VOL_RATIO else None,
            "ok": True,
        })
    return out


def parse_quote_time(qt: str):
    """'20261019142647' → aware datetime；不可解析返回 None。"""
    qt = (qt or "").strip()
    if len(qt) < 14 or not qt[:14].isdigit():
        return None
    try:
        return datetime.strptime(qt[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def quote_age_sec(quote: dict, now: datetime = None) -> float | None:
    """行情时间距现在的秒数。**唯一的时效来源是 fields[30]。**"""
    dt = parse_quote_time(quote.get("quote_time"))
    if dt is None:
        return None
    ref = now or datetime.now()
    return round((ref - dt).total_seconds(), 1)


def is_fresh(quote: dict, max_age_sec: float = 300, now: datetime = None) -> bool:
    """行情是否在容忍时效内（默认 5 分钟）。

    注意：非交易时段行情时间会停在收盘时刻，`age_sec` 会很大 ——
    调用方应结合 trading_session 判定，不要单看这个布尔值。
    """
    age = quote_age_sec(quote, now)
    return age is not None and age <= max_age_sec


# ---------------------------------------------------------------------------
# 交易时段
# ---------------------------------------------------------------------------

AM_OPEN, AM_CLOSE = (9, 30), (11, 30)
PM_OPEN, PM_CLOSE = (13, 0), (15, 0)


def _hm(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def trading_session(now: datetime = None) -> dict:
    """当前是否处于 A 股连续交易时段。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return {"open": False, "reason": "周末休市", "phase": "closed"}
    t = _hm(now)
    if _hm(datetime.combine(now.date(), now.time())) < AM_OPEN[0] * 60 + AM_OPEN[1]:
        return {"open": False, "reason": "开盘前", "phase": "pre"}
    if AM_OPEN[0] * 60 + AM_OPEN[1] <= t <= AM_CLOSE[0] * 60 + AM_CLOSE[1]:
        return {"open": True, "reason": "上午交易", "phase": "am"}
    if AM_CLOSE[0] * 60 + AM_CLOSE[1] < t < PM_OPEN[0] * 60 + PM_OPEN[1]:
        return {"open": False, "reason": "午间休市", "phase": "noon"}
    if PM_OPEN[0] * 60 + PM_OPEN[1] <= t <= PM_CLOSE[0] * 60 + PM_CLOSE[1]:
        return {"open": True, "reason": "下午交易", "phase": "pm"}
    return {"open": False, "reason": "已收盘", "phase": "post"}


# ---------------------------------------------------------------------------
# 限流器
# ---------------------------------------------------------------------------

class RateLimiter:
    """全局节流：两次请求起点之间至少间隔 interval_ms。跨线程共享。"""

    def __init__(self, interval_ms: int = DEFAULT_INTERVAL_MS,
                 concurrency: int = DEFAULT_CONCURRENCY):
        self.interval_ms = max(0, int(interval_ms))
        self._lock = threading.Lock()
        self._last = 0.0
        self._sem = threading.Semaphore(max(1, int(concurrency)))
        self.stats = {"requests": 0, "waits": 0, "last_wait_ms": 0}

    def set_interval(self, interval_ms: int):
        with self._lock:
            self.interval_ms = max(0, int(interval_ms))

    def acquire(self):
        """阻塞直到可以发请求（返回前已占用一个并发槽）。"""
        self._sem.acquire()
        with self._lock:
            now = time.monotonic()
            gap = self.interval_ms / 1000.0
            wait = (self._last + gap) - now
            if wait > 0:
                self.stats["waits"] += 1
                self.stats["last_wait_ms"] = round(wait * 1000, 1)
                time.sleep(wait)
                now = time.monotonic()
            self._last = now
            self.stats["requests"] += 1

    def release(self):
        self._sem.release()

    def snapshot(self) -> dict:
        return dict(self.stats, interval_ms=self.interval_ms)


# ---------------------------------------------------------------------------
# 行情客户端
# ---------------------------------------------------------------------------

class QuoteClient:
    """带缓存 / 节流 / 并发限制 / 退避重试的腾讯行情客户端。"""

    def __init__(self, interval_ms: int = DEFAULT_INTERVAL_MS,
                 cache_ttl: float = DEFAULT_CACHE_TTL,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 timeout: float = 10.0):
        self.limiter = RateLimiter(interval_ms, concurrency)
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._cache = {}
        self._cache_lock = threading.Lock()
        # 绕开系统代理：与本项目其他行情源一致的既有做法
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        })
        self.stats = {
            "http_requests": 0, "http_errors": 0, "429": 0,
            "cache_hits": 0, "batches": 0, "parsed": 0,
        }
        self._stats_lock = threading.Lock()

    def _bump(self, key, n=1):
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    # -- 缓存 ------------------------------------------------------------
    def _cache_get(self, full_sym, allow_stale=False):
        with self._cache_lock:
            hit = self._cache.get(full_sym)
        if not hit:
            return None
        ts, data = hit
        age = time.monotonic() - ts
        if age <= self.cache_ttl:
            self._bump("cache_hits")
            return dict(data, _cache_age=round(age, 2))
        if allow_stale:
            return dict(data, _cache_age=round(age, 2), _stale=True)
        return None

    def _cache_put(self, full_sym, data):
        with self._cache_lock:
            self._cache[full_sym] = (time.monotonic(), data)

    # -- 单批 ------------------------------------------------------------
    def fetch_batch(self, symbols, retries: int = 3, use_cache: bool = True) -> list:
        """抓取一批（≤ MAX_BATCH）行情。返回 quote dict 列表。"""
        fulls = [full_symbol(s) for s in symbols if str(s).strip()]
        if not fulls:
            return []
        if len(fulls) > MAX_BATCH:
            raise ValueError(f"单批上限 {MAX_BATCH} 只，收到 {len(fulls)} 只")

        result, need = {}, []
        if use_cache:
            for fs in fulls:
                c = self._cache_get(fs)
                if c is not None:
                    result[fs] = c
                else:
                    need.append(fs)
        else:
            need = list(fulls)

        if need:
            self._bump("batches")
            got = self._http_fetch(need, retries=retries)
            for q in got:
                fs = q.get("full_symbol")
                if q.get("ok"):
                    self._cache_put(fs, q)
                result[fs] = q

        # 保持传入顺序；缺失的补一条失败记录
        out = []
        for fs in fulls:
            if fs in result:
                out.append(result[fs])
            else:
                out.append({"full_symbol": fs, "symbol": bare_code(fs),
                            "ok": False, "error": "未返回该代码"})
        return out

    def _http_fetch(self, fulls: list, retries: int = 3) -> list:
        url = QT_URL + ",".join(fulls)
        last_err = None
        for attempt in range(retries):
            self.limiter.acquire()
            try:
                self._bump("http_requests")
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 429:
                    self._bump("429")
                    # 指数退避：0.5s, 1s, 2s…（叠加在正常节流之上）
                    backoff = min(8.0, 0.5 * (2 ** attempt))
                    last_err = "429 Too Many Requests"
                    time.sleep(backoff)
                    continue
                if r.status_code >= 500:
                    self._bump("http_errors")
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                    continue
                r.raise_for_status()
                text = r.content.decode("gb18030", errors="replace")
                quotes = parse_quote_text(text)
                self._bump("parsed", len(quotes))
                return quotes
            except Exception as e:
                self._bump("http_errors")
                last_err = f"{type(e).__name__}: {str(e)[:80]}"
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
            finally:
                self.limiter.release()

        # 全部重试失败：返回失败记录，**不抛出**（盯盘不能因单次失败中断）
        return [{"full_symbol": fs, "symbol": bare_code(fs), "ok": False,
                 "error": f"请求失败: {last_err}"} for fs in fulls]

    # -- 多批 ------------------------------------------------------------
    def fetch(self, symbols, use_cache: bool = True) -> list:
        """自动分批抓取任意数量（>500 自动切分）。"""
        fulls = [full_symbol(s) for s in symbols if str(s).strip()]
        seen, uniq = set(), []
        for f in fulls:
            if f not in seen:
                seen.add(f)
                uniq.append(f)
        out = []
        for i in range(0, len(uniq), MAX_BATCH):
            chunk = uniq[i:i + MAX_BATCH]
            out.extend(self.fetch_batch(chunk, use_cache=use_cache))
        return out

    def stats_snapshot(self) -> dict:
        with self._stats_lock:
            s = dict(self.stats)
        s["limiter"] = self.limiter.snapshot()
        with self._cache_lock:
            s["cache_size"] = len(self._cache)
        return s

    def clear_cache(self):
        with self._cache_lock:
            self._cache.clear()


#: 进程内共享的默认客户端（供 API 与后台引擎复用）
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> QuoteClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            interval = int(os.environ.get("KRONOS_QT_INTERVAL_MS",
                                          DEFAULT_INTERVAL_MS))
            _CLIENT = QuoteClient(interval_ms=interval)
        return _CLIENT
