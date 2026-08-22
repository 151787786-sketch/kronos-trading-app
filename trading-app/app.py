"""Kronos Trading App - Flask backend.

A local web app that combines real A-share market data, technical indicators,
Kronos AI forecasts, trading signals, a simulated trading account, and strategy
backtesting.

Run:  python app.py   (or double-click 启动交易APP.bat)
"""
import os
import sys

import pandas as pd
from flask import Flask, jsonify, render_template, request

import account
import accuracy
import alerts
import auto_trade
import backtest
import buysell
import fundamentals
import global_market
import indicators as ind
import kronos_service
import market
import notify
import recommend
from signals import forecast_direction_signals, indicator_signals

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # reject bodies > 1 MB

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


# ---- forecast -----------------------------------------------------------------


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

            # 3) 自动接管交易（规则触发 → 自动下单）
            try:
                executed = auto_trade.check_and_execute(qmap, name_map)
                for e in executed:
                    print(f"[auto] {e['symbol']} {e['action']} {e['shares']}股 @ {e['price']} ({e['reason']})")
            except Exception as ex:
                print(f"[monitor] auto_trade error: {ex}")
        except Exception as e:
            print(f"[monitor] error: {e}")


def _start_monitor():
    import threading
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    import threading
    import time
    import webbrowser

    account.init_db()
    alerts.init_tables()
    auto_trade.init_tables()
    _start_monitor()
    print("=" * 52)
    print("  Kronos Trading App")
    print("  URL: http://localhost:7071")
    print("=" * 52)

    # Open the browser shortly after the server is up.
    threading.Timer(2.0, lambda: webbrowser.open("http://localhost:7071")).start()

    app.run(host="127.0.0.1", port=7071, debug=False, use_reloader=False)

