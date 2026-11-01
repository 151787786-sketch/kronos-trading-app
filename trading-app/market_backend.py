"""行情数据源适配层：统一契约 + 可插拔后端

设计目标
--------
项目里已有 **68 个** `market.fetch_*` 调用点，散落在 10 个模块。本层让数据源
可替换，而**调用方一行都不用改** —— 所有后端实现同一个契约，`market.py` 通过
本层分发。

契约（三件事）
--------------
1. `fetch_kline(symbol, period, bars)` → DataFrame
   列：timestamps / open / high / low / close / volume / amount
   按 timestamps 升序；空数据抛 RuntimeError
2. `fetch_realtime(codes)` → list[dict]
   字段：symbol / name / price / change / pct / open / high / low /
        volume / amount / time / turnover / volume_ratio
   取不到的代码**跳过**（不抛异常），保持与既有实现一致的宽容度
3. `resolve(query)` → dict{symbol, name} 或抛 ValueError

后端注册
--------
后端用装饰器注册，按优先级排序；`get_backend()` 取第一个"可用"的后端。
`available()` 返回 False 的后端会被自动跳过 —— 这就是 Wind 未安装时的行为：
**不报错、不阻塞**，静默回退到腾讯。

诚实声明
--------
* 每个后端必须声明 `capabilities()`：支持哪些周期、是否提供真实成交额、
  是否有涨跌停/复权/Level-2 等。UI 与诊断据此显示，不猜测。
* Wind 后端在未安装 WindPy 时 `available()` 返回 False，并给出**明确原因**，
  而不是假装能取数。
"""
from __future__ import annotations

import os
import threading

import pandas as pd


# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

KLINE_COLUMNS = ["timestamps", "open", "high", "low", "close", "volume", "amount"]

#: 项目支持的 K 线周期
PERIODS = ("day", "1m", "5m", "15m", "30m", "60m")

#: 实时行情字段（缺一不可，调用方按这些键取值）
QUOTE_FIELDS = (
    "symbol", "name", "price", "change", "pct", "open", "high", "low",
    "volume", "amount", "time", "turnover", "volume_ratio",
)


class BackendError(RuntimeError):
    """后端可用但本次取数失败（网络、无数据、限流等）。"""


class BackendUnavailable(RuntimeError):
    """后端在当前环境不可用（未安装、未登录、未配置）。"""


# ---------------------------------------------------------------------------
# 后端基类
# ---------------------------------------------------------------------------

class DataBackend:
    """行情后端基类。子类至少实现 fetch_kline / fetch_realtime。"""

    #: 注册名，例如 "tencent" / "wind"
    name = "base"

    #: 优先级：数字越大越优先。Wind 高于腾讯（专业数据源优先）。
    priority = 0

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # -- 能力声明 ------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        """(是否可用, 原因)。原因是给人看的，不可用时必须说明缺什么。"""
        return True, ""

    def capabilities(self) -> dict:
        """能力声明。调用方/UI 据此显示，不做假设。"""
        return {
            "name": self.name,
            "periods": list(PERIODS),
            "real_amount": False,     # 是否提供真实成交额（非估算）
            "level2": False,          # 是否提供 Level-2
            "adjusted": False,        # 是否提供复权价
            "history_depth": None,    # 历史深度（bars），None = 不限
        }

    # -- 取数 ----------------------------------------------------------
    def fetch_kline(self, symbol: str, period: str = "day",
                    bars: int = 600) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_realtime(self, codes: list) -> list:
        raise NotImplementedError

    def resolve(self, query: str) -> dict:
        raise NotImplementedError

    # -- 工具 ----------------------------------------------------------
    @staticmethod
    def _empty_kline(symbol: str, period: str) -> pd.DataFrame:
        return pd.DataFrame(columns=KLINE_COLUMNS)

    @staticmethod
    def _validate_kline(df: pd.DataFrame, symbol: str, period: str) -> pd.DataFrame:
        """统一校验与规范化 —— 每个后端都必须过这一关。"""
        if df is None or len(df) == 0:
            raise BackendError(
                f"未获取到 {symbol} 的 {period} 周期数据（请确认代码正确，或稍后重试）"
            )
        missing = [c for c in ("timestamps", "open", "high", "low", "close", "volume")
                   if c not in df.columns]
        if missing:
            raise BackendError(f"后端返回缺少必需列: {missing}")
        out = df.copy()
        out["timestamps"] = pd.to_datetime(out["timestamps"])
        for col in ("open", "high", "low", "close", "volume"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        if "amount" not in out.columns:
            out["amount"] = float("nan")
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce")
        out = out.dropna(subset=["open", "high", "low", "close"])
        if out.empty:
            raise BackendError(f"{symbol} 的 {period} 数据全部无效")
        return out.sort_values("timestamps").reset_index(drop=True)[KLINE_COLUMNS]


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, DataBackend] = {}
_LOCK = threading.Lock()
_STATE = {
    "active": None,        # 当前生效后端名
    "last_error": None,    # 最近一次回退原因
    "fallbacks": 0,        # 回退次数
}


def register(backend: DataBackend, name: str | None = None) -> DataBackend:
    """注册后端。同名覆盖（便于测试替换）。"""
    key = name or backend.name
    with _LOCK:
        _BACKENDS[key] = backend
    return backend


def unregister(name: str) -> None:
    with _LOCK:
        _BACKENDS.pop(name, None)


def list_backends() -> list:
    """列出所有后端及其可用性 —— 诊断面板用。"""
    with _LOCK:
        items = list(_BACKENDS.values())
    out = []
    for b in sorted(items, key=lambda x: -x.priority):
        ok, reason = b.available()
        out.append({
            "name": b.name,
            "priority": b.priority,
            "available": ok,
            "unavailable_reason": reason,
            "capabilities": b.capabilities(),
        })
    return out


def get_backend(preferred: str | None = None) -> DataBackend:
    """取当前可用后端（优先 preferred，其次按 priority 降序）。"""
    with _LOCK:
        items = list(_BACKENDS.values())

    if preferred:
        for b in items:
            if b.name == preferred:
                ok, reason = b.available()
                if ok:
                    _STATE["active"] = b.name
                    return b
                _STATE["last_error"] = f"{b.name} 不可用：{reason}"
                _STATE["fallbacks"] += 1

    for b in sorted(items, key=lambda x: -x.priority):
        ok, reason = b.available()
        if ok:
            if _STATE["active"] != b.name:
                if _STATE["active"] is not None:
                    _STATE["fallbacks"] += 1
                _STATE["active"] = b.name
            return b

    raise BackendUnavailable(
        "没有任何可用的行情后端。已注册："
        + ", ".join(f"{b.name}({b.available()[1] or 'ok'})" for b in items)
    )


def state() -> dict:
    """当前适配层状态（诊断用）。"""
    with _LOCK:
        active = _STATE["active"]
        err = _STATE["last_error"]
        fb = _STATE["fallbacks"]
    return {"active": active, "last_error": err, "fallbacks": fb,
            "registered": sorted(_BACKENDS.keys())}


# ---------------------------------------------------------------------------
# 后端选择偏好（存 settings 表，与项目其余配置一致）
# ---------------------------------------------------------------------------

SETTING_KEY = "datasource_backend"
VALID_CHOICES = ("auto", "tencent", "wind")


def _settings_conn():
    """连 trade.db 的 settings 表。失败返回 None（配置读不到就用默认）。"""
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade.db")
    try:
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("CREATE TABLE IF NOT EXISTS settings "
                     "(key TEXT PRIMARY KEY, value TEXT)")
        return conn
    except Exception:
        return None


def get_preference() -> str:
    """当前选择：'auto' / 'tencent' / 'wind'。默认 auto（按优先级，有 Wind 优先）。"""
    env = os.environ.get("KRONOS_DATASOURCE", "").strip().lower()
    if env in VALID_CHOICES:
        return env
    conn = _settings_conn()
    if conn is None:
        return "auto"
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (SETTING_KEY,)).fetchone()
        val = (row[0] or "").strip().lower() if row else ""
        return val if val in VALID_CHOICES else "auto"
    except Exception:
        return "auto"
    finally:
        conn.close()


def set_preference(choice: str) -> dict:
    """设置后端选择。'auto' 表示按优先级自动挑（有 Wind 用 Wind）。"""
    c = str(choice or "").strip().lower()
    if c not in VALID_CHOICES:
        raise ValueError(f"backend 必须是 {list(VALID_CHOICES)} 之一，收到 {choice!r}")
    conn = _settings_conn()
    if conn is None:
        raise BackendError("无法写入配置（trade.db 不可用）")
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                     (SETTING_KEY, c))
        conn.commit()
    finally:
        conn.close()
    # 切换后清空探测缓存，让新选择立即生效
    with _LOCK:
        for b in _BACKENDS.values():
            if hasattr(b, "refresh_probe"):
                b.refresh_probe()
        _STATE["active"] = None
        _STATE["last_error"] = None
    return {"backend": c}


def effective_backend_name() -> str:
    """实际会用的后端名（解析 auto）。没有可用后端时返回 None。"""
    pref = get_preference()
    try:
        b = get_backend(None if pref == "auto" else pref)
        return b.name
    except BackendUnavailable:
        return None


# ---------------------------------------------------------------------------
# 腾讯后端（默认，无需任何配置）
# ---------------------------------------------------------------------------

class TencentBackend(DataBackend):
    """腾讯免费行情。项目原有实现，提取为后端以便与其他源并列。"""

    name = "tencent"
    priority = 10

    def available(self):
        return True, ""   # 公共接口，永远可用

    def capabilities(self):
        return {
            "name": self.name,
            "periods": list(PERIODS),
            # K 线成交额是 volume × close 估算（接口不返回该字段，已实测确认）；
            # 实时行情里的 amount 是腾讯返回的真实值。见 fetch_kline 注释。
            "real_amount": False,
            "real_amount_realtime": True,
            "level2": False,
            "adjusted": True,       # 日线支持前复权 qfq
            "history_depth": 640,
        }

    def fetch_kline(self, symbol, period="day", bars=600):
        import market   # 延迟导入，避免循环
        df = market._tencent_fetch_kline(symbol, period, bars)
        return self._validate_kline(df, symbol, period)

    def fetch_realtime(self, codes):
        import market
        return market._tencent_fetch_realtime(codes)

    def resolve(self, query):
        import market
        return market._tencent_resolve(query)


# ---------------------------------------------------------------------------
# Wind 后端（专业数据源；未安装时优雅不可用）
# ---------------------------------------------------------------------------

#: Wind 周期 → Wind 的 barSize
WIND_PERIODS = {
    "day": "1d",
    "1m": "1",       # wsi 的 1 分钟
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
}


class WindBackend(DataBackend):
    """万得（Wind）行情。

    依赖本机安装 Wind 金融终端并登录，且 pip 安装 WindPy。未满足时
    ``available()`` 返回 False 并说明缺什么 —— 不会静默假装成功。
    """

    name = "wind"
    priority = 100      # 高于腾讯：有 Wind 就优先用 Wind

    def __init__(self, config=None):
        super().__init__(config)
        self._probe = None      # 缓存探测结果，避免每次取数都启动 Wind

    # -- 可用性探测 ----------------------------------------------------
    def available(self):
        if self._probe is not None:
            return self._probe

        # 1. 开关：允许用环境变量强制关闭
        if os.environ.get("KRONOS_DISABLE_WIND", "").strip() in ("1", "true", "yes"):
            self._probe = (False, "已被 KRONOS_DISABLE_WIND 环境变量关闭")
            return self._probe

        # 2. WindPy 是否可导入
        try:
            import WindPy  # noqa: F401
        except Exception:
            self._probe = (False, "未安装 WindPy（需先安装 Wind 金融终端，再 pip install WindPy）")
            return self._probe

        # 3. 终端是否已登录（w.start 会连终端）
        try:
            from WindPy import w
            res = w.start(waitTime=3)
            if getattr(res, "ErrorCode", -1) != 0:
                self._probe = (False, f"Wind 终端未就绪（ErrorCode={getattr(res, 'ErrorCode', '?')}），请确认已登录")
                return self._probe
        except Exception as e:
            self._probe = (False, f"Wind 终端启动失败：{type(e).__name__}: {str(e)[:80]}")
            return self._probe

        self._probe = (True, "")
        return self._probe

    def refresh_probe(self):
        self._probe = None

    def capabilities(self):
        return {
            "name": self.name,
            "periods": list(PERIODS),
            "real_amount": True,
            "level2": True,
            "adjusted": True,
            "history_depth": None,   # Wind 历史很深，不设限
        }

    # -- 代码转换 ------------------------------------------------------
    @staticmethod
    def to_wind_code(symbol: str) -> str:
        """600519 → 600519.SH；000001 → 000001.SZ；430047 → 430047.BJ"""
        s = str(symbol).strip().upper()
        if "." in s:
            return s
        for pre, suf in (("SH", ".SH"), ("SZ", ".SZ"), ("BJ", ".BJ")):
            if s.startswith(pre):
                return s[2:].zfill(6) + suf
        code = s.zfill(6)
        if code[0] in "695":
            return code + ".SH"
        if code[0] in "48":
            return code + ".BJ"
        return code + ".SZ"

    @staticmethod
    def from_wind_code(code: str) -> str:
        return str(code).split(".")[0].zfill(6)

    # -- K 线 ----------------------------------------------------------
    def fetch_kline(self, symbol, period="day", bars=600):
        ok, reason = self.available()
        if not ok:
            raise BackendUnavailable(f"Wind 不可用：{reason}")
        if period not in WIND_PERIODS:
            raise BackendError(f"Wind 后端不支持周期 {period}")

        from WindPy import w
        wcode = self.to_wind_code(symbol)
        fields = "open,high,low,close,volume,amt"

        # 日线用 wsd（历史日线），分钟线用 wsi（分钟序列）
        if period == "day":
            end = pd.Timestamp.now().normalize()
            start = end - pd.Timedelta(days=max(60, int(bars * 1.6)))
            res = w.wsd(wcode, fields, start.strftime("%Y-%m-%d"),
                        end.strftime("%Y-%m-%d"), "")
        else:
            size = WIND_PERIODS[period]
            res = w.wsi(wcode, fields,
                        pd.Timestamp.now().strftime("%Y-%m-%d 09:00:00"),
                        pd.Timestamp.now().strftime("%Y-%m-%d 16:00:00"),
                        f"BarSize={size}")

        if getattr(res, "ErrorCode", -1) != 0:
            raise BackendError(f"Wind 返回错误 {res.ErrorCode}（{wcode} {period}）")

        data = getattr(res, "Data", None)
        if not data or len(data) < 6:
            raise BackendError(f"Wind 无数据（{wcode} {period}）")

        times = [pd.Timestamp(t) for t in res.Times]
        df = pd.DataFrame({
            "timestamps": times,
            "open": data[0], "high": data[1], "low": data[2],
            "close": data[3], "volume": data[4], "amount": data[5],
        })
        df = df.dropna(subset=["close"])
        if len(df) > bars:
            df = df.tail(bars)
        return self._validate_kline(df, symbol, period)

    # -- 实时行情 ------------------------------------------------------
    def fetch_realtime(self, codes):
        ok, reason = self.available()
        if not ok:
            raise BackendUnavailable(f"Wind 不可用：{reason}")
        if not codes:
            return []

        from WindPy import w
        wcodes = [self.to_wind_code(c) for c in codes]
        fields = ("rt_last,rt_chg,rt_pct_chg,rt_open,rt_high,rt_low,"
                  "rt_vol,rt_amt,rt_turn,rt_vol_ratio")
        res = w.wsq(",".join(wcodes), fields)
        if getattr(res, "ErrorCode", -1) != 0:
            raise BackendError(f"Wind wsq 返回错误 {res.ErrorCode}")

        codes_out = getattr(res, "Codes", wcodes)
        d = getattr(res, "Data", [])

        def col(i, j):
            try:
                return float(d[i][j])
            except Exception:
                return 0.0

        out = []
        for j, wcode in enumerate(codes_out):
            symbol = self.from_wind_code(wcode)
            name = self._name_of(symbol)
            price = col(0, j)
            if price <= 0:
                continue      # 取不到就跳过，与腾讯后端行为一致
            out.append({
                "symbol": symbol,
                "name": name,
                "price": price,
                "change": col(1, j),
                "pct": col(2, j),
                "open": col(3, j),
                "high": col(4, j),
                "low": col(5, j),
                "volume": col(6, j),
                "amount": col(7, j),
                "time": pd.Timestamp.now().strftime("%Y%m%d%H%M%S"),
                "turnover": col(8, j),
                "volume_ratio": col(9, j),
            })
        return out

    def _name_of(self, symbol):
        """取证券名称；失败返回代码本身，never raises。"""
        try:
            from WindPy import w
            res = w.wss(self.to_wind_code(symbol), "sec_name")
            if getattr(res, "ErrorCode", -1) == 0 and res.Data and res.Data[0]:
                return str(res.Data[0][0])
        except Exception:
            pass
        return str(symbol)

    # -- 代码解析 ------------------------------------------------------
    def resolve(self, query):
        ok, reason = self.available()
        if not ok:
            raise BackendUnavailable(f"Wind 不可用：{reason}")
        q = str(query).strip()
        if not q:
            raise ValueError("查询为空")
        # 纯 6 位或带前缀 → 直接用
        if q.replace(".", "").isdigit() or q[:2].upper() in ("SH", "SZ", "BJ"):
            code = self.from_wind_code(q)
            return {"symbol": code, "name": self._name_of(code)}
        # 按名称查：Wind 的 sectoranalyzer 太重，这里用 wset 的简单检索
        from WindPy import w
        try:
            res = w.wset("sectorconstituent", f"date={pd.Timestamp.now():%Y-%m-%d}")
            if getattr(res, "ErrorCode", -1) == 0 and res.Data:
                names = res.Data[1]
                codes = res.Data[2]
                for n, c in zip(names, codes):
                    if str(n) == q:
                        return {"symbol": self.from_wind_code(c), "name": str(n)}
        except Exception:
            pass
        raise ValueError(f"Wind 未找到股票「{query}」")


# ---------------------------------------------------------------------------
# 公开 API（market.py 转发到这些）
# ---------------------------------------------------------------------------

def active_backend(preferred: str | None = None) -> DataBackend:
    """取当前后端；preferred 优先。"""
    return get_backend(preferred)


def fetch_kline(symbol: str, period: str = "day", bars: int = 600,
                backend: str | None = None) -> pd.DataFrame:
    b = get_backend(backend)
    return b.fetch_kline(symbol, period, bars)


def fetch_realtime(codes, backend: str | None = None) -> list:
    b = get_backend(backend)
    return b.fetch_realtime(list(codes))


def resolve(query: str, backend: str | None = None) -> dict:
    b = get_backend(backend)
    return b.resolve(query)
