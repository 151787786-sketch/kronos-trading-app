"""Kronos Trading App - Flask backend.

A local web app that combines real A-share market data, technical indicators,
Kronos AI forecasts, trading signals, a simulated trading account, and strategy
backtesting.

Run:  python app.py   (or double-click 启动交易APP.bat)
"""
import os
import sys
import time  # noqa: F401  —— 模块级导入：monitor 等后台函数在 __main__ 之外也要用
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------- 日志
# 同时输出到控制台 + trading-app/logs/app.log（崩溃后仍可回溯）
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _setup_logging():
    import logging
    from logging.handlers import RotatingFileHandler
    os.makedirs(_LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        fh = RotatingFileHandler(os.path.join(_LOG_DIR, "app.log"),
                                 maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


_setup_logging()

import account  # noqa: E402
import accuracy  # noqa: E402
import advanced  # noqa: E402
import alerts  # noqa: E402
import analytics  # noqa: E402
import anomaly  # noqa: E402
import audit  # noqa: E402
import auto_trade  # noqa: E402
import backtest  # noqa: E402
import buysell  # noqa: E402
import calibration  # noqa: E402
import committee  # noqa: E402
import compliance  # noqa: E402
import deepseek  # noqa: E402
import dip  # noqa: E402
import evidence_center  # noqa: E402
import fundamentals  # noqa: E402
import freq_probe  # noqa: E402
import global_market  # noqa: E402
import indicators as ind  # noqa: E402
import industry  # noqa: E402
import kronos_service  # noqa: E402
import macro  # noqa: E402
import market  # noqa: E402
import nightly  # noqa: E402
import notify  # noqa: E402
import recommend  # noqa: E402
import requirements_map  # noqa: E402
import research_db  # noqa: E402
import research_report  # noqa: E402
import sentiment  # noqa: E402
import snapshots  # noqa: E402
import tail  # noqa: E402
import tencent_quote  # noqa: E402
import watch_engine  # noqa: E402
from agents import ROLES as AGENT_ROLES  # noqa: E402
from agents import llm as llm_adapter  # noqa: E402
from signals import forecast_direction_signals, indicator_signals  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # reject bodies > 1 MB
# 模板改动立即生效（debug=False 时 Jinja 默认会缓存模板，改了 index.html 不重启看不到）
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# POST 不带 body 时 request.get_json() 会抛 415/400；这里让它返回 None，
# 各处理函数既有的 `or {}` 兜底即可生效，避免调用方（脚本/curl）踩坑。
app.config["JSON_STRICT_DEFAULT"] = False


@app.errorhandler(415)
@app.errorhandler(400)
def _handle_bad_json(e):
    """请求体不是合法 JSON 时，对 API 返回结构化错误而非 HTML 页。"""
    from werkzeug.exceptions import UnsupportedMediaType
    if request.path.startswith("/api/"):
        msg = ("请求体不是合法 JSON 或缺少 Content-Type"
               if isinstance(e, UnsupportedMediaType)
               else str(getattr(e, "description", e)))
        return jsonify({"error": msg}), getattr(e, "code", 400)
    return e


@app.errorhandler(Exception)
def _handle_unexpected(e):
    """未捕获异常写入日志（附带堆栈），页面返回友好提示而不是裸 500。"""
    import logging
    import traceback
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    logging.getLogger("app").error("未处理异常: %s\n%s", e, traceback.format_exc())
    if request.path.startswith("/api/"):
        return jsonify({"error": f"服务内部错误：{e}"}), 500
    return (f"<h3>服务内部错误</h3><pre>{e}</pre>"
            f"<p>详细堆栈已写入 trading-app/logs/app.log</p>"), 500


# ---- helpers ----------------------------------------------------------------

MAX_PRED_LEN = 120
MAX_SAMPLE_COUNT = 5
MAX_ROUNDS = 5


def _int_arg(name, default, lo=None, hi=None):
    """Parse an int query arg with range clamping; bad values fall back to default."""
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def _float_arg(name, default, lo=None, hi=None):
    try:
        v = float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v


def _now_str():
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")


def _load_with_indicators(symbol, bars=600, force=False):
    df = market.fetch_daily(symbol, bars=bars, force=force)
    df = ind.all_indicators(df)
    return df


def _name_of(symbol):
    q = market.fetch_realtime_one(symbol)
    return q["name"] if q else symbol


# ---- pages -------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


# ---- system status ---------------------------------------------------------------


@app.route("/api/status")
def api_status():
    """Lightweight system status: model state, device, cache/DB sizes."""
    import torch

    cache_size = 0
    cache_files = 0
    for f in os.listdir(market.DATA_DIR):
        fp = os.path.join(market.DATA_DIR, f)
        if os.path.isfile(fp):
            cache_files += 1
            cache_size += os.path.getsize(fp)

    db_size = os.path.getsize(account.DB_PATH) if os.path.exists(account.DB_PATH) else 0

    return jsonify({
        "model_loaded": kronos_service.is_loaded(),
        "model": kronos_service.current_model(),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "cuda_version": torch.version.cuda if torch.cuda.is_available() else None,
        "torch_version": torch.__version__,
        "cache_files": cache_files,
        "cache_size_mb": round(cache_size / 1048576, 1),
        "db_size_kb": round(db_size / 1024, 1),
        "watchlist_count": len(account.get_watchlist()),
        "positions_count": len(account.get_positions()),
        "alert_plans": len(alerts.get_plans()),
        "alert_events": len(alerts.get_events()),
    })


# ---- model management -------------------------------------------------------------


@app.route("/api/models")
def api_models():
    """List available models (zoo + local)."""
    return jsonify({"models": kronos_service.available_models(),
                    "current": kronos_service.current_model()})


@app.route("/api/models/load", methods=["POST"])
def api_models_load():
    data = request.get_json() or {}
    model_id = str(data.get("model_id", "")).strip() or "Kronos-base"
    model_dir = str(data.get("model_dir", "")).strip() or None
    device = str(data.get("device", "")).strip() or None
    res = kronos_service.load_model(model_id, model_dir=model_dir, device=device)
    code = 200 if res["ok"] else 400
    return jsonify(res), code


@app.route("/api/models/unload", methods=["POST"])
def api_models_unload():
    return jsonify(kronos_service.unload_model())


# ---- fundamentals ----------------------------------------------------------------


@app.route("/api/fundamentals")
def api_fundamentals():
    """Fundamental snapshot: valuation + financials + company info + news."""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        data = fundamentals.analyze(symbol)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"基本面数据获取失败: {e}"}), 500


# ---- global markets ----------------------------------------------------------------


@app.route("/api/global")
def api_global():
    """Global indices: US/HK/Nikkei/KOSPI with quotes."""
    try:
        data = global_market.get_global_markets()
        return jsonify({"markets": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- market -------------------------------------------------------------------


@app.route("/api/market/resolve")
def api_market_resolve():
    """Resolve a code or name into an A-share symbol. e.g. ?q=金风科技 -> sz002202"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "missing q"}), 400
    try:
        r = market.resolve_symbol(q)
        return jsonify(r)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market/quotes")
def api_quotes():
    symbols = request.args.get("symbols", "")
    codes = [s for s in symbols.split(",") if s] or account.get_watchlist() and [w["symbol"] for w in account.get_watchlist()]
    quotes = market.fetch_realtime(codes)
    return jsonify({"quotes": quotes})


@app.route("/api/market/kline")
def api_kline():
    symbol = request.args.get("symbol", "")
    bars = _int_arg("bars", 300, 10, 1000)
    period = request.args.get("period", "day")
    force = request.args.get("force", "0") == "1"
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period=period, bars=bars, force=force)
        df = ind.all_indicators(df)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    data = df.tail(bars).reset_index(drop=True)
    cols = ["timestamps", "open", "high", "low", "close", "volume", "amount",
            "ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd",
            "rsi", "kdj_k", "kdj_d", "kdj_j", "boll_mid", "boll_upper", "boll_lower"]
    payload = []
    for _, row in data.iterrows():
        payload.append({c: (None if pd.isna(row[c]) else (float(row[c]) if isinstance(row[c], (int, float)) else str(row[c]))) for c in cols})

    sig = indicator_signals(data).tail(20)
    sig_rows = []
    for i, row in sig.iterrows():
        if row["signal"] != "HOLD":
            sig_rows.append({
                "date": str(data["timestamps"].iloc[i].date()),
                "signal": row["signal"],
                "reason": row["reason"],
                "strength": row["strength"],
            })
    return jsonify({"symbol": symbol, "period": period, "bars": payload, "signals": sig_rows})


# ---- P1: 下探预警 / 尾盘预判 / 午间复盘 / 自适应校准 --------------------------

def _sym_context(symbol: str) -> tuple:
    """取名称 + 板块 + 大盘指数，任一失败都不影响主流程（返回 None）。"""
    name = symbol
    try:
        for w in account.get_watchlist():
            if w["symbol"] == symbol:
                name = w["name"] or symbol
                break
    except Exception:
        pass

    sector = None
    try:
        sector = industry.industry_for(symbol)
    except Exception:
        sector = None

    index_quote = None
    try:
        qs = market.fetch_realtime(["000001"])   # 上证指数
        if qs:
            index_quote = {"name": "上证指数", "pct": qs[0].get("pct")}
    except Exception:
        index_quote = None

    return name, sector, index_quote


def _fetch_1m(symbol, bars=400):
    return market.fetch_kline(symbol, period="1m", bars=bars)


def _no_data(symbol, e=None):
    """1 分钟数据取不到（代码错误/无数据）→ 404 而非 500。

    500 会让前端把它当成服务端故障；这类情况属于「这个标的就是没数据」，
    用 404 语义更准确，前端也能给出更清楚的提示。
    """
    msg = f"未找到 {symbol} 的 1 分钟数据（请确认 6 位代码是否正确）"
    if e is not None:
        msg += f"；原始错误：{str(e)[:120]}"
    return jsonify({"ok": False, "symbol": symbol, "error": msg}), 404


@app.route("/api/dip")
def api_dip():
    """下探过程实时预警：四阶段拆分 + Dip Quality Score（洗盘型/出货型）。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = _fetch_1m(symbol, _int_arg("bars", 400, 30, 1000))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 10:
        return jsonify({"ok": False, "symbol": symbol, "error": "1 分钟数据不足"}), 200

    tuned = None
    try:
        tuned = nightly.get_param
    except Exception:
        pass
    try:
        r = dip.dip_stage(df, dip.dip_params(tuned))
    except Exception as e:
        return jsonify({"error": f"下探分析失败: {e}"}), 500
    r["symbol"] = symbol
    return jsonify(r)


@app.route("/api/tail")
def api_tail():
    """尾盘拉板信号六因子 + 三种结论 + 14:30 预警线。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = _fetch_1m(symbol, _int_arg("bars", 400, 30, 1000))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 20:
        return jsonify({"ok": False, "symbol": symbol, "error": "1 分钟数据不足"}), 200

    name, sector, index_quote = _sym_context(symbol)
    daily = None
    try:
        daily = market.fetch_daily(symbol, bars=60)
    except Exception:
        daily = None

    tuned = None
    try:
        tuned = nightly.get_param
    except Exception:
        pass
    try:
        r = tail.tail_analysis(df, sector=sector, daily=daily,
                               index_quote=index_quote, tuned_getter=tuned)
    except Exception as e:
        return jsonify({"error": f"尾盘分析失败: {e}"}), 500
    r["symbol"] = symbol
    r["name"] = name
    return jsonify(r)


@app.route("/api/noon")
def api_noon():
    """午间快照（11:30 口径）+ 三种剧本假设。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = _fetch_1m(symbol, _int_arg("bars", 400, 30, 1000))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 10:
        return jsonify({"ok": False, "symbol": symbol, "error": "1 分钟数据不足"}), 200

    name, sector, _idx = _sym_context(symbol)
    try:
        r = tail.noon_snapshot(df, sector=sector)
    except Exception as e:
        return jsonify({"error": f"午间复盘失败: {e}"}), 500
    r["symbol"] = symbol
    r["name"] = name
    return jsonify(r)


@app.route("/api/calibration")
def api_calibration():
    """日内自适应校准：按当日波动率缩放异动阈值 + 溯源。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = _fetch_1m(symbol, _int_arg("bars", 400, 30, 1000))
    except Exception as e:
        return _no_data(symbol, e)

    base = anomaly.get_params()
    try:
        r = calibration.calibrate(df,
                                  base_wave_pct=base["wave_pct"]["value"],
                                  base_vol_ratio=base["vol_ratio"]["value"])
    except Exception as e:
        return jsonify({"error": f"校准失败: {e}"}), 500
    r["symbol"] = symbol
    return jsonify(r)


@app.route("/api/p1/overview")
def api_p1_overview():
    """P1 三合一总览：下探阶段 + 尾盘结论 + 午间主剧本（前端一次拉取）。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = _fetch_1m(symbol, _int_arg("bars", 400, 30, 1000))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 20:
        return jsonify({"ok": False, "symbol": symbol, "error": "1 分钟数据不足"}), 200

    name, sector, index_quote = _sym_context(symbol)
    daily = None
    try:
        daily = market.fetch_daily(symbol, bars=60)
    except Exception:
        daily = None
    tuned = None
    try:
        tuned = nightly.get_param
    except Exception:
        pass

    out = {"ok": True, "symbol": symbol, "name": name}
    try:
        out["dip"] = dip.dip_stage(df, dip.dip_params(tuned))
    except Exception as e:
        out["dip"] = {"ok": False, "error": str(e)[:200]}
    try:
        out["tail"] = tail.tail_analysis(df, sector=sector, daily=daily,
                                         index_quote=index_quote, tuned_getter=tuned)
    except Exception as e:
        out["tail"] = {"ok": False, "error": str(e)[:200]}
    try:
        out["noon"] = tail.noon_snapshot(df, sector=sector)
    except Exception as e:
        out["noon"] = {"ok": False, "error": str(e)[:200]}
    try:
        base = anomaly.get_params()
        out["calibration"] = calibration.calibrate(
            df, base_wave_pct=base["wave_pct"]["value"],
            base_vol_ratio=base["vol_ratio"]["value"])
    except Exception as e:
        out["calibration"] = {"applied": False, "reason": str(e)[:200]}
    return jsonify(out)


# ---- P3: 科学依据中心 / 模拟器 / 高级预判 -------------------------------------

@app.route("/api/evidence/params")
def api_evidence_params():
    """参数溯源总览：全系统可调参数 + 来源 + 依据 + 是否被自迭代改动。"""
    try:
        r = evidence_center.provenance_table()
    except Exception as e:
        return jsonify({"ok": False, "error": f"溯源总览生成失败: {e}"}), 500
    return jsonify(r)


@app.route("/api/evidence/diagnose")
def api_evidence_diagnose():
    """诊断面板：基于真实运行统计指出哪个参数可能在拖后腿。"""
    days = _int_arg("days", 30, 1, 365)
    try:
        r = evidence_center.diagnose(days=days)
    except Exception as e:
        return jsonify({"ok": False, "error": f"诊断失败: {e}"}), 500
    return jsonify(r)


@app.route("/api/simulate", methods=["POST"])
def api_simulate():
    """模拟器：用真实历史 K 线对比调参前后的触发次数与方向兑现率。"""
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period="1m",
                                bars=_int_arg("bars", 400, 60, 1000))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 60:
        return jsonify({"ok": False, "symbol": symbol,
                        "error": "1 分钟历史数据不足 60 根，无法模拟"}), 200

    overrides = {}
    for k in ("wave_pct", "vol_ratio", "cool_down_min"):
        if data.get(k) is not None:
            try:
                overrides[k] = float(data[k])
            except (TypeError, ValueError):
                pass
    horizon = _int_arg("horizon", 30, 5, 120)
    try:
        r = evidence_center.simulate(symbol, df, overrides, horizon=horizon)
    except Exception as e:
        return jsonify({"ok": False, "error": f"模拟失败: {e}"}), 500
    return jsonify(r)


@app.route("/api/evidence/schedule")
def api_evidence_schedule():
    """周度校准 / 月度全量回测的到期状态。"""
    return jsonify(evidence_center.calibration_schedule())


@app.route("/api/advanced/momentum")
def api_advanced_momentum():
    """动量衰减检测：价格加速度与拐点提前预警。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period="1m",
                                bars=_int_arg("bars", 400, 30, 1000))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 20:
        return jsonify({"ok": False, "symbol": symbol,
                        "error": "1 分钟数据不足"}), 200
    window = _int_arg("window", 5, 2, 30)
    try:
        r = advanced.momentum_analysis(df, window=window)
    except Exception as e:
        return jsonify({"ok": False, "error": f"动量分析失败: {e}"}), 500
    r["symbol"] = symbol
    return jsonify(r)


@app.route("/api/advanced/orderbook")
def api_advanced_orderbook():
    """盘口异动监测：真实数据不可得 → 明确降级 + 提供代理指标。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    cap = advanced.orderbook_capability()
    out = {"symbol": symbol, "capability": cap}
    try:
        df = market.fetch_kline(symbol, period="1m", bars=240)
        out["proxy"] = advanced.orderbook_proxy(df) if df is not None and len(df) >= 20 \
            else {"ok": False, "error": "数据不足"}
    except Exception as e:
        out["proxy"] = {"ok": False, "error": str(e)[:150]}
    return jsonify(out)


@app.route("/api/advanced/ml")
def api_advanced_ml():
    """ML 概率分：真实历史训练 + 对最新一根打分。样本不足时拒绝训练。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period="1m",
                                bars=_int_arg("bars", 600, 100, 1500))
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 50:
        return jsonify({"ok": False, "symbol": symbol,
                        "error": "1 分钟数据不足 50 根，无法训练（拒绝给出无意义的概率）"}), 200
    horizon = _int_arg("horizon", 30, 5, 120)
    try:
        model = advanced.train_model(df, horizon=horizon)
    except Exception as e:
        return jsonify({"ok": False, "error": f"训练失败: {e}"}), 500
    if not model.get("ok"):
        return jsonify({"ok": False, "symbol": symbol, **model}), 200
    try:
        pred = advanced.ml_predict(model, df)
    except Exception as e:
        pred = {"ok": False, "error": str(e)[:150]}
    # 不返回 mu/sd/weights 原始数组之外的大对象时也保留，便于复现
    return jsonify({"ok": True, "symbol": symbol, "model": model, "prediction": pred})


@app.route("/api/p3/overview")
def api_p3_overview():
    """P3 三合一：动量 + 盘口能力 + ML 概率分（前端一次拉取）。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period="1m", bars=600)
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 30:
        return jsonify({"ok": False, "symbol": symbol, "error": "1 分钟数据不足"}), 200

    out = {"ok": True, "symbol": symbol}
    try:
        out["momentum"] = advanced.momentum_analysis(df)
    except Exception as e:
        out["momentum"] = {"ok": False, "error": str(e)[:150]}
    out["orderbook"] = {"capability": advanced.orderbook_capability()}
    try:
        out["orderbook"]["proxy"] = advanced.orderbook_proxy(df)
    except Exception as e:
        out["orderbook"]["proxy"] = {"ok": False, "error": str(e)[:150]}
    try:
        model = advanced.train_model(df)
        out["ml"] = {"model": model, "prediction": advanced.ml_predict(model, df)}
    except Exception as e:
        out["ml"] = {"model": {"ok": False, "error": str(e)[:150]},
                     "prediction": {"ok": False, "error": str(e)[:150]}}
    return jsonify(out)


# ---- 实时盯盘（交易舱 v1 / P0）-----------------------------------------------

def _watch_symbols() -> list:
    """盯盘标的 = 自选股。DSA 未配置时直接用本地自选。"""
    try:
        return [w["symbol"] for w in account.get_watchlist()]
    except Exception:
        return []


def _watch_engine():
    """取（或惰性创建）盯盘引擎，并绑定自选来源。"""
    return watch_engine.get_engine(symbols_provider=_watch_symbols)


@app.route("/api/watch/status")
def api_watch_status():
    """盯盘引擎状态：运行中/轮询次数/退避/并发/缓存/行情时效。"""
    try:
        return jsonify(_watch_engine().status())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/watch/start", methods=["POST"])
def api_watch_start():
    eng = _watch_engine()
    r = eng.start()
    return jsonify({**r, "running": eng.is_running()})


@app.route("/api/watch/stop", methods=["POST"])
def api_watch_stop():
    eng = _watch_engine()
    r = eng.stop()
    return jsonify({**r, "running": eng.is_running()})


@app.route("/api/watch/quotes")
def api_watch_quotes():
    """盯盘行情（带 5 秒缓存）。含 fields[30] 行情时间与距今秒数。"""
    raw = request.args.get("symbols", "").strip()
    symbols = [s for s in raw.split(",") if s] if raw else _watch_symbols()
    if not symbols:
        return jsonify({"ok": True, "quotes": [], "note": "自选股为空"})
    use_cache = request.args.get("cache", "1") == "1"
    try:
        quotes = _watch_engine().client.fetch(symbols, use_cache=use_cache)
    except Exception as e:
        return jsonify({"ok": False, "error": f"抓取行情失败: {e}"}), 500

    now = datetime.now()
    out = []
    for q in quotes:
        item = dict(q)
        item["age_sec"] = tencent_quote.quote_age_sec(q, now)
        item["fresh"] = tencent_quote.is_fresh(q, max_age_sec=300, now=now)
        out.append(item)
    return jsonify({
        "ok": True,
        "quotes": out,
        "session": tencent_quote.trading_session(now),
        "source": "qt.gtimg.cn",
        "time_basis": "fields[30]",
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/watch/events")
def api_watch_events():
    """拉取预警事件（默认取走并清空；peek=1 则只读）。"""
    eng = _watch_engine()
    limit = _int_arg("limit", 50, 1, 200)
    peek = request.args.get("peek", "0") == "1"
    events = eng.peek_events(limit) if peek else eng.drain_events(limit)
    return jsonify({"ok": True, "events": events, "count": len(events),
                    "peek": peek})


@app.route("/api/watch/events/clear", methods=["POST"])
def api_watch_events_clear():
    return jsonify({"ok": True, "cleared": _watch_engine().clear_events()})


@app.route("/api/watch/check", methods=["POST"])
def api_watch_check():
    """立即检查一次（不改变周期节奏），用于手动验证。"""
    # 容错：未带 body 或 Content-Type 不匹配时不应报 400/415
    data = request.get_json(silent=True) or {}
    syms = data.get("symbols") or None
    if isinstance(syms, str):
        syms = [s for s in syms.split(",") if s]
    try:
        events = _watch_engine().check_now(syms)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "events": events, "count": len(events)})


@app.route("/api/watch/config")
def api_watch_config():
    """盯盘配置：tick 间隔、每只股票的周期与规则。"""
    cfg = _watch_engine().config
    data = cfg.all()
    symbols = _watch_symbols()
    items = []
    for s in symbols:
        code = tencent_quote.bare_code(s)
        it = data["items"].get(code, {})
        items.append({
            "symbol": code,
            "period_min": cfg.period(code),
            "rules": cfg.rules(code),
            "configured": code in data["items"],
        })
    return jsonify({
        "ok": True,
        "tick_sec": data["tick_sec"],
        "enabled": data["enabled"],
        "allowed_periods": list(watch_engine.ALLOWED_PERIODS),
        "default_rules": watch_engine.DEFAULT_RULES,
        "items": items,
    })


@app.route("/api/watch/config", methods=["POST"])
def api_watch_config_set():
    """设置：单只股票周期 / 规则，或全局 tick 间隔 / 开关。"""
    data = request.get_json() or {}
    eng = _watch_engine()
    cfg = eng.config
    out = {}

    if data.get("symbol") and data.get("period_min") is not None:
        try:
            out["period"] = cfg.set_period(data["symbol"], data["period_min"])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    if data.get("symbol") and isinstance(data.get("rules"), dict):
        out["rules"] = cfg.set_rules(data["symbol"], data["rules"])

    if data.get("tick_sec") is not None:
        out["tick"] = cfg.set_tick(data["tick_sec"])

    if data.get("enabled") is not None:
        out["enabled"] = cfg.set_enabled(bool(data["enabled"]))

    if data.get("interval_ms") is not None:
        try:
            ms = int(data["interval_ms"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "interval_ms 必须是整数"}), 400
        ms = max(100, min(10000, ms))
        eng.client.limiter.set_interval(ms)
        out["interval_ms"] = ms

    if not out:
        return jsonify({"ok": False, "error": "没有可应用的配置项"}), 400
    return jsonify({"ok": True, **out, "config": cfg.all()})


@app.route("/api/probe/run", methods=["POST"])
def api_probe_run():
    """频次探针：实测不同间隔的成功率/耗时/限流。"""
    data = request.get_json() or {}
    tiers = data.get("tiers")
    if isinstance(tiers, str):
        tiers = [tiers]
    samples = data.get("samples", 10)
    symbols = data.get("symbols")
    if isinstance(symbols, str):
        symbols = [s for s in symbols.split(",") if s]
    symbols = symbols or _watch_symbols()

    block = data.get("block", True)
    try:
        r = freq_probe.run_probe(symbols, tier_keys=tiers,
                                 samples=samples, block=block)
    except Exception as e:
        return jsonify({"ok": False, "error": f"探针执行失败: {e}"}), 500
    return jsonify(r)


@app.route("/api/probe/report")
def api_probe_report():
    return jsonify(freq_probe.last_report())


@app.route("/api/probe/abort", methods=["POST"])
def api_probe_abort():
    return jsonify(freq_probe.abort_probe())


@app.route("/api/watch/overview")
def api_watch_overview():
    """盯盘页一次拉全：行情 + 引擎状态 + 配置 + 待消费事件（不取走）。"""
    symbols = _watch_symbols()
    eng = _watch_engine()
    now = datetime.now()

    quotes = []
    try:
        quotes = eng.client.fetch(symbols) if symbols else []
    except Exception:
        quotes = []
    for q in quotes:
        q["age_sec"] = tencent_quote.quote_age_sec(q, now)
        q["fresh"] = tencent_quote.is_fresh(q, max_age_sec=300, now=now)

    cfg = eng.config
    items = []
    for s in symbols:
        code = tencent_quote.bare_code(s)
        items.append({"symbol": code, "period_min": cfg.period(code),
                      "rules": cfg.rules(code)})

    st = eng.status()
    return jsonify({
        "ok": True,
        "quotes": quotes,
        "config": {"tick_sec": cfg.all()["tick_sec"],
                   "enabled": cfg.all()["enabled"],
                   "allowed_periods": list(watch_engine.ALLOWED_PERIODS),
                   "items": items},
        "engine": {
            "running": st.get("running"),
            "tick_count": st.get("tick_count"),
            "alert_count": st.get("alert_count"),
            "consecutive_failures": st.get("consecutive_failures"),
            "recovered_count": st.get("recovered_count"),
            "pending_events": st.get("pending_events"),
            "backoff_remaining": st.get("backoff_remaining"),
        },
        "events": eng.peek_events(50),
        "session": tencent_quote.trading_session(now),
        "time_basis": "fields[30]",
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
    })


# ---- forecast -----------------------------------------------------------------


@app.route("/api/anomaly")
def api_anomaly():
    """盘中变化时刻线 + 前置预警线 + 依据卡片 + 参数溯源 + 回测参考。

    基于 1 分钟 K 线。Level-2 类因子（撤单率/大单流向/主动买卖比/封单比）
    在本机数据源下不可得，结果里以 degraded 显式标注，不编造数值。
    """
    symbol = request.args.get("symbol", "")
    bars = _int_arg("bars", 400, 20, 1000)
    force = request.args.get("force", "0") == "1"
    with_bt = request.args.get("backtest", "1") == "1"
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period="1m", bars=bars, force=force)
    except Exception as e:
        return _no_data(symbol, e)
    if df is None or len(df) < 5:
        return jsonify({"ok": False, "symbol": symbol,
                        "error": "1 分钟 K 线数据不足，无法检测异动"}), 200

    name = symbol
    try:
        for w in account.get_watchlist():
            if w["symbol"] == symbol:
                name = w["name"] or symbol
                break
    except Exception:
        pass

    tuned = None
    try:
        tuned = nightly.get_param
    except Exception:
        pass

    try:
        result = anomaly.analyze(symbol, df, name=name, tuned_getter=tuned,
                                 with_backtest=with_bt)
    except Exception as e:
        return jsonify({"error": f"异动分析失败: {e}"}), 500
    return jsonify(result)


@app.route("/api/anomaly/params")
def api_anomaly_params():
    """参数溯源表：每个阈值的生效值、来源、依据、样本量。"""
    tuned = None
    try:
        tuned = nightly.get_param
    except Exception:
        pass
    return jsonify({
        "params": list(anomaly.get_params(tuned).values()),
        "degraded": anomaly.unavailable_factors(),
        "defaults": {k: {"value": v[0], "unit": v[1], "source": v[2], "basis": v[3]}
                     for k, v in anomaly.DEFAULTS.items()},
    })


@app.route("/api/anomaly/watchlist")
def api_anomaly_watchlist():
    """批量扫描自选股，返回每只的异动摘要（供总览与预警面板用）。"""
    limit = _int_arg("limit", 8, 1, 40)
    out = []
    try:
        watch = account.get_watchlist()[:limit]
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    for w in watch:
        sym = w["symbol"]
        try:
            df = market.fetch_kline(sym, period="1m", bars=240)
            if df is None or len(df) < 5:
                out.append({"symbol": sym, "name": w["name"], "ok": False,
                            "error": "数据不足"})
                continue
            r = anomaly.analyze(sym, df, name=w["name"], with_backtest=False)
            card = r.get("card") or {}
            evs = r.get("events") or []
            out.append({
                "symbol": sym,
                "name": w["name"],
                "ok": True,
                "event_count": len(evs),
                "score": card.get("score"),
                "level": card.get("level"),
                "level_label": card.get("level_label"),
                "latest": evs[-1] if evs else None,
                "conclusion": card.get("conclusion"),
            })
        except Exception as e:
            out.append({"symbol": sym, "name": w.get("name"), "ok": False,
                        "error": str(e)[:120]})
    ranked = sorted([o for o in out if o.get("ok")],
                    key=lambda x: x.get("score") or 0, reverse=True)
    return jsonify({"items": out, "ranked": ranked, "count": len(out)})


@app.route("/api/anomaly/scan")
def api_anomaly_scan():
    """扫描重点标的的剧烈异动，返回需要「全屏爆红/爆绿」的事件。

    前端轮询本接口；服务端只做判定与依据组装，是否真正全屏由前端
    （冷静期 / 免打扰 / 用户静默）决定——避免服务端无法感知用户已关闭。
    """
    threshold = _float_arg("threshold", 70.0, 0.0, 100.0)
    limit = _int_arg("limit", 5, 1, 20)
    try:
        watch = account.get_watchlist()[:limit]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    triggers = []
    for w in watch:
        sym, nm = w["symbol"], w["name"] or w["symbol"]
        try:
            df = market.fetch_kline(sym, period="1m", bars=240)
            if df is None or len(df) < 5:
                continue
            r = anomaly.analyze(sym, df, name=nm, with_backtest=False)
            card = r.get("card") or {}
            evs = r.get("events") or []
            if not evs:
                continue
            score = card.get("score") or 0
            last = evs[-1]
            # 触发条件：评分过阈值 且 最新异动振幅显著（真正的剧烈波动）
            if score < threshold or float(last.get("magnitude") or 0) < 1.0:
                continue
            up = last["direction"] == "up"
            triggers.append({
                "symbol": sym, "name": nm, "score": score,
                "level": card.get("level"), "level_label": card.get("level_label"),
                "direction": last["direction"],
                "event_ts": last["ts"],
                "reason": last["reason"],
                "magnitude": last.get("magnitude"),
                "vol_ratio": last.get("vol_ratio"),
                "title": ("🚀 " + nm + " 快速拉升") if up else ("🔥 " + nm + " 剧烈下杀"),
                "sub": f"{last['ts']} · {last['reason']} · 强度 {score} 分",
                "factors": card.get("factors") or [],
                "risks": card.get("risks") or [],
                "backtest": card.get("backtest"),
                "degraded": r.get("degraded") or [],
            })
        except Exception:
            continue
    return jsonify({"triggers": triggers, "scanned": len(watch),
                    "threshold": threshold})


@app.route("/api/forecast")
def api_forecast():
    symbol = request.args.get("symbol", "")
    pred_len = _int_arg("pred_len", 120, 1, MAX_PRED_LEN)
    period = request.args.get("period", "day")
    T = _float_arg("T", 1.3, 0.1, 5.0)
    top_p = _float_arg("top_p", 0.95, 0.01, 1.0)
    sample_count = _int_arg("sample_count", 2, 1, MAX_SAMPLE_COUNT)
    force = request.args.get("force", "0") == "1"
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400

    try:
        df = market.fetch_kline(symbol, period=period, bars=600, force=force)
        result = kronos_service.forecast(symbol, df, lookback=400, pred_len=pred_len,
                                         T=T, top_p=top_p, sample_count=sample_count)
        result["period"] = period
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/forecast/signal")
def api_forecast_signal():
    symbol = request.args.get("symbol", "")
    period = request.args.get("period", "day")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period=period, bars=600)
        fc = kronos_service.forecast(symbol, df, lookback=400, pred_len=120)
        dfe = ind.all_indicators(df)
        sig = forecast_direction_signals(dfe, fc)
        sig.update({"last_close": fc["last_close"], "end_close": fc["end_close"],
                    "change_pct": fc["change_pct"], "device": fc["device"]})
        return jsonify(sig)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- buy/sell points -----------------------------------------------------------


@app.route("/api/buysell")
def api_buysell():
    symbol = request.args.get("symbol", "")
    period = request.args.get("period", "day")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_kline(symbol, period=period, bars=600)
        fc = kronos_service.forecast(symbol, df, lookback=400, pred_len=120)
        plan = buysell.buy_sell_plan(df, forecast=fc)
        plan["symbol"] = symbol
        plan["period"] = period
        return jsonify(plan)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- accuracy -------------------------------------------------------------------


@app.route("/api/accuracy")
def api_accuracy():
    symbol = request.args.get("symbol", "")
    period = request.args.get("period", "day")
    pred_len = _int_arg("pred_len", 60, 1, MAX_PRED_LEN)
    rounds = _int_arg("rounds", 3, 1, MAX_ROUNDS)
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        result = accuracy.evaluate(symbol, period=period, lookback=400,
                                   pred_len=pred_len, rounds=rounds, verbose=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- compare --------------------------------------------------------------------


@app.route("/api/compare")
def api_compare():
    raw = [s for s in request.args.get("symbols", "").split(",") if s]
    period = request.args.get("period", "day")
    bars = _int_arg("bars", 250, 10, 1000)
    if len(raw) < 2:
        return jsonify({"error": "至少需要 2 只股票"}), 400

    # resolve codes or names
    symbols = []
    names0 = {}
    for q in raw:
        try:
            r = market.resolve_symbol(q)
            symbols.append(r["symbol"])
            names0[r["symbol"]] = r["name"]
        except ValueError as e:
            return jsonify({"error": f"无法识别「{q}」: {e}"}), 400

    try:
        series = []
        for sym in symbols:
            df = market.fetch_kline(sym, period=period, bars=bars)
            q = market.fetch_realtime_one(sym)
            name = q["name"] if q else names0.get(sym, sym)
            base = df["close"].iloc[0]
            series.append({
                "symbol": sym,
                "name": name,
                "dates": [str(d.date()) for d in df["timestamps"]],
                "close": [round(float(v), 3) for v in df["close"]],
                "normalized": [round((float(v) / base - 1) * 100, 2) for v in df["close"]],
                "last_close": round(float(df["close"].iloc[-1]), 3),
                "return_pct": round((float(df["close"].iloc[-1]) / base - 1) * 100, 2),
            })
        return jsonify({"symbols": series, "period": period})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- report export --------------------------------------------------------------


@app.route("/api/report")
def api_report():
    symbol = request.args.get("symbol", "")
    period = request.args.get("period", "day")
    fmt = request.args.get("format", "json")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400

    from flask import Response
    try:
        df = market.fetch_kline(symbol, period=period, bars=600)
        fc = kronos_service.forecast(symbol, df, lookback=400, pred_len=120)
        plan = buysell.buy_sell_plan(df, forecast=fc)
        dfe = ind.all_indicators(df)
        sig = forecast_direction_signals(dfe, fc)

        if fmt == "csv":
            # CSV with forecast table
            lines = [f"# Kronos Trading Report - {symbol} ({period})",
                     f"# Generated: {pd.Timestamp.now()}",
                     f"# Last close: {fc['last_close']}  Pred end: {fc['end_close']}  ({fc['change_pct']}%)",
                     f"# Action: {plan['action']}  Score: {plan['score']}",
                     f"# Stop loss: {plan['stop_loss']}  Target up: {plan['target_up']}  Target down: {plan['target_down']}",
                     "",
                     "date,open,high,low,close,volume"]
            for i, d in enumerate(fc["dates"]):
                lines.append(f"{d},{fc['open'][i]},{fc['high'][i]},{fc['low'][i]},{fc['close'][i]},{fc['volume'][i]}")
            body = "\n".join(lines)
            return Response(
                body, mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=kronos_report_{symbol}_{period}.csv"})

        # default JSON report
        report = {
            "symbol": symbol, "period": period, "generated": str(pd.Timestamp.now()),
            "forecast": fc, "buysell": plan, "signal": sig,
        }
        return jsonify(report)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- watchlist ----------------------------------------------------------------


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    account.init_db()
    sort = request.args.get("sort", "pct")   # pct / volume_ratio / turnover / name
    grouped = request.args.get("group", "0") == "1"
    items = account.get_watchlist()
    symbols = [w["symbol"] for w in items]
    quotes = market.fetch_realtime(symbols)
    qmap = {q["symbol"]: q for q in quotes}
    for w in items:
        q = qmap.get(w["symbol"])
        if q:
            w.update({
                "price": q["price"], "pct": q["pct"], "name": q["name"],
                "change": q["change"], "turnover": q.get("turnover"),
                "volume_ratio": q.get("volume_ratio"),
                "high": q["high"], "low": q["low"], "open": q["open"],
            })
            # 异动标记
            flags = []
            if q["pct"] is not None:
                if q["pct"] >= 9.8:
                    flags.append("涨停")
                elif q["pct"] >= 5:
                    flags.append("大涨")
                elif q["pct"] <= -9.8:
                    flags.append("跌停")
                elif q["pct"] <= -5:
                    flags.append("大跌")
            if q.get("volume_ratio") and q["volume_ratio"] >= 3:
                flags.append("放量")
            elif q.get("volume_ratio") and q["volume_ratio"] >= 1.5:
                flags.append("量增")
            w["flags"] = flags
        else:
            w.update({"price": None, "pct": None, "name": w["name"], "flags": []})

    def _key(w):
        v = w.get(sort)
        return v if isinstance(v, (int, float)) else -999
    items.sort(key=_key, reverse=True)

    if not grouped:
        return jsonify({"watchlist": items, "sort": sort})

    # group by track (grp column), with per-track heat stats
    tracks = {}
    for w in items:
        g = (w.get("grp") or "").strip() or "未分组"
        tracks.setdefault(g, []).append(w)

    groups = []
    for g, members in tracks.items():
        pcts = [m["pct"] for m in members if m.get("pct") is not None]
        up = sum(1 for p in pcts if p > 0)
        down = sum(1 for p in pcts if p < 0)
        avg = sum(pcts) / len(pcts) if pcts else None
        leader = max(members, key=lambda m: m.get("pct") or -999)
        groups.append({
            "name": g,
            "members": members,
            "count": len(members),
            "up": up, "down": down,
            "avg_pct": round(avg, 2) if avg is not None else None,
            "leader": {"symbol": leader["symbol"], "name": leader["name"],
                       "pct": leader.get("pct")},
        })
    # sort tracks by average pct desc
    groups.sort(key=lambda g: g["avg_pct"] if g["avg_pct"] is not None else -999, reverse=True)
    return jsonify({"groups": groups, "sort": sort, "grouped": True})


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    data = request.get_json() or {}
    query = str(data.get("symbol", "")).strip()
    name = data.get("name", "").strip()
    if not query:
        return jsonify({"error": "missing symbol"}), 400

    # resolve code or Chinese name to a proper 6-digit A-share symbol
    try:
        resolved = market.resolve_symbol(query)
        symbol = resolved["symbol"]
        if not name:
            name = resolved["name"]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # avoid duplicates
    for w in account.get_watchlist():
        if w["symbol"] == symbol:
            return jsonify({"ok": True, "message": f"{name} ({symbol}) 已在自选列表中"})

    grp = str(data.get("grp", "")).strip()
    account.add_watch(symbol, name, grp=grp)
    return jsonify({"ok": True, "message": f"已添加 {name} ({symbol})", "symbol": symbol, "name": name})


@app.route("/api/watchlist/<symbol>/group", methods=["POST"])
def api_watchlist_set_group(symbol):
    data = request.get_json() or {}
    grp = str(data.get("grp", "")).strip()
    account.set_watch_group(symbol, grp)
    return jsonify({"ok": True, "message": f"{symbol} 已归入「{grp or '未分组'}」"})


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def api_watchlist_del(symbol):
    account.remove_watch(symbol)
    return jsonify({"ok": True})


# ---- account & trading ----------------------------------------------------------


@app.before_request
def _set_account_context():
    """Pick the current simulated account: X-Account-Id header wins, else the
    persisted global selection (set via /api/accounts/switch)."""
    aid = request.headers.get("X-Account-Id")
    if aid:
        try:
            account.set_current_account(int(aid))
            return
        except (TypeError, ValueError):
            pass
    # no header: use the persisted selection without re-saving it
    account._tls.account_id = account._persisted_account_id()


@app.route("/api/accounts")
def api_accounts():
    """List all simulated accounts."""
    return jsonify({"accounts": account.list_accounts(),
                    "current": account.current_account_id()})


@app.route("/api/accounts", methods=["POST"])
def api_accounts_create():
    data = request.get_json() or {}
    name = str(data.get("name", "新账户")).strip() or "新账户"
    try:
        initial_cash = float(data.get("initial_cash", account.INITIAL_CASH))
    except (TypeError, ValueError):
        initial_cash = account.INITIAL_CASH
    acc = account.create_account(name=name, initial_cash=initial_cash)
    return jsonify({"ok": True, "message": f"账户「{acc['name']}」已创建 (ID {acc['id']})",
                    "account": acc})


@app.route("/api/accounts/<int:aid>", methods=["DELETE"])
def api_accounts_delete(aid):
    return jsonify(account.delete_account(aid))


@app.route("/api/accounts/<int:aid>/rename", methods=["POST"])
def api_accounts_rename(aid):
    data = request.get_json() or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "缺少名称"}), 400
    return jsonify(account.rename_account(aid, name))


@app.route("/api/accounts/switch", methods=["POST"])
def api_accounts_switch():
    data = request.get_json() or {}
    try:
        aid = int(data.get("account_id", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "account_id 无效"}), 400
    exists = [a for a in account.list_accounts() if a["id"] == aid]
    if not exists:
        return jsonify({"error": f"账户 {aid} 不存在"}), 404
    account.set_current_account(aid)
    return jsonify({"ok": True, "message": f"已切换到账户「{exists[0]['name']}」", "account_id": aid})


# ---- auto trading takeover ---------------------------------------------------------


@app.route("/api/auto/rules", methods=["GET"])
def api_auto_rules():
    return jsonify({"rules": auto_trade.list_rules()})


@app.route("/api/auto/rules", methods=["POST"])
def api_auto_rules_add():
    data = request.get_json() or {}
    symbol = str(data.get("symbol", "")).strip()
    trigger_type = str(data.get("trigger_type", "")).strip()
    action = str(data.get("action", "")).upper()
    if not symbol or trigger_type not in ("above_pct", "below_pct", "volume_surge", "signal_buy", "signal_sell") \
            or action not in ("BUY", "SELL"):
        return jsonify({"error": "参数错误：symbol/trigger_type/action 无效"}), 400
    try:
        aid = int(data.get("account_id", 1))
        value = float(data.get("trigger_value", 0))
        shares = float(data.get("shares", 0) or 0)
        amount = float(data.get("amount_value", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "数值参数错误"}), 400
    name = data.get("name", "")
    if not name:
        q = market.fetch_realtime_one(symbol)
        name = q["name"] if q else symbol
    res = auto_trade.add_rule(aid, symbol, name, trigger_type, value, action, shares, amount)
    return jsonify(res)


@app.route("/api/auto/rules/<int:rid>", methods=["DELETE"])
def api_auto_rules_delete(rid):
    auto_trade.delete_rule(rid)
    return jsonify({"ok": True})


@app.route("/api/auto/rules/<int:rid>/toggle", methods=["POST"])
def api_auto_rules_toggle(rid):
    data = request.get_json() or {}
    auto_trade.update_rule(rid, enabled=data.get("enabled", True))
    return jsonify({"ok": True})


@app.route("/api/auto/logs")
def api_auto_logs():
    return jsonify({"logs": auto_trade.get_logs()})


# ---- auto stock recommendation ---------------------------------------------------


@app.route("/api/recommend")
def api_recommend():
    """Full-market leaderboard recommendation (top A-share gainers, scored)."""
    top_n = _int_arg("top_n", 5, 1, 10)
    use_forecast = request.args.get("forecast", "1") != "0"
    force = request.args.get("force", "0") == "1"
    pool = _int_arg("pool", 40, 10, 100)
    try:
        data = recommend.recommend(top_n=top_n, use_forecast=use_forecast,
                                   force=force, pool_size=pool)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news")
def api_news():
    """Daily top finance headlines (5 items)."""
    force = request.args.get("force", "0") == "1"
    limit = _int_arg("limit", 5, 1, 10)
    try:
        return jsonify({"news": recommend.daily_news(limit=limit, force=force)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==== 投研智能体（数字员工团队）====================================================


@app.route("/api/research/agents")
def api_research_agents():
    """List the 9 digital-employee roles."""
    return jsonify({
        "agents": [{"id": r.id, "name": r.name, "title": r.title,
                    "focus": r.focus, "stance": r.stance, "is_chair": r.is_chair}
                   for r in AGENT_ROLES],
        "llm": {"available": llm_adapter.is_available(),
                "engine": "llm" if llm_adapter.is_available() else "rule"},
    })


@app.route("/api/research/requirements")
def api_research_requirements():
    """需求清单逐条对照表（需求原文 → 实现位置 → 网页可见位置 → 状态）。"""
    return jsonify(requirements_map.all_requirements())


@app.route("/api/llm/status")
def api_llm_status():
    return jsonify(deepseek.status())


@app.route("/api/llm/config", methods=["GET", "POST"])
def api_llm_config():
    if request.method == "GET":
        cfg = deepseek.load_config()
        cfg.pop("api_key", None)
        cfg["key_masked"] = deepseek.status().get("key_masked")
        return jsonify(cfg)
    data = request.get_json() or {}
    kw = {}
    for k in ("model", "base_url", "temperature", "max_tokens", "concurrency", "cache_ttl", "timeout"):
        if k in data:
            kw[k] = data[k]
    if data.get("api_key"):
        kw["api_key"] = data["api_key"]
        kw["enabled"] = True
    if data.get("enabled") is not None:
        kw["enabled"] = bool(data["enabled"])
    deepseek.save_config(**kw)
    return jsonify({"ok": True, "status": deepseek.status()})


@app.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    """探活：发一条最小请求验证 DeepSeek 是否可用。"""
    res = deepseek.chat([{"role": "system", "content": "你是一个测试助手。"},
                         {"role": "user", "content": "只回复四个字：连接正常"}],
                        max_tokens=20, tag="ping", use_cache=False)
    return jsonify({"ok": res["ok"], "reply": res["text"].strip()[:60],
                    "error": res["error"], "latency": round(res["latency"], 2),
                    "status": deepseek.status()})


# ---- 宏观 / 利率 ----

@app.route("/api/macro")
def api_macro():
    return jsonify(macro.snapshot())


@app.route("/api/macro/brief", methods=["POST"])
def api_macro_brief():
    data = request.get_json() or {}
    sym = str(data.get("symbol") or "").strip()
    res = macro.llm_brief(sym, data.get("industry") or "")
    res["snapshot"] = macro.snapshot()
    return jsonify(res)


# ---- 行业景气度 ----

@app.route("/api/industry/ranking")
def api_industry_ranking():
    limit = _int_arg("limit", 30, 1, 200)
    return jsonify({"ranking": industry.ranking(limit), "generated": _now_str()})


@app.route("/api/industry/<symbol>")
def api_industry_one(symbol):
    return jsonify(industry.industry_for(symbol))


@app.route("/api/industry/brief", methods=["POST"])
def api_industry_brief():
    data = request.get_json() or {}
    return jsonify(industry.llm_brief(str(data.get("symbol") or "").strip(),
                                      data.get("industry") or ""))


# ---- 舆情情感分析 ----

@app.route("/api/sentiment/<symbol>")
def api_sentiment(symbol):
    use_llm = request.args.get("llm", "1") != "0"
    return jsonify(sentiment.analyze(symbol, use_llm=use_llm))


@app.route("/api/sentiment/brief", methods=["POST"])
def api_sentiment_brief():
    data = request.get_json() or {}
    sym = str(data.get("symbol") or "").strip()
    if not sym:
        return jsonify({"error": "请提供 symbol"}), 400
    result = sentiment.analyze(sym, use_llm=True)
    brief = sentiment.llm_brief(result)
    brief["result"] = result
    return jsonify(brief)


# ---- 券商研报数据库 ----

@app.route("/api/reports/<symbol>")
def api_reports(symbol):
    days = _int_arg("days", 180, 7, 730)
    limit = _int_arg("limit", 15, 1, 50)
    return jsonify({"symbol": symbol, "reports": research_db.stock_reports(symbol, days, limit),
                    "ratings": research_db.ratings_summary(symbol, days)})


@app.route("/api/reports/industry")
def api_reports_industry():
    industry_name = request.args.get("industry") or ""
    days = _int_arg("days", 60, 7, 365)
    return jsonify({"industry": industry_name,
                    "reports": research_db.industry_reports(industry_name, days, 15),
                    "strategy": research_db.strategy_reports(30, 8)})


@app.route("/api/reports/brief", methods=["POST"])
def api_reports_brief():
    data = request.get_json() or {}
    return jsonify(research_db.llm_brief(str(data.get("symbol") or "").strip(),
                                         data.get("industry") or ""))


# ---- 夜间自迭代 ----

@app.route("/api/nightly/params")
def api_nightly_params():
    return jsonify({"params": nightly.param_table(), "updated": nightly.load_tuning().get("updated"),
                    "last_iteration": nightly.load_tuning().get("last_iteration")})


@app.route("/api/nightly/iterations")
def api_nightly_iterations():
    return jsonify({"iterations": nightly.list_iterations(_int_arg("limit", 20, 1, 100))})


@app.route("/api/nightly/iterations/<int:rid>")
def api_nightly_iteration(rid):
    it = nightly.get_iteration(rid)
    if not it:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify(it)


@app.route("/api/nightly/run", methods=["POST"])
def api_nightly_run():
    """手动触发一次自迭代（dry_run=1 只给建议不生效）。"""
    data = request.get_json() or {}
    dry = bool(data.get("dry_run"))
    res = nightly.run_iteration(trigger="manual", dry_run=dry)
    return jsonify(res)


@app.route("/api/nightly/reset", methods=["POST"])
def api_nightly_reset():
    nightly.reset_tuning()
    return jsonify({"ok": True, "params": nightly.param_table()})


# ---- 买卖点 AI 解读 ----

@app.route("/api/buysell/reading", methods=["POST"])
def api_buysell_reading():
    data = request.get_json() or {}
    sym = str(data.get("symbol") or "").strip()
    if not sym:
        return jsonify({"error": "请提供 symbol"}), 400
    try:
        df = market.fetch_kline(sym, period=data.get("period") or "day",
                                bars=_int_arg("bars", 400, 60, 800))
        fc = None
        if data.get("forecast", True):
            try:
                fc = kronos_service.forecast(sym, df, lookback=400, pred_len=30)
            except Exception:
                fc = None
        plan = buysell.buy_sell_plan(df, forecast=fc)
    except Exception as e:
        return jsonify({"error": f"计划生成失败：{e}"}), 400

    extra = {}
    try:
        extra["industry"] = industry.industry_for(sym)
    except Exception:
        pass
    try:
        extra["sentiment"] = sentiment.analyze(sym, use_llm=True, limit=6)
    except Exception:
        pass
    try:
        extra["macro"] = (macro.snapshot().get("cards") or [])
    except Exception:
        pass
    if fc:
        extra["forecast"] = (f"{fc.get('change_pct'):+.1f}%（误差带 ±"
                             f"{((fc.get('confidence') or {}).get('mae_pct', 0.15)) * 100:.0f}%）")
    name = ""
    try:
        name = (market.fetch_realtime_one(sym) or {}).get("name") or ""
    except Exception:
        pass
    res = buysell.llm_reading(plan, symbol=sym, name=name, extra=extra)
    res["plan"] = plan
    return jsonify(res)


@app.route("/api/research/meeting", methods=["POST"])
def api_research_meeting():
    """Run a full 4-round research committee meeting."""
    data = request.get_json() or {}
    symbols = data.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbols:
        # default: current watchlist (max 20)
        symbols = [w["symbol"] for w in account.get_watchlist()][:20]
    if not symbols:
        return jsonify({"error": "请提供标的（symbols）或先添加自选股"}), 400
    if len(symbols) > 20:
        symbols = symbols[:20]
    with_forecast = data.get("forecast", True)
    name = str(data.get("name", "")).strip() or None
    try:
        minutes = committee.run_committee(symbols, session_name=name,
                                         with_forecast=bool(with_forecast))
        outputs = research_report.build_all(minutes)
        return jsonify({"ok": True, "minutes": minutes, "outputs": outputs})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"投研会执行失败: {e}"}), 500


@app.route("/api/research/sessions")
def api_research_sessions():
    """List past research sessions (audit trail index)."""
    return jsonify({"sessions": audit.list_sessions()})


@app.route("/api/research/sessions/<int:sid>")
def api_research_session(sid):
    """Full audit trail of one session."""
    s = audit.get_session(sid)
    if not s:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(s)


@app.route("/api/research/sessions/<int:sid>", methods=["DELETE"])
def api_research_session_delete(sid):
    audit.delete_session(sid)
    return jsonify({"ok": True})


@app.route("/api/research/analytics", methods=["POST"])
def api_research_analytics():
    """Professional analytics layer: correlation / attribution / monte-carlo / risk / rebalance."""
    data = request.get_json() or {}
    symbols = data.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "请提供标的"}), 400
    symbols = symbols[:20]
    out = {}
    try:
        out["correlation"] = analytics.correlation_matrix(symbols)
    except Exception as e:
        out["correlation"] = {"error": str(e)}
    try:
        out["attribution"] = analytics.attribution(symbols)
    except Exception as e:
        out["attribution"] = {"error": str(e)}
    try:
        n_sim = _int_arg("n_sim", 10000, 1000, 50000)
        out["monte_carlo"] = analytics.monte_carlo(symbols, n_sim=n_sim, horizon=30)
    except Exception as e:
        out["monte_carlo"] = {"error": str(e)}
    try:
        out["risk_scan"] = analytics.risk_scan(symbols)
    except Exception as e:
        out["risk_scan"] = {"error": str(e)}
    try:
        out["rebalance"] = analytics.build_rebalance_list(symbols)
    except Exception as e:
        out["rebalance"] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/research/compliance", methods=["POST"])
def api_research_compliance():
    """Scan arbitrary text for compliance violations."""
    data = request.get_json() or {}
    text = str(data.get("text", ""))
    return jsonify(compliance.scan_text(text))


@app.route("/api/research/llm", methods=["GET", "POST"])
def api_research_llm():
    """Get/set LLM adapter config (预留 DeepSeek 等)."""
    if request.method == "GET":
        cfg = llm_adapter.load_config()
        cfg["api_key"] = "***" if cfg.get("api_key") else ""
        cfg["available"] = llm_adapter.is_available()
        return jsonify(cfg)
    data = request.get_json() or {}
    cfg = llm_adapter.save_config(
        enabled=data.get("enabled"),
        provider=data.get("provider"),
        api_key=data.get("api_key") if data.get("api_key") != "***" else None,
        base_url=data.get("base_url"),
        model=data.get("model"),
    )
    return jsonify({"ok": True, "available": llm_adapter.is_available(),
                    "model": cfg.get("model")})


@app.route("/api/account")
def api_account():
    account.init_db()
    acc = account.get_account()
    # refresh positions with realtime prices
    positions = acc["positions"]
    if positions:
        symbols = [p["symbol"] for p in positions]
        quotes = market.fetch_realtime(symbols)
        qmap = {q["symbol"]: q for q in quotes}
        mv_total = 0.0
        pnl_total = 0.0
        for p in positions:
            q = qmap.get(p["symbol"])
            if q:
                p["market_price"] = q["price"]
                p["name"] = q["name"]
            else:
                p["market_price"] = p["avg_cost"]
            p["market_value"] = p["shares"] * p["market_price"]
            p["pnl"] = (p["market_price"] - p["avg_cost"]) * p["shares"]
            p["pnl_pct"] = (p["market_price"] / p["avg_cost"] - 1) * 100 if p["avg_cost"] else 0
            mv_total += p["market_value"]
            pnl_total += p["pnl"]
        acc["market_value"] = round(mv_total, 2)
        acc["total_asset"] = round(acc["cash"] + mv_total, 2)
        acc["total_pnl"] = round(acc["total_asset"] - acc["initial_cash"], 2)
        acc["positions"] = positions
    return jsonify(acc)


@app.route("/api/trade", methods=["POST"])
def api_trade():
    data = request.get_json() or {}
    symbol = str(data.get("symbol", "")).strip()
    side = str(data.get("side", "")).upper()
    price = float(data.get("price", 0))
    shares = float(data.get("shares", 0))
    name = data.get("name", "").strip()
    if not symbol or side not in ("BUY", "SELL") or price <= 0 or shares <= 0:
        return jsonify({"error": "参数错误"}), 400
    if not name:
        q = market.fetch_realtime_one(symbol)
        name = q["name"] if q else symbol
    result = account.place_order(symbol, name, side, price, shares)
    return jsonify(result)


@app.route("/api/orders")
def api_orders():
    return jsonify({"orders": account.get_orders()})


@app.route("/api/account/reset", methods=["POST"])
def api_reset():
    account.reset_account()
    return jsonify({"ok": True, "message": "账户已重置为初始资金"})


# ---- one-click follow invest ---------------------------------------------------------


@app.route("/api/follow", methods=["POST"])
def api_follow():
    """One-click invest in a "strong" stock at market price using a portion of
    the current account's cash. amount=0 uses a default 10% of cash.
    Risk limits: single-stock cap = 20% of total assets; auto-generates a
    buy/sell plan (stop-loss) for the position afterwards."""
    data = request.get_json() or {}
    query = str(data.get("symbol", "")).strip()
    if not query:
        return jsonify({"error": "missing symbol"}), 400

    # resolve code or name
    try:
        resolved = market.resolve_symbol(query)
        symbol = resolved["symbol"]
        name = resolved["name"]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    q = market.fetch_realtime_one(symbol)
    if not q or not q.get("price"):
        return jsonify({"error": f"无法获取 {symbol} 实时价格"}), 500
    price = q["price"]

    acc = account.get_account()
    cash = acc.get("cash", 0)
    total_asset = acc.get("total_asset", cash)

    try:
        amount = float(data.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        amount = cash * 0.10  # default: 10% of available cash

    # --- risk limits ---
    # single-stock cap: 20% of total assets
    single_cap = total_asset * 0.20
    if amount > single_cap:
        return jsonify({"ok": False,
                        "message": f"风控：单票投入上限为总资产 20%（{single_cap:,.0f} 元），"
                                   f"本次 {amount:,.0f} 元超限，请减少金额"})

    # round down to whole lots of 100 within the budget
    shares = int(amount / price / 100) * 100
    if shares <= 0:
        return jsonify({"ok": False,
                        "message": f"资金不足一手：预算 {amount:.0f} 元不够买 100 股 @ {price:.2f}"})

    res = account.place_order(symbol, name, "BUY", price, shares)
    res["symbol"] = symbol
    res["name"] = name
    res["price"] = price
    res["shares"] = shares
    # auto-generate a buy/sell plan (stop-loss) for the followed position
    if res.get("ok"):
        try:
            df = market.fetch_daily(symbol, bars=600)
            fc = kronos_service.forecast(symbol, df, lookback=400, pred_len=120)
            plan = buysell.buy_sell_plan(df, forecast=fc)
            plan["period"] = "day"
            alerts.save_plan(symbol, name, plan)
            res["plan"] = {"stop_loss": plan.get("stop_loss"),
                           "target_up": plan.get("target_up")}
        except Exception:
            pass
    return jsonify(res)


# ---- backtest ------------------------------------------------------------------


@app.route("/api/backtest")
def api_backtest():
    symbol = request.args.get("symbol", "")
    strategy = request.args.get("strategy", "combined")
    cash = float(request.args.get("cash", 100000))
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    try:
        df = market.fetch_daily(symbol, bars=600)
        result = backtest.backtest(df, strategy=strategy, initial_cash=cash)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategies")
def api_strategies():
    return jsonify({"strategies": backtest.STRATEGIES})


# ---- forecast compare ------------------------------------------------------------


@app.route("/api/compare/forecast")
def api_compare_forecast():
    raw = [s for s in request.args.get("symbols", "").split(",") if s]
    period = request.args.get("period", "day")
    pred_len = _int_arg("pred_len", 60, 1, MAX_PRED_LEN)
    if len(raw) < 2:
        return jsonify({"error": "至少需要 2 只股票"}), 400

    # resolve codes or names
    symbols = []
    names0 = {}
    for q in raw:
        try:
            r = market.resolve_symbol(q)
            symbols.append(r["symbol"])
            names0[r["symbol"]] = r["name"]
        except ValueError as e:
            return jsonify({"error": f"无法识别「{q}」: {e}"}), 400

    try:
        results = []
        for sym in symbols:
            df = market.fetch_kline(sym, period=period, bars=600)
            q = market.fetch_realtime_one(sym)
            name = q["name"] if q else names0.get(sym, sym)
            fc = kronos_service.forecast(sym, df, lookback=400, pred_len=pred_len,
                                         T=1.3, top_p=0.95, sample_count=1)
            results.append({
                "symbol": sym,
                "name": name,
                "last_close": fc["last_close"],
                "end_close": fc["end_close"],
                "change_pct": fc["change_pct"],
                "dates": fc["dates"],
                "close": fc["close"],
            })
        return jsonify({"results": results, "period": period, "pred_len": pred_len})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- P2: 预警快照 / 时间轴 / 日报 / 反馈闭环 ---------------------------------

def _bars_payload(symbol, bars=120):
    """取用于快照的 K 线（1 分钟），失败返回 []。"""
    try:
        df = market.fetch_kline(symbol, period="1m", bars=max(240, bars * 2))
        if df is None or len(df) == 0:
            return []
        tail = df.tail(bars)
        return [{"timestamps": str(r["timestamps"])[:16],
                 "open": round(float(r["open"]), 3),
                 "high": round(float(r["high"]), 3),
                 "low": round(float(r["low"]), 3),
                 "close": round(float(r["close"]), 3),
                 "volume": float(r["volume"])}
                for _, r in tail.iterrows()]
    except Exception:
        return []


@app.route("/api/snapshots")
def api_snapshots():
    """预警快照列表（二级及以上自动存）。"""
    symbol = request.args.get("symbol") or None
    day = request.args.get("day") or None
    limit = _int_arg("limit", 50, 1, 500)
    full = request.args.get("full", "0") == "1"
    items = snapshots.list_snapshots(symbol=symbol, day=day, limit=limit,
                                     with_payload=full)
    return jsonify({"snapshots": items, "count": len(items)})


@app.route("/api/snapshots/<int:sid>")
def api_snapshot_get(sid):
    s = snapshots.get_snapshot(sid)
    if not s:
        return jsonify({"error": "快照不存在"}), 404
    return jsonify(s)


@app.route("/api/snapshots/<int:sid>", methods=["DELETE"])
def api_snapshot_delete(sid):
    snapshots.delete_snapshot(sid)
    return jsonify({"ok": True})


@app.route("/api/snapshots/clear", methods=["POST"])
def api_snapshots_clear():
    n = snapshots.clear_snapshots()
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/snapshots/capture", methods=["POST"])
def api_snapshots_capture():
    """手动/自动捕获一条预警快照。

    供前端在全屏预警触发、以及后台 monitor 检测到异动时调用。
    级别 < 2 的请求会被拒绝保存（返回 saved=False）。
    """
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    level = int(data.get("level") or 2)
    if level < 2:
        return jsonify({"saved": False, "reason": "二级以下预警不存快照", "level": level})

    name = data.get("name") or symbol
    try:
        for w in account.get_watchlist():
            if w["symbol"] == symbol:
                name = w.get("name") or name
                break
    except Exception:
        pass

    bars = data.get("bars") or _bars_payload(symbol, _int_arg("window", 120, 20, 400))
    res = snapshots.save_snapshot(
        symbol=symbol, name=name,
        event_type=data.get("event_type") or "manual",
        direction=data.get("direction") or "up",
        level=level,
        price=data.get("price"),
        ts=time.time(),
        reason=data.get("reason") or "",
        score=data.get("score"),
        bars=bars,
        evidence=data.get("evidence"),
        summary=data.get("summary"),
    )
    if res is None:
        return jsonify({"saved": False, "reason": "级别不足或保存失败"})
    return jsonify({**res, "level": level, "bars": len(bars or [])})


@app.route("/api/timeline")
def api_timeline():
    """预警时间轴：某标的某日的全部预警点（按时间升序）。"""
    symbol = request.args.get("symbol", "")
    if not symbol:
        return jsonify({"error": "missing symbol"}), 400
    day = request.args.get("day") or None
    limit = _int_arg("limit", 200, 1, 500)
    return jsonify(snapshots.timeline(symbol, day=day, limit=limit))


@app.route("/api/daily-report")
def api_daily_report():
    """每日预警报告：次数、类型/方向/级别分布、小时分布、走势回溯、反馈汇总。"""
    day = request.args.get("day") or None
    verify = request.args.get("verify", "1") == "1"
    try:
        r = snapshots.daily_report(day=day, with_verify=verify)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify(r)


@app.route("/api/feedback", methods=["POST"])
def api_feedback_add():
    """提交用户反馈：有用 / 无用 / 晚了。"""
    data = request.get_json() or {}
    verdict = (data.get("verdict") or "").strip()
    if not verdict:
        return jsonify({"error": "missing verdict"}), 400
    try:
        res = snapshots.add_feedback(
            snapshot_id=data.get("snapshot_id"),
            verdict=verdict,
            note=data.get("note") or "",
            symbol=data.get("symbol"),
            event_type=data.get("event_type"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **res})


@app.route("/api/feedback")
def api_feedback_list():
    limit = _int_arg("limit", 100, 1, 500)
    return jsonify({"feedback": snapshots.list_feedback(limit=limit)})


@app.route("/api/feedback/stats")
def api_feedback_stats():
    days = _int_arg("days", 30, 1, 365)
    return jsonify(snapshots.feedback_stats(days=days))


# ---- alerts / notify --------------------------------------------------------------


@app.route("/api/notify/config", methods=["GET"])
def api_notify_config_get():
    cfg = notify.load_config()
    cfg["configured"] = notify.is_configured()
    return jsonify(cfg)


@app.route("/api/notify/config", methods=["POST"])
def api_notify_config_set():
    data = request.get_json() or {}
    cfg = notify.update_config(
        channel=data.get("channel"),
        serverchan_key=data.get("serverchan_key"),
        pushplus_token=data.get("pushplus_token"),
        enabled=data.get("enabled"),
    )
    cfg["configured"] = notify.is_configured()
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    result = notify.test_push()
    return jsonify(result)


@app.route("/api/alerts/plans", methods=["GET"])
def api_alerts_plans_get():
    plans = alerts.get_plans()
    return jsonify({"plans": plans})


@app.route("/api/alerts/plans/refresh", methods=["POST"])
def api_alerts_plans_refresh():
    """(Re)generate buy/sell plans for all watchlist symbols."""
    data = request.get_json() or {}
    period = data.get("period", "day")
    account.init_db()
    watch = account.get_watchlist()
    if not watch:
        return jsonify({"ok": False, "message": "自选股为空，请先添加股票"})
    done, failed = [], []
    for w in watch:
        try:
            df = market.fetch_kline(w["symbol"], period=period, bars=600)
            fc = kronos_service.forecast(w["symbol"], df, lookback=400, pred_len=120)
            plan = buysell.buy_sell_plan(df, forecast=fc)
            plan["period"] = period
            alerts.save_plan(w["symbol"], w["name"] or w["symbol"], plan)
            done.append(w["symbol"])
        except Exception as e:
            failed.append({"symbol": w["symbol"], "error": str(e)})
    return jsonify({"ok": True, "done": done, "failed": failed})


@app.route("/api/alerts/plans/<symbol>", methods=["DELETE"])
def api_alerts_plans_delete(symbol):
    alerts.delete_plan(symbol)
    return jsonify({"ok": True})


@app.route("/api/alerts/check", methods=["POST"])
def api_alerts_check():
    """Check all plans against current prices now; fire notifications."""
    data = request.get_json() or {}
    fire = data.get("fire", True)
    plans = alerts.get_plans()
    if not plans:
        return jsonify({"ok": True, "events": [], "message": "暂无买卖点计划"})
    symbols = [p["symbol"] for p in plans]
    quotes = market.fetch_realtime(symbols)
    qmap = {q["symbol"]: q for q in quotes}
    events = []
    for p in plans:
        q = qmap.get(p["symbol"])
        if not q:
            continue
        ev = alerts.check_plan(p["symbol"], p["name"] or p["symbol"], p["plan"], q["price"])
        if ev:
            events.append(ev)
            if fire:
                alerts.fire_notification(ev)
    return jsonify({"ok": True, "events": events})


@app.route("/api/alerts/events")
def api_alerts_events():
    return jsonify({"events": alerts.get_events()})


@app.route("/api/alerts/events/clear", methods=["POST"])
def api_alerts_events_clear():
    alerts.clear_events()
    return jsonify({"ok": True})


# ---- background monitor -----------------------------------------------------------

_MONITOR_RUNNING = False

#: 快照去重：同一标的同一异动时点只存一次
_SNAPSHOT_SEEN = {}


def _capture_momentum_snapshots(name_map: dict, quotes: list,
                                min_score: float = 55.0) -> list:
    """对自选股跑异动分析，达标的存快照（P2）。

    达标条件：依据卡片评分 >= min_score（默认 55，即「中等信号」以上），
    且最新异动方向明确。按 symbol+异动时点去重，避免每轮重复落库。
    """
    saved = []
    for q in quotes:
        sym = q.get("symbol")
        if not sym:
            continue
        try:
            df = market.fetch_kline(sym, period="1m", bars=240)
            if df is None or len(df) < 20:
                continue
            nm = name_map.get(sym) or q.get("name") or sym
            r = anomaly.analyze(sym, df, name=nm, with_backtest=False)
            card = r.get("card") or {}
            evs = r.get("events") or []
            if not evs:
                continue
            score = card.get("score") or 0
            if score < min_score:
                continue
            last = evs[-1]
            key = f"{sym}|{last['ts']}|{last['direction']}"
            if _SNAPSHOT_SEEN.get(key):
                continue
            # 级别：评分 >=75 记三级，>=55 记二级
            level = 3 if score >= 75 else 2

            df2 = market.fetch_kline(sym, period="1m", bars=240)
            bars = []
            if df2 is not None and len(df2):
                t = df2.tail(120)
                bars = [{"timestamps": str(x["timestamps"])[:16],
                         "open": round(float(x["open"]), 3),
                         "high": round(float(x["high"]), 3),
                         "low": round(float(x["low"]), 3),
                         "close": round(float(x["close"]), 3),
                         "volume": float(x["volume"])}
                        for _, x in t.iterrows()]

            res = snapshots.save_snapshot(
                symbol=sym, name=nm,
                event_type=last.get("kind") or "anomaly",
                direction=last.get("direction"),
                level=level, price=last.get("price"),
                ts=time.time(),
                reason=last.get("reason") or "",
                score=score, bars=bars,
                evidence={"factors": card.get("factors"),
                          "risks": card.get("risks"),
                          "degraded": r.get("degraded"),
                          "params": r.get("params")},
                summary=card.get("conclusion"),
            )
            if res:
                _SNAPSHOT_SEEN[key] = time.time()
                saved.append({**res, "symbol": sym, "level": level, "score": score})
                print(f"[monitor] snapshot #{res['id']} {nm} lv{level} score={score}")
        except Exception:
            continue

    # 清理过期的去重键（保留 6 小时）
    cutoff = time.time() - 6 * 3600
    for k in [k for k, v in _SNAPSHOT_SEEN.items() if v < cutoff]:
        _SNAPSHOT_SEEN.pop(k, None)
    return saved


def _monitor_loop():
    """Periodically check alert plans + watchlist momentum. Daemon thread."""
    global _MONITOR_RUNNING
    _MONITOR_RUNNING = True
    while True:
        try:
            time.sleep(60)
            account.init_db()
            watch = account.get_watchlist()
            if not watch:
                continue

            symbols = [w["symbol"] for w in watch]
            name_map = {w["symbol"]: w["name"] or w["symbol"] for w in watch}

            # 3) 自动接管：合并规则里的符号一起拉行情
            try:
                rules = auto_trade.list_rules()
                for r in rules:
                    if r["symbol"] not in name_map:
                        name_map[r["symbol"]] = r["name"] or r["symbol"]
                        symbols.append(r["symbol"])
            except Exception:
                pass

            quotes = market.fetch_realtime(symbols)
            qmap = {q["symbol"]: q for q in quotes}

            # 1) 买卖点计划触发（需要已生成计划）
            plans = alerts.get_plans()
            for p in plans:
                q = qmap.get(p["symbol"])
                if not q:
                    continue
                ev = alerts.check_plan(p["symbol"], p["name"] or p["symbol"],
                                       p["plan"], q["price"])
                if ev:
                    alerts.fire_notification(ev)
                    print(f"[monitor] fired: {ev['message']}")

            # 2) 盘中异动（涨幅/放量），记录并推送
            if notify.is_configured():
                for ev in alerts.check_momentum(name_map, quotes):
                    alerts.fire_notification(ev)
                    print(f"[monitor] momentum: {ev['message']}")

            # 2.5) 买卖点提前预警（接近买点/止损/目标）
            try:
                for ev in alerts.check_near_plans(plans, quotes):
                    if notify.is_configured():
                        alerts.fire_notification(ev)
                    print(f"[monitor] near: {ev['message']}")
            except Exception as ex:
                print(f"[monitor] near_plans error: {ex}")

            # 2.6) 盘中异动快照：二级及以上自动存 K 线+指标（P2）
            try:
                _capture_momentum_snapshots(name_map, quotes)
            except Exception as ex:
                print(f"[monitor] snapshot error: {ex}")

            # 3) 自动接管交易（规则触发 → 自动下单）
            try:
                executed = auto_trade.check_and_execute(qmap, name_map)
                for e in executed:
                    print(f"[auto] {e['symbol']} {e['action']} {e['shares']}股 @ {e['price']} ({e['reason']})")
            except Exception as ex:
                print(f"[monitor] auto_trade error: {ex}")
        except Exception as e:
            print(f"[monitor] error: {e}")


def _nightly_loop():
    """夜间自迭代：每天 02:00 自动跑一次（DeepSeek 根据真实运行数据调参）。"""
    time.sleep(90)  # 等启动稳定
    while True:
        try:
            last = nightly.load_tuning().get("last_run_ts") or 0
            if nightly.should_run_now(last, hour=2):
                print("[nightly] 开始夜间自迭代 ...")
                res = nightly.run_iteration(trigger="schedule")
                cfg = nightly.load_tuning()
                cfg["last_run_ts"] = time.time()
                nightly.save_tuning(cfg)
                print(f"[nightly] 完成：{res['summary']}")
            time.sleep(300)
        except Exception as e:
            print(f"[nightly] error: {e}")
            time.sleep(600)


def _start_monitor():
    import threading
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    t2 = threading.Thread(target=_nightly_loop, daemon=True)
    t2.start()


def _port_in_use(host: str, port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


if __name__ == "__main__":
    import logging
    import threading
    import time
    import webbrowser

    log = logging.getLogger("app")
    PORT = int(os.environ.get("KRONOS_PORT", "7071"))
    HOST = "127.0.0.1"
    URL = f"http://localhost:{PORT}"

    account.init_db()
    alerts.init_tables()
    auto_trade.init_tables()
    nightly.init_tables()
    snapshots.init_tables()

    if _port_in_use(HOST, PORT):
        print("=" * 52)
        print(f"  端口 {PORT} 已被占用 —— 可能服务已经在运行了。")
        print(f"  直接打开浏览器访问：{URL}")
        print("  如果不是本程序占用的，可以改端口后重试：")
        print(f"      set KRONOS_PORT=7072 && python app.py")
        print("=" * 52)
        try:
            webbrowser.open(URL)
        except Exception:
            pass
        sys.exit(0)

    _start_monitor()

    banner = [
        "=" * 52,
        "  Kronos Trading App",
        f"  URL: {URL}",
        f"  DeepSeek: {'已连接 ' + deepseek.status()['model'] if deepseek.available() else '未配置（回退规则引擎）'}",
        f"  日志: {os.path.join(_LOG_DIR, 'app.log')}",
        "=" * 52,
    ]
    for line in banner:
        print(line)
        log.info(line.strip())

    # Open the browser shortly after the server is up.
    # 由 .bat 启动时它自己会开浏览器（设了 KRONOS_NO_BROWSER=1），避免弹出两个标签页。
    if not os.environ.get("KRONOS_NO_BROWSER"):
        threading.Timer(2.0, lambda: webbrowser.open(URL)).start()

    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，服务停止")
    except Exception as e:
        log.exception("服务异常退出: %s", e)
        print(f"\n[错误] 服务异常退出：{e}")
        print(f"详细堆栈见 {os.path.join(_LOG_DIR, 'app.log')}")
        raise SystemExit(1)
    finally:
        log.info("服务已停止")

