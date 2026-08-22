"""Forecast accuracy evaluation: rolling out-of-sample validation of Kronos.

For each validation window we use the first `lookback` bars to predict the next
`pred_len` bars, then compare against the actual bars that follow. Aggregates
MAE/RMSE/MAPE on close price and direction accuracy.
"""
import time

import numpy as np
import pandas as pd

import kronos_service


def _errors(pred_close: list, actual_close: list) -> dict:
    pred = np.asarray(pred_close, dtype=float)
    act = np.asarray(actual_close, dtype=float)
    n = min(len(pred), len(act))
    if n == 0:
        return {"mae": None, "rmse": None, "mape": None, "dir_acc": None, "n": 0}
    pred, act = pred[:n], act[:n]
    diff = pred - act
    mae = float(np.mean(np.abs(diff)))
    rmse = float(np.sqrt(np.mean(diff ** 2)))
    scale = np.where(np.abs(act) > 1e-9, np.abs(act), 1.0)
    mape = float(np.mean(np.abs(diff) / scale) * 100)
    # direction accuracy: sign of (pred_end - start) vs (actual_end - start)
    if n >= 2:
        pred_dir = np.sign(pred[-1] - pred[0])
        act_dir = np.sign(act[-1] - act[0])
        dir_acc = float(pred_dir == act_dir) * 100
    else:
        dir_acc = None
    return {"mae": mae, "rmse": rmse, "mape": mape, "dir_acc": dir_acc, "n": n}


def evaluate(symbol: str, period: str = "day", lookback: int = 400, pred_len: int = 60,
             rounds: int = 3, T: float = 1.0, top_p: float = 0.9, verbose: bool = True) -> dict:
    """Rolling evaluation. Requires at least lookback + pred_len * (rounds+1) bars.

    Returns per-round results plus aggregated stats.
    """
    import market

    df = market.fetch_kline(symbol, period=period, bars=lookback + pred_len * (rounds + 1) + 20)
    total = len(df)
    required = lookback + pred_len * (rounds + 1)
    if total < required:
        # adjust rounds to what the data allows
        rounds = max(1, (total - lookback) // pred_len - 1)
        if rounds < 1:
            raise ValueError(
                f"数据不足：{period} 周期仅有 {total} 根，至少需要 {lookback + pred_len * 2} 根"
            )

    results = []
    t_start = time.time()
    for r in range(rounds):
        # window: [start, start+lookback) is input; [start+lookback, +pred_len) is target
        start = total - lookback - pred_len * (r + 1)
        window = df.iloc[start:start + lookback].reset_index(drop=True)
        actual = df.iloc[start + lookback:start + lookback + pred_len].reset_index(drop=True)

        fc = kronos_service.forecast(
            symbol, window, lookback=lookback, pred_len=pred_len,
            T=T, top_p=top_p, sample_count=1, use_cache=False,
        )
        err = _errors(fc["close"], actual["close"].tolist())
        err.update({
            "round": r + 1,
            "start_date": str(window["timestamps"].iloc[-1].date()),
            "end_date": str(actual["timestamps"].iloc[-1].date()),
            "last_close": fc["last_close"],
            "pred_end": fc["end_close"],
            "actual_end": round(float(actual["close"].iloc[-1]), 3),
        })
        results.append(err)
        if verbose:
            print(f"  round {r+1}: start={err['start_date']} pred={err['pred_end']} "
                  f"actual={err['actual_end']} mae={err['mae']} dir_acc={err['dir_acc']}")

    valid = [r for r in results if r["mae"] is not None]
    agg = {
        "symbol": symbol,
        "period": period,
        "lookback": lookback,
        "pred_len": pred_len,
        "rounds": len(results),
        "mae": round(float(np.mean([r["mae"] for r in valid])), 4) if valid else None,
        "rmse": round(float(np.mean([r["rmse"] for r in valid])), 4) if valid else None,
        "mape_pct": round(float(np.mean([r["mape"] for r in valid])), 3) if valid else None,
        "dir_acc_pct": round(float(np.mean([r["dir_acc"] for r in valid])), 2)
        if valid and all(r["dir_acc"] is not None for r in valid) else None,
        "avg_pred_change_pct": round(float(np.mean(
            [(r["pred_end"] / r["last_close"] - 1) * 100 for r in valid])), 2) if valid else None,
        "avg_actual_change_pct": round(float(np.mean(
            [(r["actual_end"] / r["last_close"] - 1) * 100 for r in valid])), 2) if valid else None,
        "elapsed": round(time.time() - t_start, 1),
    }
    agg["rounds_detail"] = results
    return agg
