"""Kronos Trading App - automated regression test suite.

Runs against a live server (default http://127.0.0.1:7071).
Usage:
    python regression.py [base_url]
Exit code 0 = all passed, 1 = failures.
"""
import json
import sys
import time
import urllib.request
import urllib.parse

# Windows console may default to GBK; force UTF-8 for emoji output
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7071"

PASS = 0
FAIL = 0
FAILURES = []


def req(method, path, body=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"raw": raw[:200]}
            return resp.status, {"text": raw, "content_type": ctype}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return -1, {"error": str(e)}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  ❌ {name} {detail}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- tests

def t1_pages():
    section("1. Pages & status")
    st, body = req("GET", "/")
    check("首页 200", st == 200)
    st, body = req("GET", "/api/status")
    check("状态接口 200", st == 200 and "device" in body)
    check("状态含模型/缓存字段", all(k in body for k in
          ["model_loaded", "device", "cache_files", "cache_size_mb", "watchlist_count"]))


def t2_market():
    section("2. Market data")
    st, body = req("GET", "/api/market/kline?symbol=600585&period=day&bars=250")
    check("日线K线 250根", st == 200 and len(body.get("bars", [])) == 250)
    st, body = req("GET", "/api/market/kline?symbol=600585&period=5m&bars=200")
    check("5分钟K线 200根", st == 200 and len(body.get("bars", [])) == 200)
    st, body = req("GET", "/api/market/kline?symbol=600585&period=1m&bars=200")
    check("1分钟K线", st == 200 and len(body.get("bars", [])) > 0)
    st, body = req("GET", "/api/market/quotes?symbols=600585,002202")
    check("实时报价 2条", st == 200 and len(body.get("quotes", [])) == 2)
    st, body = req("GET", "/api/market/resolve?q=" + urllib.parse.quote("金风科技"))
    check("名称解析 金风科技->002202", st == 200 and body.get("symbol") == "002202")
    st, body = req("GET", "/api/market/resolve?q=600519")
    check("代码解析 600519->贵州茅台", st == 200 and body.get("name") == "贵州茅台")
    st, body = req("GET", "/api/market/resolve?q=" + urllib.parse.quote("不存在的股票XYZ123"))
    check("无效名称返回404", st == 404)


def t3_indicators():
    section("3. Indicators in kline payload")
    st, body = req("GET", "/api/market/kline?symbol=600585&period=day&bars=100")
    bars = body.get("bars", [])
    last = bars[-1] if bars else {}
    check("指标字段存在", all(k in last for k in
          ["ma5", "ma20", "dif", "dea", "macd", "rsi", "kdj_k", "boll_upper"]))
    check("MA5有数值", last.get("ma5") is not None and last["ma5"] > 0)
    check("RSI在0-100", last.get("rsi") is None or 0 <= last["rsi"] <= 100)


def t4_forecast():
    section("4. Kronos forecast")
    st, body = req("GET", "/api/forecast?symbol=600585&period=day&pred_len=30", timeout=120)
    check("预测200", st == 200 and "close" in body)
    check("预测30根", len(body.get("close", [])) == 30)
    check("预测字段完整", all(k in body for k in
          ["last_close", "end_close", "change_pct", "dates", "open", "high", "low"]))
    st, body = req("GET", "/api/forecast/signal?symbol=600585&period=day", timeout=120)
    check("信号接口", st == 200 and "signal" in body)


def t5_buysell():
    section("5. Buy/sell plan")
    st, body = req("GET", "/api/buysell?symbol=600585&period=day", timeout=120)
    check("买卖点200", st == 200)
    check("关键字段", all(k in body for k in
          ["action", "score", "entry_zone", "stop_loss", "target_up", "support_levels", "resistance_levels"]))
    check("止损<现价<目标", body["stop_loss"] < body["last_close"] < body["target_up"])
    check("action合法", body["action"] in ("买入", "卖出", "持有", "观望"))


def t6_accuracy():
    section("6. Accuracy")
    st, body = req("GET", "/api/accuracy?symbol=600585&period=day&pred_len=30&rounds=1", timeout=120)
    check("准确率200", st == 200)
    check("统计字段", all(k in body for k in ["mae", "rmse", "mape_pct", "rounds", "rounds_detail"]))


def t7_compare():
    section("7. Compare")
    st, body = req("GET", "/api/compare?symbols=" + urllib.parse.quote("600585,金风科技") + "&period=day")
    check("对比(代码+名称)", st == 200 and len(body.get("symbols", [])) == 2)
    st, body = req("GET", "/api/compare/forecast?symbols=600585,002202&period=day&pred_len=30", timeout=120)
    check("预测对比", st == 200 and len(body.get("results", [])) == 2)


def t8_trade():
    section("8. Trading")
    # reset first for deterministic state
    req("POST", "/api/account/reset")
    body = {"symbol": "600585", "name": "海螺水泥", "side": "BUY", "price": 17.41, "shares": 1000}
    st, r = req("POST", "/api/trade", body)
    check("买入成功", st == 200 and r.get("ok"))
    body = {"symbol": "600585", "name": "海螺水泥", "side": "SELL", "price": 17.50, "shares": 400}
    st, r = req("POST", "/api/trade", body)
    check("卖出成功", st == 200 and r.get("ok"))
    st, acc = req("GET", "/api/account")
    check("持仓600股", st == 200 and len(acc.get("positions", [])) == 1
          and abs(acc["positions"][0]["shares"] - 600) < 1e-6)
    # insufficient funds
    body = {"symbol": "600519", "name": "贵州茅台", "side": "BUY", "price": 1300, "shares": 1000}
    st, r = req("POST", "/api/trade", body)
    check("资金不足被拒", st == 200 and not r.get("ok"))
    # oversell rejected
    body = {"symbol": "600585", "side": "SELL", "price": 17.0, "shares": 99999}
    st, r = req("POST", "/api/trade", body)
    check("超量卖出被拒", st == 200 and not r.get("ok"))
    st, body = req("GET", "/api/orders")
    check("委托记录2条", st == 200 and len(body.get("orders", [])) == 2)
    # cleanup
    req("POST", "/api/account/reset")


def t9_backtest():
    section("9. Backtest")
    st, body = req("GET", "/api/backtest?symbol=600585&strategy=combined")
    check("回测200", st == 200)
    check("回测字段", all(k in body for k in
          ["total_return_pct", "max_drawdown_pct", "win_rate_pct", "trades", "equity", "benchmark"]))
    st, body = req("GET", "/api/strategies")
    check("策略列表", st == 200 and len(body.get("strategies", {})) == 4)


def t10_alerts():
    section("10. Alerts & notify")
    st, body = req("GET", "/api/alerts/plans")
    check("计划列表", st == 200)
    st, body = req("POST", "/api/alerts/plans/refresh", {"period": "day"}, timeout=120)
    check("生成计划", st == 200 and body.get("ok"))
    st, body = req("GET", "/api/alerts/events")
    check("事件列表", st == 200)
    st, body = req("GET", "/api/notify/config")
    check("通知配置", st == 200 and "channel" in body)
    req("POST", "/api/alerts/events/clear")


def t11_report():
    section("11. Report export")
    st, body = req("GET", "/api/report?symbol=600585&period=day&format=csv", timeout=120)
    csv_ok = st == 200 and "text/csv" in body.get("content_type", "") and "date,open,high" in body.get("text", "")
    check("CSV报告200", csv_ok)
    st, body = req("GET", "/api/report?symbol=600585&period=day&format=json", timeout=120)
    check("JSON报告200", st == 200 and "forecast" in body and "buysell" in body)


def t12_errors():
    section("12. Error handling")
    st, body = req("GET", "/api/market/kline?symbol=999999")
    check("无效代码K线错误", st == 500 or st == 200)  # may return error or empty
    st, body = req("GET", "/api/market/kline")
    check("缺symbol参数400", st == 400)
    st, body = req("GET", "/api/forecast")
    check("预测缺参400", st == 400)
    st, body = req("POST", "/api/trade", {})
    check("交易缺参400", st == 400)
    st, body = req("GET", "/api/compare?symbols=600585")
    check("对比缺股400", st == 400)
    st, body = req("GET", "/api/backtest")
    check("回测缺参400", st == 400)


def t13_watchlist_crud():
    section("13. Watchlist CRUD")
    # add by Chinese name
    st, r = req("POST", "/api/watchlist", {"symbol": "比亚迪"}, timeout=30)
    check("中文名添加", st == 200 and r.get("symbol") == "002594")
    # duplicate
    st, r = req("POST", "/api/watchlist", {"symbol": "比亚迪"}, timeout=30)
    check("重复添加提示", st == 200 and "已" in r.get("message", ""))
    # delete
    st, r = req("DELETE", "/api/watchlist/002594")
    check("删除", st == 200 and r.get("ok"))
    # delete non-existent
    st, r = req("DELETE", "/api/watchlist/002594")
    check("删除不存在的也OK", st == 200 and r.get("ok"))


def main():
    print(f"Kronos Trading App Regression Suite -> {BASE}")
    print(f"Started at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    t1_pages(); t2_market(); t3_indicators(); t4_forecast(); t5_buysell()
    t6_accuracy(); t7_compare(); t8_trade(); t9_backtest()
    t10_alerts(); t11_report(); t12_errors(); t13_watchlist_crud()

    print(f"\n{'='*50}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
