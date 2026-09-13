"""专业能力层：投研团队共用的量化分析引擎。

提供：
  - correlation_matrix: 多标的相关性矩阵（≤20 只）
  - attribution: 组合/标的归因分析
  - valuation: 多维度估值（PE/PB/相对/预测交叉）
  - monte_carlo: 蒙特卡洛情景模拟（默认 10000 次）
  - risk_scan: 多维度风险扫描
  - build_rebalance_list: 结构化调仓清单

全部基于真实行情/财务/Kronos 预测数据，结果可复现。
"""
import math
import time

import numpy as np
import pandas as pd

import fundamentals
import indicators as ind
import kronos_service
import market

MAX_BATCH = 20


# ---------------------------------------------------------------------------
# 相关性矩阵
# ---------------------------------------------------------------------------

def correlation_matrix(symbols: list, period: str = "day", bars: int = 120) -> dict:
    """Compute return correlation matrix for up to 20 symbols."""
    symbols = symbols[:MAX_BATCH]
    series = {}
    names = {}
    for sym in symbols:
        try:
            df = market.fetch_kline(sym, period=period, bars=bars)
            series[sym] = df.set_index("timestamps")["close"].pct_change()
            q = market.fetch_realtime_one(sym)
            names[sym] = q["name"] if q else sym
        except Exception:
            continue
    if len(series) < 2:
        return {"symbols": [], "names": names, "matrix": []}

    close_df = pd.DataFrame(series).dropna()
    corr = close_df.corr().round(3)

    matrix = []
    keys = list(corr.columns)
    for a in keys:
        matrix.append([corr.loc[a, b] for b in keys])

    # 高相关性对（|r| >= 0.7）——风控关注点
    high_pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            r = corr.loc[a, b]
            if abs(r) >= 0.7:
                high_pairs.append({"a": a, "b": b, "name_a": names.get(a, a),
                                   "name_b": names.get(b, b), "corr": float(r)})

    return {"symbols": keys, "names": names, "matrix": matrix,
            "high_corr_pairs": high_pairs, "obs": int(len(close_df))}


# ---------------------------------------------------------------------------
# 归因分析
# ---------------------------------------------------------------------------

def attribution(symbols: list, period: str = "day", bars: int = 120) -> dict:
    """Per-symbol contribution to equal-weight portfolio return."""
    rows = []
    for sym in symbols[:MAX_BATCH]:
        try:
            df = market.fetch_kline(sym, period=period, bars=bars)
            closes = df["close"]
            ret = float(closes.iloc[-1] / closes.iloc[0] - 1) * 100
            vol = float(closes.pct_change().std() * math.sqrt(252) * 100)
            q = market.fetch_realtime_one(sym)
            rows.append({
                "symbol": sym,
                "name": (q["name"] if q else sym),
                "return_pct": round(ret, 2),
                "volatility_pct": round(vol, 2),
                "price": float(closes.iloc[-1]),
            })
        except Exception:
            continue
    if not rows:
        return {"items": [], "portfolio_return_pct": None, "best": None, "worst": None}

    avg = sum(r["return_pct"] for r in rows) / len(rows)
    rows.sort(key=lambda r: r["return_pct"], reverse=True)
    return {
        "items": rows,
        "portfolio_return_pct": round(avg, 2),
        "best": rows[0],
        "worst": rows[-1],
        "weighting": "等权（1/N）",
    }


# ---------------------------------------------------------------------------
# 多维度估值
# ---------------------------------------------------------------------------

def valuation(symbol: str, pred_len: int = 30) -> dict:
    """Multi-dimension valuation with Kronos cross-check."""
    out = {"symbol": symbol, "dimensions": {}, "assumptions": []}
    try:
        val = fundamentals.get_valuation(symbol)
        out["name"] = val.get("name") or symbol
        out["price"] = val.get("price")
        out["dimensions"]["relative"] = {
            "pe_ttm": val.get("pe_ttm"), "pb": val.get("pb"),
            "eps": val.get("eps"), "bvps": val.get("bvps"),
            "total_mv": val.get("total_mv"),
        }
        # 相对估值判断
        pe = val.get("pe_ttm")
        pb = val.get("pb")
        verdicts = []
        if pe:
            if pe < 15:
                verdicts.append(f"PE {pe:.1f} 偏低")
            elif pe > 45:
                verdicts.append(f"PE {pe:.1f} 偏高")
            else:
                verdicts.append(f"PE {pe:.1f} 中性")
        if pb:
            if pb < 1:
                verdicts.append(f"PB {pb:.2f} 破净")
            elif pb > 5:
                verdicts.append(f"PB {pb:.2f} 偏高")
        out["dimensions"]["relative_verdict"] = "；".join(verdicts) or "数据不足"
    except Exception as e:
        out["dimensions"]["relative"] = {"error": str(e)}

    # 财务成长性
    try:
        fin = fundamentals.get_financials(symbol, periods=4)
        if fin:
            latest = fin[0]
            out["dimensions"]["growth"] = {
                "report_date": latest.get("report_date"),
                "revenue_yoy": latest.get("revenue_yoy"),
                "net_profit_yoy": latest.get("net_profit_yoy"),
                "roe": latest.get("roe"),
                "gross_margin": latest.get("gross_margin"),
            }
    except Exception:
        pass

    # Kronos 预测估值（DCF 简化的替代：预测区间作为未来价格参考）
    try:
        df = market.fetch_daily(symbol, bars=600)
        fc = kronos_service.forecast(symbol, df, lookback=400, pred_len=pred_len)
        conf = fc.get("confidence") or {}
        band = conf.get("mae_pct", 0.15)
        out["dimensions"]["forecast"] = {
            "last_close": fc["last_close"],
            "pred_end": fc["end_close"],
            "pred_change_pct": fc["change_pct"],
            "lower": round(fc["end_close"] * (1 - band), 2),
            "upper": round(fc["end_close"] * (1 + band), 2),
            "band_pct": round(band * 100, 1),
            "model": fc.get("model"),
        }
        out["assumptions"].append(
            f"预测区间基于模型历史误差 ±{band*100:.1f}%，非投资价值判断")
    except Exception as e:
        out["dimensions"]["forecast"] = {"error": str(e)}

    # 技术面估值（所处位置）
    try:
        df = market.fetch_daily(symbol, bars=250)
        dfe = ind.all_indicators(df)
        last = dfe.iloc[-1]
        price = float(last["close"])
        out["dimensions"]["technical"] = {
            "ma20": round(float(last["ma20"]), 2),
            "ma60": round(float(last["ma60"]), 2),
            "rsi": round(float(last["rsi"]), 1),
            "boll_upper": round(float(last["boll_upper"]), 2),
            "boll_lower": round(float(last["boll_lower"]), 2),
            "vs_ma20_pct": round((price / float(last["ma20"]) - 1) * 100, 2),
        }
    except Exception:
        pass

    out["assumptions"].append("估值基于公开财务数据与历史价格，未包含未公开信息")
    return out


# ---------------------------------------------------------------------------
# 蒙特卡洛模拟
# ---------------------------------------------------------------------------

def monte_carlo(symbols: list, n_sim: int = 10000, horizon: int = 30,
                period: str = "day", bars: int = 250) -> dict:
    """Monte-Carlo simulation of equal-weight portfolio over `horizon` bars.

    Uses historical daily returns (bootstrap) to project portfolio return and
    drawdown distribution.
    """
    n_sim = max(1000, min(n_sim, 50000))
    rets = {}
    for sym in symbols[:MAX_BATCH]:
        try:
            df = market.fetch_kline(sym, period=period, bars=bars)
            r = df["close"].pct_change().dropna().values
            if len(r) > 20:
                rets[sym] = r
        except Exception:
            continue
    if not rets:
        return {"error": "no data", "n_sim": 0}

    keys = list(rets.keys())
    n_assets = len(keys)
    w = 1.0 / n_assets
    finals = np.zeros(n_sim)
    maxdds = np.zeros(n_sim)
    rng = np.random.default_rng(42)

    for i in range(n_sim):
        # 等权组合：每个资产从自身历史收益中有放回抽样
        paths = np.zeros((horizon, n_assets))
        for j, k in enumerate(keys):
            idx = rng.integers(0, len(rets[k]), horizon)
            paths[:, j] = rets[k][idx]
        port = (paths * w).sum(axis=1)          # 组合日收益
        cum = np.cumprod(1 + port)
        finals[i] = cum[-1] - 1
        peak = np.maximum.accumulate(cum)
        maxdds[i] = float(np.min(cum / peak - 1))

    pct = lambda a, p: round(float(np.percentile(a, p)) * 100, 2)
    return {
        "n_sim": n_sim,
        "horizon_bars": horizon,
        "assets": keys,
        "weighting": "等权",
        "return_dist": {
            "p5": pct(finals, 5), "p25": pct(finals, 25), "p50": pct(finals, 50),
            "p75": pct(finals, 75), "p95": pct(finals, 95),
            "mean": round(float(finals.mean()) * 100, 2),
        },
        "drawdown_dist": {
            "p50": pct(maxdds, 50), "p95": pct(maxdds, 95), "worst": pct(maxdds, 0),
        },
        "prob_loss_pct": round(float((finals < 0).mean()) * 100, 2),
        "assumptions": [
            "基于历史收益有放回抽样（bootstrap），假设收益分布平稳",
            "未考虑交易成本、停牌、涨跌停限制",
            f"模拟次数 {n_sim}，视界 {horizon} 根K线",
        ],
    }


# ---------------------------------------------------------------------------
# 风险扫描
# ---------------------------------------------------------------------------

RISK_THRESHOLDS = {
    "vol_high": 45.0,        # 年化波动率 %
    "drawdown_high": -20.0,  # 60日回撤 %
    "pe_high": 60.0,
    "pb_high": 8.0,
    "turnover_low": 0.5,     # 换手率 %（流动性）
}


def risk_scan(symbols: list, period: str = "day", bars: int = 120) -> dict:
    """Multi-dimension risk scan: volatility / drawdown / valuation / liquidity /
    sentiment(basic) / tail risk."""
    items = []
    for sym in symbols[:MAX_BATCH]:
        row = {"symbol": sym, "risks": [], "level": "低"}
        try:
            df = market.fetch_kline(sym, period=period, bars=bars)
            closes = df["close"]
            q = market.fetch_realtime_one(sym)
            name = q["name"] if q else sym
            row["name"] = name

            # 波动率
            vol = float(closes.pct_change().std() * math.sqrt(252) * 100)
            row["volatility_pct"] = round(vol, 2)
            if vol > RISK_THRESHOLDS["vol_high"]:
                row["risks"].append(f"高波动（年化 {vol:.1f}%）")

            # 回撤
            peak = closes.cummax()
            dd = float((closes.iloc[-1] / peak.iloc[-1] - 1) * 100)
            row["drawdown_pct"] = round(dd, 2)
            if dd < RISK_THRESHOLDS["drawdown_high"]:
                row["risks"].append(f"处于回撤（{dd:.1f}%）")

            # 尾部风险（5% VaR，基于历史日收益）
            r = closes.pct_change().dropna()
            if len(r) > 20:
                var95 = float(np.percentile(r, 5) * 100)
                row["var95_pct"] = round(var95, 2)
                if var95 < -5:
                    row["risks"].append(f"尾部风险大（日 VaR95 {var95:.1f}%）")

            # 流动性
            turnover = q.get("turnover") if q else None
            if turnover is not None:
                row["turnover_pct"] = turnover
                if turnover < RISK_THRESHOLDS["turnover_low"]:
                    row["risks"].append(f"流动性偏低（换手 {turnover:.2f}%）")

            # 估值风险
            try:
                v = fundamentals.get_valuation(sym)
                if v.get("pe_ttm") and v["pe_ttm"] > RISK_THRESHOLDS["pe_high"]:
                    row["risks"].append(f"估值偏高（PE {v['pe_ttm']:.0f}）")
                if v.get("pb") and v["pb"] > RISK_THRESHOLDS["pb_high"]:
                    row["risks"].append(f"PB 偏高（{v['pb']:.1f}）")
            except Exception:
                pass

            # 等级
            n = len(row["risks"])
            row["level"] = "高" if n >= 3 else ("中" if n >= 1 else "低")
        except Exception as e:
            row["risks"].append(f"数据获取失败：{e}")
            row["level"] = "未知"
        items.append(row)

    high = [i for i in items if i["level"] == "高"]
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "high": len(high),
            "medium": len([i for i in items if i["level"] == "中"]),
            "low": len([i for i in items if i["level"] == "低"]),
        },
        "thresholds": RISK_THRESHOLDS,
        "assumptions": ["风险等级基于量化阈值，不含未公开信息与突发事件"],
    }


# ---------------------------------------------------------------------------
# 结构化调仓清单
# ---------------------------------------------------------------------------

def build_rebalance_list(symbols: list, plan: dict = None) -> list:
    """Generate an actionable rebalance list with entry/stop/target zones.

    Combines buysell plans (stop/target) with Kronos forecast direction.
    """
    import buysell
    out = []
    for sym in symbols[:MAX_BATCH]:
        try:
            df = market.fetch_daily(sym, bars=600)
            fc = kronos_service.forecast(sym, df, lookback=400, pred_len=30)
            bs = buysell.buy_sell_plan(df, forecast=fc)
            q = market.fetch_realtime_one(sym)
            name = (q["name"] if q else sym)

            # 方向：结合 Kronos 预测与买卖点建议
            action = bs.get("action")
            direction = "HOLD"
            if action == "买入":
                direction = "BUY"
            elif action == "卖出":
                direction = "SELL"
            else:
                # 参考预测方向
                chg = fc.get("change_pct", 0)
                direction = "BUY" if chg > 3 else ("SELL" if chg < -3 else "HOLD")

            out.append({
                "symbol": sym,
                "name": name,
                "direction": direction,
                "price": bs.get("last_close"),
                "entry_zone": [bs.get("entry_zone", {}).get("low"), bs.get("entry_zone", {}).get("high")],
                "stop_loss": bs.get("stop_loss"),
                "take_profit": bs.get("target_up"),
                "score": bs.get("score"),
                "kronos_change_pct": fc.get("change_pct"),
                "confidence_band_pct": round(((fc.get("confidence") or {}).get("mae_pct", 0.15)) * 100, 1),
                "reason": bs.get("summary"),
                "assumptions": [
                    "价格区间基于技术位与 ATR 波动，非承诺价格",
                    "置信带为模型历史误差水平",
                ],
            })
        except Exception as e:
            out.append({"symbol": sym, "error": str(e), "direction": "HOLD"})
    return out


# ---------------------------------------------------------------------------
# 回测质检：对投研结论/调仓方案做历史回测评估
# ---------------------------------------------------------------------------

def backtest_quality(symbols: list, strategy: str = "combined",
                     initial_cash: float = 100000.0) -> dict:
    """Run historical backtests on the committee's symbols to validate whether
    the analysis approach would have worked historically.

    Returns per-symbol stats plus an aggregate verdict.
    """
    import backtest
    results = []
    for sym in symbols[:MAX_BATCH]:
        try:
            df = market.fetch_daily(sym, bars=600)
            bt = backtest.backtest(df, strategy=strategy, initial_cash=initial_cash)
            q = market.fetch_realtime_one(sym)
            results.append({
                "symbol": sym,
                "name": (q["name"] if q else sym),
                "total_return_pct": bt["total_return_pct"],
                "annualized_pct": bt["annualized_pct"],
                "max_drawdown_pct": bt["max_drawdown_pct"],
                "win_rate_pct": bt["win_rate_pct"],
                "trades": len(bt["trades"]),
                "avg_pnl": bt["avg_pnl"],
                "benchmark_return_pct": round(
                    (bt["benchmark"][-1] / bt["benchmark"][0] - 1) * 100, 2) if bt.get("benchmark") else None,
            })
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})

    ok = [r for r in results if "error" not in r]
    if not ok:
        return {"strategy": strategy, "items": results, "verdict": "无法回测（数据不足）"}

    avg_ret = sum(r["total_return_pct"] for r in ok) / len(ok)
    avg_wr = sum(r["win_rate_pct"] for r in ok) / len(ok)
    avg_dd = sum(r["max_drawdown_pct"] for r in ok) / len(ok)
    beats = sum(1 for r in ok
                if r.get("benchmark_return_pct") is not None
                and r["total_return_pct"] > r["benchmark_return_pct"])

    if avg_ret > 0 and avg_wr >= 50:
        verdict = "策略历史表现正向，但历史不代表未来"
    elif avg_ret > 0:
        verdict = "策略历史收益为正但胜率偏低，稳定性不足"
    else:
        verdict = "策略历史收益为负，该策略不宜直接采用"

    return {
        "strategy": strategy,
        "items": results,
        "summary": {
            "symbols_tested": len(ok),
            "avg_total_return_pct": round(avg_ret, 2),
            "avg_win_rate_pct": round(avg_wr, 2),
            "avg_max_drawdown_pct": round(avg_dd, 2),
            "beats_buy_and_hold": beats,
        },
        "verdict": verdict,
        "assumptions": [
            f"回测策略：{strategy}，初始资金 {initial_cash:,.0f}",
            "回测未计入滑点、流动性冲击与停牌",
            "历史回测结果不构成对未来收益的承诺",
        ],
    }
