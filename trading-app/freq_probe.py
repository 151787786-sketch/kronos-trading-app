"""接口频次探针：实测腾讯行情在不同间隔下的成功率 / 耗时 / 是否被限流

需求「交易舱 v1」P0 第四项。

用法
----
探针分**三档间隔**（30 秒 / 1 秒 / 0.5 秒），每档连续请求 N 次（默认 10），
记录：
  * 成功率（HTTP 200 且解析出行情）
  * 响应耗时（均值 / 中位 / P95 / 最大）
  * 是否出现 429 或 5xx 限流
  * 是否出现"返回 200 但内容异常"（字段不足等，可能被风控软限流）

产出结论：给出**建议安全频次档位**，并明确标注这是本机、本时段、本次
样本量的实测结果 —— 不代表长期或全市场的普遍结论。

安全约束
--------
* 探针**默认只跑 30 秒档**（保守）；1 秒 / 0.5 秒档必须显式开关，避免误触发限流。
* 每次请求之间使用独立的（不受全局节流影响的）间隔，才能真实测出该档位表现。
* 探针可随时中止；中止后结果仍会返回已完成的部分。
"""
from __future__ import annotations

import statistics
import threading
import time
from datetime import datetime

import tencent_quote as tq

#: 三档实测间隔（秒）
PROBE_TIERS = (
    {"key": "30s", "interval_sec": 30.0, "label": "30 秒（保守档）"},
    {"key": "1s", "interval_sec": 1.0, "label": "1 秒（实验档）"},
    {"key": "0.5s", "interval_sec": 0.5, "label": "0.5 秒（激进档）"},
)

#: 默认档位（只跑保守档）
DEFAULT_TIER = "30s"

#: 默认每档请求次数
DEFAULT_SAMPLES = 10


class _Probe:
    """单个档位的实测执行器。"""

    def __init__(self, tier: dict, symbols: list, samples: int,
                 timeout: float = 10.0):
        self.tier = tier
        self.symbols = symbols or []
        self.samples = max(1, int(samples))
        self.timeout = timeout
        self._abort = threading.Event()
        self.started_at = None
        self.finished_at = None
        self.records = []

    def abort(self):
        self._abort.set()

    def run(self) -> dict:
        import requests
        self.started_at = time.time()
        s = requests.Session()
        s.trust_env = False          # 绕开系统代理
        s.headers.update({"User-Agent": "Mozilla/5.0",
                          "Referer": "https://gu.qq.com/"})
        url = tq.QT_URL + ",".join(tq.full_symbol(x) for x in self.symbols)
        iv = float(self.tier["interval_sec"])

        for i in range(self.samples):
            if self._abort.is_set():
                break
            rec = {"idx": i + 1, "t": datetime.now().strftime("%H:%M:%S.%f")[:-3]}
            t0 = time.monotonic()
            try:
                r = s.get(url, timeout=self.timeout)
                elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
                rec["status"] = r.status_code
                rec["elapsed_ms"] = elapsed_ms
                if r.status_code == 429:
                    rec["ok"] = False
                    rec["rate_limited"] = True
                    rec["error"] = "429 Too Many Requests"
                elif r.status_code >= 500:
                    rec["ok"] = False
                    rec["server_error"] = True
                    rec["error"] = f"HTTP {r.status_code}"
                elif r.status_code == 200:
                    text = r.content.decode("gb18030", errors="replace")
                    quotes = tq.parse_quote_text(text)
                    good = [q for q in quotes if q.get("ok")]
                    rec["ok"] = len(good) > 0
                    rec["parsed"] = len(good)
                    if not good:
                        rec["suspect"] = True
                        rec["error"] = "200 但无有效行情（疑似软限流）"
                else:
                    rec["ok"] = False
                    rec["error"] = f"HTTP {r.status_code}"
            except Exception as e:
                rec["ok"] = False
                rec["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
                rec["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            self.records.append(rec)

            if i < self.samples - 1 and not self._abort.is_set():
                self._abort.wait(iv)

        self.finished_at = time.time()
        return self.summarize()

    def summarize(self) -> dict:
        recs = self.records
        n = len(recs)
        ok = [r for r in recs if r.get("ok")]
        lat = [r["elapsed_ms"] for r in recs if r.get("elapsed_ms") is not None]
        limited = [r for r in recs if r.get("rate_limited")]
        suspect = [r for r in recs if r.get("suspect")]
        errors = [r for r in recs if not r.get("ok") and not r.get("rate_limited")]

        def _pct(vals, p):
            if not vals:
                return None
            vals = sorted(vals)
            k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
            return round(vals[k], 1)

        return {
            "tier": self.tier["key"],
            "tier_label": self.tier["label"],
            "interval_sec": self.tier["interval_sec"],
            "samples": n,
            "success": len(ok),
            "success_pct": round(len(ok) / n * 100, 1) if n else None,
            "latency_ms_avg": round(statistics.mean(lat), 1) if lat else None,
            "latency_ms_median": round(statistics.median(lat), 1) if lat else None,
            "latency_ms_p95": _pct(lat, 95),
            "latency_ms_max": round(max(lat), 1) if lat else None,
            "rate_limited": len(limited),
            "suspect_soft_limit": len(suspect),
            "error_count": len(errors),
            "errors": [
                {"idx": r["idx"], "error": r.get("error"), "status": r.get("status")}
                for r in errors[:5]
            ],
            "aborted": self._abort.is_set(),
            "started_at": (datetime.fromtimestamp(self.started_at)
                           .strftime("%Y-%m-%d %H:%M:%S")) if self.started_at else None,
            "finished_at": (datetime.fromtimestamp(self.finished_at)
                            .strftime("%Y-%m-%d %H:%M:%S")) if self.finished_at else None,
            "duration_sec": round((self.finished_at - self.started_at), 1)
            if self.started_at and self.finished_at else None,
            "records": recs,      # 逐条明细，便于复核
        }


#: 最近一次探针结果（供 API 读取）
_LAST = {"report": None, "running": False, "started_at": None}
_LOCK = threading.Lock()
_ACTIVE = {"probe": None}


def _recommend(tiers: list) -> dict:
    """根据多档实测结果给出建议档位。

    判定规则（保守）：
      * 任何一档出现 429 → 该档及更激进档 不可用
      * 成功率 100% 且 无软限流 → 可用
      * 在可用档中，选**最激进**的一档作为建议（最省服务器资源），
        但若最激进档的 P95 耗时 > 2000ms，则回退到上一档
    """
    usable = []
    for t in tiers:
        if not t.get("samples"):
            continue
        if t.get("rate_limited", 0) > 0:
            continue
        if t.get("success_pct") == 100.0 and t.get("suspect_soft_limit", 0) == 0:
            usable.append(t)

    if not usable:
        return {
            "safe_tier": None,
            "reason": "所有实测档位均出现限流或错误，建议仅使用 30 秒以上间隔",
            "usable_tiers": [],
        }

    # 最激进 = interval 最小
    usable.sort(key=lambda x: x["interval_sec"])
    best = usable[0]
    if (best.get("latency_ms_p95") or 0) > 2000:
        for t in usable:
            if t["interval_sec"] >= 30:
                best = t
                break

    return {
        "safe_tier": best["tier"],
        "safe_interval_sec": best["interval_sec"],
        "reason": (f"{best.get('tier_label') or best['tier']} 实测成功率 "
                   f"{best['success_pct']}%，无 429/软限流，"
                   f"P95 耗时 {best['latency_ms_p95']}ms"),
        "usable_tiers": [t["tier"] for t in usable],
    }


def run_probe(symbols: list, tier_keys: list = None, samples: int = None,
              block: bool = True) -> dict:
    """执行频次探针。

    tier_keys 默认只跑 30s 保守档；传 None 或 ["30s","1s","0.5s"] 才跑全部。
    返回完整报告（含逐档明细与建议）。
    """
    keys = tier_keys or [DEFAULT_TIER]
    tiers = [t for t in PROBE_TIERS if t["key"] in keys]
    if not tiers:
        return {"ok": False, "error": f"未知档位 {keys}，可选 {[t['key'] for t in PROBE_TIERS]}"}
    if not symbols:
        return {"ok": False, "error": "未提供标的，探针需要一个非空股票列表"}

    n = int(samples or DEFAULT_SAMPLES)
    n = max(1, min(100, n))

    def _run_all():
        results = []
        for tier in tiers:
            p = _Probe(tier, symbols, n)
            with _LOCK:
                _ACTIVE["probe"] = p
            results.append(p.run())
        with _LOCK:
            _ACTIVE["probe"] = None
            _LAST["report"] = {
                "ok": True,
                "symbols": [tq.full_symbol(s) for s in symbols],
                "samples_per_tier": n,
                "tiers": results,
                "recommendation": _recommend(results),
                "caveat": ("本结果为**本机、本时段、本次样本量**的实测，"
                           "不代表长期表现或全市场普遍结论。社区传言（500 只/0.5 秒长期未封禁）"
                           "为非官方口径，不可直接作为生产参数。"),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            _LAST["running"] = False

    with _LOCK:
        if _LAST["running"]:
            return {"ok": False, "error": "已有探针正在运行"}
        _LAST["running"] = True
        _LAST["started_at"] = time.time()

    if block:
        _run_all()
        with _LOCK:
            return dict(_LAST["report"] or {})
    t = threading.Thread(target=_run_all, daemon=True, name="freq-probe")
    t.start()
    return {"ok": True, "started": True,
            "tiers": [t["key"] for t in tiers], "samples": n}


def abort_probe() -> dict:
    with _LOCK:
        p = _ACTIVE["probe"]
    if p is None:
        return {"ok": False, "error": "没有正在运行的探针"}
    p.abort()
    return {"ok": True, "aborting": True}


def last_report() -> dict:
    with _LOCK:
        return {"report": _LAST["report"], "running": _LAST["running"],
                "started_at": _LAST["started_at"]}
