"""Agent 引擎：规则引擎生成各角色观点（可插拔 LLM）。

无 LLM key 时使用规则引擎：每个角色按其 focus 从真实数据推导「观点/论据/风险点/置信度/假设」。
若配置了 LLM（见 llm.py），则把同一份结构化数据交给 LLM 生成自然语言观点。

规则引擎的设计目标：可复现、可解释、不产生幻觉（所有论据都附带数据来源）。
"""
import time

import analytics
import fundamentals
import indicators as ind
import kronos_service
import market
from agents.roles import ROLES, Role, get_role


# ---------------------------------------------------------------------------
# 数据上下文（9 个 Agent 共享的输入，只抓一次）
# ---------------------------------------------------------------------------

def build_context(symbols: list, with_forecast: bool = True) -> dict:
    """Gather all data the agents need. symbols: list of 6-digit codes."""
    ctx = {"symbols": symbols, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "quotes": {}, "kline_meta": {}, "forecasts": {}, "buysell": {},
           "valuation": {}, "fundamentals": {}, "errors": []}

    for sym in symbols:
        try:
            q = market.fetch_realtime_one(sym)
            if q:
                ctx["quotes"][sym] = q
        except Exception as e:
            ctx["errors"].append(f"quote {sym}: {e}")

        try:
            df = market.fetch_daily(sym, bars=600)
            dfe = ind.all_indicators(df)
            last = dfe.iloc[-1]
            closes = df["close"]
            ctx["kline_meta"][sym] = {
                "last_close": float(closes.iloc[-1]),
                "ret_20d_pct": round(float(closes.iloc[-1] / closes.iloc[-21] - 1) * 100, 2) if len(closes) > 21 else None,
                "ret_60d_pct": round(float(closes.iloc[-1] / closes.iloc[-61] - 1) * 100, 2) if len(closes) > 61 else None,
                "ma20": round(float(last["ma20"]), 2),
                "ma60": round(float(last["ma60"]), 2),
                "rsi": round(float(last["rsi"]), 1),
                "macd": round(float(last["macd"]), 3),
                "dif": round(float(last["dif"]), 3),
                "dea": round(float(last["dea"]), 3),
                "kdj_k": round(float(last["kdj_k"]), 1),
                "kdj_d": round(float(last["kdj_d"]), 1),
                "boll_upper": round(float(last["boll_upper"]), 2),
                "boll_lower": round(float(last["boll_lower"]), 2),
                "vol_annual_pct": round(float(closes.pct_change().std() * (252 ** 0.5) * 100), 2),
            }
        except Exception as e:
            ctx["errors"].append(f"kline {sym}: {e}")

        if with_forecast:
            try:
                df = market.fetch_daily(sym, bars=600)
                ctx["forecasts"][sym] = kronos_service.forecast(sym, df, lookback=400, pred_len=30)
            except Exception as e:
                ctx["errors"].append(f"forecast {sym}: {e}")

        try:
            import buysell
            df = market.fetch_daily(sym, bars=600)
            fc = ctx["forecasts"].get(sym)
            ctx["buysell"][sym] = buysell.buy_sell_plan(df, forecast=fc)
        except Exception as e:
            ctx["errors"].append(f"buysell {sym}: {e}")

        try:
            ctx["fundamentals"][sym] = fundamentals.analyze(sym)
        except Exception as e:
            ctx["errors"].append(f"fundamentals {sym}: {e}")

    return ctx


# ---------------------------------------------------------------------------
# 规则引擎：按角色生成观点
# ---------------------------------------------------------------------------

def _view(sym, ctx):
    """Return (view_text, evidence[], risks[], confidence, assumptions[])."""
    q = ctx["quotes"].get(sym, {})
    km = ctx["kline_meta"].get(sym, {})
    fc = ctx["forecasts"].get(sym, {})
    bs = ctx["buysell"].get(sym, {})
    fu = ctx["fundamentals"].get(sym, {})
    name = q.get("name") or fu.get("company", {}).get("name") or sym
    return q, km, fc, bs, fu, name


def analyze_as(role: Role, symbols: list, ctx: dict) -> dict:
    """Run one role over the symbols; returns its structured opinion."""
    rid = role.id
    per_symbol = []
    evidence_all, risks_all, assumptions_all = [], [], []
    stance_score = 0.0   # >0 偏多, <0 偏空

    for sym in symbols:
        q, km, fc, bs, fu, name = _view(sym, ctx)
        ev, rk, asm = [], [], []
        contribution = 0.0

        if rid == "cio":
            ev.append(f"标的池 {len(symbols)} 只，任务：投研分析与调仓建议")
            asm.append("会议结论基于各专家独立观点汇总，存在信息时滞")

        elif rid == "pm":
            ret20 = km.get("ret_20d_pct")
            if ret20 is not None:
                ev.append(f"{name} 近20日收益 {ret20:+.1f}%")
                contribution += max(-2, min(2, ret20 / 10))
            asm.append("等权组合假设，未考虑税费与冲击成本")

        elif rid == "researcher":
            rel = (fu.get("valuation") or {})
            if rel.get("pe_ttm"):
                ev.append(f"{name} PE(TTM) {rel['pe_ttm']:.1f}，PB {rel.get('pb') or 0:.2f}")
            if fc:
                ev.append(f"Kronos 30日预测 {fc.get('change_pct'):+.1f}%（区间 ±{((fc.get('confidence') or {}).get('mae_pct',0.15))*100:.0f}%）")
                contribution += max(-2, min(2, fc.get("change_pct", 0) / 5))
            asm.append("估值与预测均基于公开数据，未含未公开信息")

        elif rid == "risk":
            vol = km.get("vol_annual_pct")
            if vol:
                ev.append(f"{name} 年化波动 {vol:.1f}%")
                if vol > 45:
                    rk.append(f"高波动（{vol:.1f}%），需控制仓位")
                    contribution -= 1.0
            ret60 = km.get("ret_60d_pct")
            if ret60 is not None and ret60 < -15:
                rk.append(f"近60日下跌 {ret60:.1f}%，处于弱势")
                contribution -= 1.0
            if not rk:
                ev.append(f"{name} 未触发内置风险阈值")
            asm.append("风险扫描基于量化阈值，不含突发事件与未公开信息")

        elif rid == "compliance":
            ev.append(f"审查 {name} 相关表述：禁止保本/稳赚；须标注假设与置信度")
            asm.append("合规判断基于内置规则库，非法律意见")

        elif rid == "trader":
            turnover = q.get("turnover")
            if turnover is not None:
                ev.append(f"{name} 换手率 {turnover:.2f}%")
                if turnover < 0.5:
                    rk.append("换手偏低，大额建仓可能产生滑点")
            entry = (bs.get("entry_zone") or {})
            if entry.get("low"):
                ev.append(f"技术买点区间 {entry.get('low')}~{entry.get('high')}")
            asm.append("滑点按历史波动估算，实际成交受盘口影响")

        elif rid == "industry":
            fu_c = fu.get("company") or {}
            if fu_c.get("industry"):
                ev.append(f"{name} 所属行业：{fu_c['industry']}")
            ev.append("行业景气度需结合板块数据（当前按个股财务代理）")
            asm.append("缺少实时行业数据源时以公司财务作代理判断")

        elif rid == "macro":
            try:
                import global_market
                g = global_market.get_global_markets()
                up = sum(1 for m in g if (m.get("pct") or 0) > 0)
                ev.append(f"外围 {len(g)} 个指数中 {up} 个上涨")
                if g:
                    avg = sum((m.get("pct") or 0) for m in g) / len(g)
                    ev.append(f"外围均值 {avg:+.2f}%")
                    contribution += max(-1, min(1, avg))
            except Exception:
                ev.append("外围数据暂不可用")
            asm.append("宏观判断基于外围指数代理，未含利率/通胀实时数据")

        elif rid == "quant":
            dif, dea, macd = km.get("dif"), km.get("dea"), km.get("macd")
            if dif is not None:
                if dif > dea and macd > 0:
                    ev.append(f"{name} MACD 多头（DIF {dif} > DEA {dea}）")
                    contribution += 0.6
                elif dif < dea:
                    ev.append(f"{name} MACD 空头（DIF {dif} < DEA {dea}）")
                    contribution -= 0.6
            rsi = km.get("rsi")
            if rsi is not None:
                ev.append(f"{name} RSI {rsi}")
                if rsi > 75:
                    rk.append(f"RSI {rsi} 超买，短线回调概率上升")
                    contribution -= 0.4
                elif rsi < 30:
                    rk.append(f"RSI {rsi} 超卖，留意反弹")
                    contribution += 0.4
            # 预测偏差校验（与 20 日实际动量对比）
            ret20 = km.get("ret_20d_pct")
            if fc and ret20 is not None:
                diff = fc.get("change_pct", 0) - ret20
                if abs(diff) > 10:
                    rk.append(f"预测方向与近期动量偏离 {diff:+.1f}%，需警惕模型偏差")
            asm.append("量化校验基于指标与历史统计，不保证未来有效")

        per_symbol.append({
            "symbol": sym, "name": name,
            "evidence": ev, "risks": rk,
            "contribution": round(contribution, 2),
        })
        evidence_all.extend([f"[{name}] {e}" for e in ev])
        risks_all.extend([f"[{name}] {r}" for r in rk])
        stance_score += contribution          # 累加每只标的的立场贡献
        assumptions_all.extend(asm)

    # 立场与置信度
    if stance_score > 0.8:
        view = "偏多"
    elif stance_score < -0.8:
        view = "偏空"
    else:
        view = "中性"

    conf = min(0.85, 0.45 + 0.1 * len(evidence_all)) if evidence_all else 0.3

    return {
        "role_id": role.id,
        "role_name": role.name,
        "title": role.title,
        "view": view,
        "stance_score": round(stance_score, 2),
        "evidence": evidence_all,
        "risks": risks_all,
        "confidence": round(conf, 2),
        "assumptions": sorted(set(assumptions_all)),
        "per_symbol": per_symbol,
        "engine": "rule",
    }
