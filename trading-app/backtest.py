"""Strategy backtest engine: run a rule-based strategy over historical data."""
import numpy as np
import pandas as pd

from indicators import all_indicators
from signals import indicator_signals

STRATEGIES = {
    "ma_cross": "均线金叉死叉（MA5/MA20）",
    "macd_cross": "MACD 金叉死叉",
    "rsi_reverse": "RSI 超买超卖反转",
    "combined": "综合信号（全部指标投票）",
}


def _strategy_signals(df, strategy):
    sig = indicator_signals(df)
    if strategy == "ma_cross":
        keep = sig["reason"].str.contains("MA", na=False)
        sig.loc[~keep] = ("HOLD", "", 0.0)
    elif strategy == "macd_cross":
        keep = sig["reason"].str.contains("MACD", na=False)
        sig.loc[~keep] = ("HOLD", "", 0.0)
    elif strategy == "rsi_reverse":
        keep = sig["reason"].str.contains("RSI", na=False)
        sig.loc[~keep] = ("HOLD", "", 0.0)
    return sig


def backtest(df: pd.DataFrame, strategy: str = "combined", initial_cash: float = 100000.0,
             commission: float = 0.0003, stamp_tax: float = 0.0005) -> dict:
    """Run strategy over df (>= ~60 bars). Returns equity curve + stats."""
    if len(df) < 60:
        raise ValueError("数据不足，回测至少需要 60 根 K 线")

    df = all_indicators(df).reset_index(drop=True)
    sig = _strategy_signals(df, strategy)

    cash = initial_cash
    shares = 0.0
    entry_price = 0.0
    equity = []
    trades = []
    open_trade = None

    for i in range(len(df)):
        price = df["close"].iloc[i]
        signal = sig["signal"].iloc[i]

        # SELL
        if signal == "SELL" and shares > 0:
            amount = price * shares
            fee = max(amount * commission, 5.0) + amount * stamp_tax
            cash += amount - fee
            pnl = (price - entry_price) * shares - fee - open_trade["fee"]
            trades.append({
                "entry_date": str(df["timestamps"].iloc[open_trade["i"]].date()),
                "exit_date": str(df["timestamps"].iloc[i].date()),
                "entry": round(entry_price, 3), "exit": round(price, 3),
                "pnl": round(pnl, 2),
                "pnl_pct": round((price / entry_price - 1) * 100, 2),
                "reason": str(sig["reason"].iloc[i]),
            })
            open_trade = None
            shares = 0.0

        # BUY
        elif signal == "BUY" and shares == 0:
            fee = max(price * commission, 5.0)
            affordable = (cash - fee) / price
            shares = int(affordable // 100) * 100  # A股整手（100股）
            if shares > 0:
                amount = price * shares
                fee = max(amount * commission, 5.0)
                cash -= amount + fee
                entry_price = price
                open_trade = {"i": i, "fee": fee}

        equity.append(cash + shares * price)

    # close any open trade at last price
    if shares > 0 and open_trade:
        price = df["close"].iloc[-1]
        amount = price * shares
        fee = max(amount * commission, 5.0) + amount * stamp_tax
        cash += amount - fee
        pnl = (price - entry_price) * shares - fee - open_trade["fee"]
        trades.append({
            "entry_date": str(df["timestamps"].iloc[open_trade["i"]].date()),
            "exit_date": str(df["timestamps"].iloc[-1].date()),
            "entry": round(entry_price, 3), "exit": round(price, 3),
            "pnl": round(pnl, 2),
            "pnl_pct": round((price / entry_price - 1) * 100, 2),
            "reason": "期末平仓",
        })

    eq = pd.Series(equity)
    final = eq.iloc[-1]
    total_ret = (final / initial_cash - 1) * 100
    # max drawdown
    peak = eq.cummax()
    dd = (eq / peak - 1) * 100
    max_dd = float(dd.min())

    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    avg_pnl = np.mean([t["pnl"] for t in trades]) if trades else 0.0

    # annualized (approx using number of bars as days)
    n = len(df)
    ann = (final / initial_cash) ** (252 / max(n, 1)) - 1

    return {
        "strategy": strategy,
        "strategy_name": STRATEGIES.get(strategy, strategy),
        "initial_cash": initial_cash,
        "final_asset": round(float(final), 2),
        "total_return_pct": round(float(total_ret), 2),
        "annualized_pct": round(float(ann) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": trades,
        "win_rate_pct": round(float(win_rate), 2),
        "avg_pnl": round(float(avg_pnl), 2),
        "dates": [str(d.date()) for d in df["timestamps"]],
        "equity": [round(float(v), 2) for v in eq],
        "benchmark": [round(float(v / df["close"].iloc[0] * initial_cash), 2) for v in df["close"]],
    }
