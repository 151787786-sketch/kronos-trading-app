"""高级算法预判 V3.1：动量衰减检测 · 盘口异动监测 · ML 概率分

需求清单 V3.0 第八节（V3.1 可选）。

三个能力
--------
1. **动量衰减检测**：用价格加速度（二阶差分）识别趋势拐点，
   在拐点确认前给出预警。核心思想：速度在衰减 → 趋势将尽。
2. **盘口异动监测**：`Level-2` 挂单量突变检测。**本机数据源不提供盘口数据**，
   因此该部分显式返回 degraded 状态并说明需要什么数据，绝不编造盘口数值。
   同时提供一个**可用的替代**：用 1 分钟量价的「准盘口代理」指标
   （量能突增 + 振幅压缩 = 疑似挂单堆积）并明确标注为代理。
3. **ML 概率分**：用历史数据训练一个轻量逻辑回归，输出异动后方向延续的
   概率（0~1）。**特征只用本机真实可得的数据**；样本不足时明确拒绝训练，
   不返回一个没有统计意义的分数。

诚实声明
--------
* ML 模型在样本不足（<  MIN_TRAIN_SAMPLES）时**拒绝训练并说明原因**，
  而不是硬拟合一个过拟合的分数。
* 输出必带 `sample_size` / `train_acc` / `baseline_rate`，让用户判断这个
  概率分是否可信 —— 一个 60% 的概率分在基线率 55% 时几乎无信息量。
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from anomaly import (_ensure_df, _is_closing_auction, _is_halted,
                     SRC_BACKTEST, SRC_HEURISTIC, SRC_DEGRADED, _provenance)

#: ML 训练所需最小样本数（低于此值拒绝训练）
MIN_TRAIN_SAMPLES = 40


# ---------------------------------------------------------------------------
# 1. 动量衰减检测
# ---------------------------------------------------------------------------

def momentum_params():
    return [
        _provenance("mom_window", 5, "根", SRC_HEURISTIC,
                    "计算价格速度/加速度的滚动窗口。5 根 1 分钟 K 线约覆盖 5 分钟，"
                    "与 A 股日内资金推动的典型节奏匹配。"),
        _provenance("mom_decay_ratio", 0.5, "", SRC_BACKTEST,
                    "动量衰减判定阈值：当前速度绝对值 < 前段峰值速度的 50% 视为衰减。"
                    "衰减到一半时趋势往往已接近尾声。"),
        _provenance("mom_accel_thresh", 0.0, "", SRC_HEURISTIC,
                    "加速度转负（与速度反向）即为拐点信号。"),
    ]


def momentum_analysis(df: pd.DataFrame, window: int = 5,
                      decay_ratio: float = 0.5) -> dict:
    """动量衰减检测：输出速度、加速度、是否衰减、拐点预警。"""
    if df is None or len(df) < window * 4:
        return {"ok": False,
                "error": f"数据不足（需至少 {window*4} 根 K 线以估计速度与加速度）"}

    d = _ensure_df(df)
    d["_valid"] = ~(d["timestamps"].apply(_is_closing_auction)
                    | d.apply(_is_halted, axis=1))
    dv = d[d["_valid"]].reset_index(drop=True)
    if len(dv) < window * 4:
        return {"ok": False, "error": "有效 K 线不足（停牌/竞价 bar 过多）"}

    close = dv["close"].astype(float)

    # 速度 = 一阶差分 / 窗口；加速度 = 速度的一阶差分
    velocity = close.diff(window) / window
    accel = velocity.diff(window) / window

    v_series = velocity.dropna()
    a_series = accel.dropna()
    if len(v_series) < 3:
        return {"ok": False, "error": "有效速度序列太短"}

    v_now = float(v_series.iloc[-1])
    a_now = float(a_series.iloc[-1]) if len(a_series) else 0.0

    # 衰减判定：峰值速度要取**足够长的回看窗口**，否则「刚涨完就横住」时
    # 近端峰值也是 0，会得出「没衰减」的错误结论。
    peak_lookback = min(len(v_series), window * 12)
    recent_v = v_series.tail(peak_lookback)
    if v_now >= 0:
        peak_v = float(recent_v.max())
        direction = "up" if peak_v > 0 else "flat"
    else:
        peak_v = float(recent_v.min())
        direction = "down"
    peak_abs = abs(peak_v)
    v_abs = abs(v_now)
    v_scale = float(recent_v.abs().max()) or 1.0

    # 「几乎不动」判定：**用更长的回看窗口**判断是否真的没有行情。
    # 只看最近 3N 根会把「刚涨完进入横盘」误判成「本来就横盘」——
    # 而那恰恰是最该提示「动能耗尽」的场景。
    lookback = min(len(dv), window * 12)
    span_recent = float(dv["high"].tail(window * 3).max() - dv["low"].tail(window * 3).min())
    span_long = float(dv["high"].tail(lookback).max() - dv["low"].tail(lookback).min())
    last_close = float(close.iloc[-1]) or 1.0
    flat_recent = (span_recent / last_close) < 0.0015
    moved_before = (span_long / last_close) >= 0.004     # 更长窗口内有明显行情
    flat = flat_recent and not moved_before

    if flat:
        return {
            "ok": True,
            "window": window,
            "velocity": round(v_now, 5),
            "acceleration": round(a_now, 5),
            "peak_velocity_abs": round(peak_abs, 5),
            "decay_pct": None,
            "decayed": False,
            "turning": False,
            "flat": True,
            "stalled": False,
            "direction": "flat",
            "strength": 0.0,
            "warning": {
                "type": "flat",
                "direction": "flat",
                "label": "价格横盘，无明确动量",
                "why": (f"近 {window*3} 根区间仅 {span_recent:.3f}"
                        f"（{span_recent/last_close*100:.2f}%），"
                        f"且更长窗口（{lookback} 根）内亦无显著行情 —— "
                        f"不存在可衰减的动量，不构成趋势拐点信号"),
            },
            "params": momentum_params(),
            "bars_used": int(len(dv)),
            "note": "速度=收盘价的 N 根一阶差分/N；加速度=速度的一阶差分/N。"
                    "仅基于价格序列，不含成交量信息。",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 衰减判定
    decayed = peak_abs > 1e-12 and v_abs < peak_abs * decay_ratio
    # 拐点：速度与加速度反向（涨势中加速度转负，跌势中加速度转正）
    turning = (v_now > 0 and a_now < 0) or (v_now < 0 and a_now > 0)

    decay_pct = round((v_abs / peak_abs * 100) if peak_abs > 0 else 0.0, 1)
    # 速度为 0（完全停滞）时，措辞要与「衰减到某个百分比」区分开
    stalled = v_abs < 1e-9

    warn = None
    if decayed and turning:
        warn = {
            "type": "reversal",
            "direction": "down" if v_now > 0 else "up",
            "label": ("上涨动能衰竭，注意回落" if v_now > 0 else "下跌动能衰竭，可能止跌"),
            "why": (f"速度已{'停滞' if stalled else f'衰减至峰值的 {decay_pct:.0f}%'}"
                    f"（峰值 {peak_abs:.4f}），且加速度 {a_now:+.4f} 与速度方向相反"),
        }
    elif decayed:
        warn = {
            "type": "decay",
            "direction": "down" if v_now > 0 else "up",
            "label": ("动量停滞（拐点未确认）" if stalled else "动量衰减中（拐点未确认）"),
            "why": (f"速度已{'降至 0' if stalled else f'衰减至峰值的 {decay_pct:.0f}%'}"
                    f"（峰值 {peak_abs:.4f}），"
                    + ("但窗口内价格曾明显移动，属推升动能耗尽后的停顿，"
                       "加速度尚未反向，方向待确认"
                       if stalled else "但加速度尚未反向，趋势可能延续")),
        }
    elif turning:
        warn = {
            "type": "turning",
            "direction": "down" if v_now > 0 else "up",
            "label": "加速度反向（早期预警）",
            "why": f"加速度 {a_now:+.4f} 已与速度方向相反，动能正在转换",
        }

    # 归一化强度（0~1）
    strength = round(min(1.0, v_abs / v_scale), 3)

    return {
        "ok": True,
        "window": window,
        "velocity": round(v_now, 5),
        "acceleration": round(a_now, 5),
        "peak_velocity_abs": round(peak_abs, 5),
        "decay_pct": decay_pct,
        "decayed": bool(decayed),
        "turning": bool(turning),
        "stalled": bool(stalled),
        "flat": False,
        "direction": direction,
        "strength": strength,
        "warning": warn,
        "params": momentum_params(),
        "bars_used": int(len(dv)),
        "note": "速度=收盘价的 N 根一阶差分/N；加速度=速度的一阶差分/N。"
                "仅基于价格序列，不含成交量信息。",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 2. 盘口异动监测
# ---------------------------------------------------------------------------

def orderbook_capability() -> dict:
    """明确声明盘口监测所需数据与当前可用性。"""
    return {
        "ok": True,
        "available": False,
        "reason": "本机数据源（腾讯行情 1 分钟 K 线）不提供盘口挂单快照与逐笔委托，"
                  "无法计算真实挂单量突变、撤单率、买卖盘口失衡。",
        "requires": [
            "Level-2 十档/千档挂单快照（含时间戳）",
            "逐笔委托明细（用于计算撤单率）",
            "逐笔成交（用于主动买卖方向标记）",
        ],
        "degraded_factors": [
            _provenance("book_bid_ask_imbalance", None, "", SRC_DEGRADED,
                        "买卖盘口失衡度需要十档挂单量，当前不可得。"),
            _provenance("book_cancel_rate", None, "", SRC_DEGRADED,
                        "撤单率需要逐笔委托数据，当前不可得。"),
            _provenance("book_order_surge", None, "", SRC_DEGRADED,
                        "挂单量突变检测需要盘口快照序列，当前不可得。"),
        ],
        "proxy_available": True,
        "proxy_note": "作为替代，可用 1 分钟量价的**准盘口代理**指标推测挂单堆积迹象"
                      "（见 /api/advanced/orderbook 的 proxy 字段），已明确标注为代理。",
    }


def orderbook_proxy(df: pd.DataFrame, window: int = 5) -> dict:
    """盘口代理指标 —— **不是**真实盘口数据，仅用量价特征推测。

    逻辑：若「量能显著放大」但「价格振幅被压缩」，往往是买卖双方在某个价位
    大量挂单对峙（一方护盘 / 一方派发）的表现，真实盘口里会直接看到挂单堆积。
    """
    if df is None or len(df) < window * 4:
        return {"ok": False, "error": "数据不足"}

    d = _ensure_df(df)
    d["_valid"] = ~(d["timestamps"].apply(_is_closing_auction)
                    | d.apply(_is_halted, axis=1))
    dv = d[d["_valid"]].reset_index(drop=True)
    if len(dv) < window * 4:
        return {"ok": False, "error": "有效 K 线不足"}

    recent = dv.tail(window)
    pre = dv.iloc[:-window]
    vol_base = float(pre["volume"].median()) if len(pre) >= 5 else float(dv["volume"].median())
    vol_ratio = float(recent["volume"].mean() / vol_base) if vol_base > 0 else 0.0

    amp_recent = float(((recent["high"] - recent["low"]) / recent["close"].replace(0, np.nan)).mean() * 100)
    amp_pre = float(((pre["high"] - pre["low"]) / pre["close"].replace(0, np.nan)).mean() * 100) if len(pre) >= 5 else amp_recent
    amp_compress = (amp_recent < amp_pre * 0.8) if amp_pre > 0 else False

    # 代理判定：放量 + 振幅压缩 => 疑似挂单对峙
    suspected = vol_ratio >= 1.8 and amp_compress

    return {
        "ok": True,
        "proxy": True,
        "disclaimer": "⚠️ 本结果为**代理指标**，由 1 分钟量价特征推测，"
                      "**不是**真实盘口数据。真实挂单量/撤单率需 Level-2。",
        "suspected_stacking": bool(suspected),
        "volume_ratio": round(vol_ratio, 2),
        "amp_recent_pct": round(amp_recent, 3),
        "amp_prev_pct": round(amp_pre, 3),
        "interpretation": (
            "量能放大但振幅被压缩，疑似有资金在同一价位大量挂单对峙（真实盘口可确认）"
            if suspected else
            "未观察到「放量+振幅压缩」的组合特征，无挂单堆积迹象"
        ),
        "bars_used": int(len(dv)),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 3. ML 概率分
# ---------------------------------------------------------------------------

FEATURE_NAMES = [
    "amp_pct",          # 当前根振幅 %
    "vol_ratio",        # 相对前段中位量的倍数
    "body_ratio",       # |close-open| / (high-low)，实体占比
    "up_streak",        # 连续同向根数
    "range_pos",        # 收盘在近 N 根高低区间中的位置 0~1
    "vol_trend",        # 近 3 根量能 / 前 3 根量能
]


def _build_features(d: pd.DataFrame) -> pd.DataFrame:
    """构造特征矩阵（全部来自真实 1 分钟 K 线）。"""
    df = d.copy()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    vol = df["volume"].astype(float)

    prev_close = close.shift(1)
    df["amp_pct"] = ((high - low) / prev_close.replace(0, np.nan) * 100).fillna(0.0)
    vol_base = vol.rolling(20, min_periods=5).median()
    df["vol_ratio"] = (vol / vol_base.replace(0, np.nan)).fillna(1.0).clip(0, 20)

    rng = (high - low).replace(0, np.nan)
    df["body_ratio"] = ((close - open_).abs() / rng).fillna(0.0).clip(0, 1)

    direction = (close >= open_).astype(int)
    streak = direction.groupby((direction != direction.shift()).cumsum()).cumcount() + 1
    df["up_streak"] = np.where(direction == 1, streak, -streak)

    roll_hi = high.rolling(20, min_periods=5).max()
    roll_lo = low.rolling(20, min_periods=5).min()
    span = (roll_hi - roll_lo).replace(0, np.nan)
    df["range_pos"] = ((close - roll_lo) / span).fillna(0.5).clip(0, 1)

    v3 = vol.rolling(3, min_periods=1).mean()
    v3p = vol.shift(3).rolling(3, min_periods=1).mean()
    df["vol_trend"] = (v3 / v3p.replace(0, np.nan)).fillna(1.0).clip(0, 20)

    return df


def train_model(df: pd.DataFrame, horizon: int = 30,
                min_samples: int = MIN_TRAIN_SAMPLES) -> dict:
    """用真实历史 K 线训练轻量逻辑回归，预测「异动后方向延续」概率。

    样本不足时**拒绝训练**，返回 insufficient 说明 —— 不返回无意义的分数。
    """
    if df is None or len(df) < min_samples + horizon + 25:
        return {
            "ok": False, "insufficient": True,
            "error": f"历史数据不足（需 ≥{min_samples + horizon + 25} 根，"
                     f"当前 {0 if df is None else len(df)} 根）—— 拒绝训练，"
                     f"避免给出没有统计意义的概率分",
            "min_samples": min_samples,
        }

    d = _ensure_df(df)
    d["_valid"] = ~(d["timestamps"].apply(_is_closing_auction)
                    | d.apply(_is_halted, axis=1))
    dv = d[d["_valid"]].reset_index(drop=True)
    dv = _build_features(dv)

    # 标签：从每根起 horizon 根后的涨跌方向
    fwd = dv["close"].shift(-horizon) / dv["close"] - 1
    dv["_fwd"] = fwd
    dv["_label"] = (dv["_fwd"] > 0).astype(int)

    # 只在「有异动特征」的样本上训练（与本系统关注的对象一致）
    valid = dv.dropna(subset=FEATURE_NAMES + ["_fwd"]).copy()
    # 特征有区分度的样本：振幅或量比明显
    train_pool = valid[(valid["amp_pct"] > 0.3) | (valid["vol_ratio"] > 1.5)]
    if len(train_pool) < min_samples:
        train_pool = valid
    if len(train_pool) < min_samples:
        return {
            "ok": False, "insufficient": True,
            "error": f"可用于训练的样本仅 {len(train_pool)} 条（需 ≥{min_samples}）"
                     f"—— 拒绝训练",
            "min_samples": min_samples,
        }

    X = train_pool[FEATURE_NAMES].to_numpy(dtype=float)
    y = train_pool["_label"].to_numpy(dtype=float)

    # 标准化
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    Xs = (X - mu) / sd
    Xs = np.hstack([np.ones((len(Xs), 1)), Xs])

    # 逻辑回归（梯度下降，带 L2）
    w = np.zeros(Xs.shape[1])
    lr, l2, epochs = 0.1, 0.01, 600
    for _ in range(epochs):
        z = Xs @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        grad = Xs.T @ (p - y) / len(y) + l2 * w
        w -= lr * grad

    # 训练集准确率 + 基线率（多数类占比）—— 两者必须一起看
    p_final = 1.0 / (1.0 + np.exp(-np.clip(Xs @ w, -30, 30)))
    pred = (p_final >= 0.5).astype(int)
    train_acc = float((pred == y).mean())
    baseline = float(max(y.mean(), 1 - y.mean()))

    coefs = {name: round(float(c), 4) for name, c in zip(FEATURE_NAMES, w[1:])}

    # 样本外：用最后 20% 做时间序列切分（不做随机打乱，避免未来信息泄漏）
    split = int(len(Xs) * 0.8)
    oos_acc = None
    if split >= 20 and len(Xs) - split >= 8:
        Xtr, ytr, Xte, yte = Xs[:split], y[:split], Xs[split:], y[split:]
        w2 = np.zeros(Xtr.shape[1])
        for _ in range(epochs):
            z = Xtr @ w2
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            grad = Xtr.T @ (p - ytr) / len(ytr) + l2 * w2
            w2 -= lr * grad
        p_te = 1.0 / (1.0 + np.exp(-np.clip(Xte @ w2, -30, 30)))
        oos_acc = float(((p_te >= 0.5).astype(int) == yte).mean())

    # 重要性：|系数| 归一
    imp_abs = np.abs(w[1:])
    imp_sum = imp_abs.sum() or 1.0
    importance = sorted(
        [{"feature": n, "coef": coefs[n], "importance": round(float(a / imp_sum), 3)}
         for n, a in zip(FEATURE_NAMES, imp_abs)],
        key=lambda x: -x["importance"])

    # 是否比基线有信息量
    informative = train_acc > baseline + 0.02

    return {
        "ok": True,
        "samples": int(len(Xs)),
        "horizon_min": horizon,
        "train_acc": round(train_acc, 4),
        "oos_acc": round(oos_acc, 4) if oos_acc is not None else None,
        "baseline_rate": round(baseline, 4),
        "informative": bool(informative),
        "coefficients": coefs,
        "importance": importance,
        "mu": [round(float(v), 6) for v in mu],
        "sd": [round(float(v), 6) for v in sd],
        "features": FEATURE_NAMES,
        "weights": [round(float(v), 6) for v in w],
        "caveat": ("训练准确率高于基线，模型有一定信息量"
                   if informative else
                   "⚠️ 训练准确率未显著高于基线率（多数类占比），"
                   "该模型的概率分信息量有限，请谨慎参考"),
        "note": f"逻辑回归，{len(Xs)} 样本，L2 正则，时间序列 8:2 切分（不打乱）。"
                f"特征全部来自 1 分钟 OHLCV，不含 Level-2 数据。",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def ml_predict(model: dict, df: pd.DataFrame) -> dict:
    """用已训练的模型对最新一根 K 线打分（0~1）。"""
    if not model or not model.get("ok"):
        return {"ok": False, "error": "模型不可用（未训练或样本不足）"}
    if df is None or len(df) < 25:
        return {"ok": False, "error": "数据不足，无法打分"}

    d = _ensure_df(df)
    d["_valid"] = ~(d["timestamps"].apply(_is_closing_auction)
                    | d.apply(_is_halted, axis=1))
    dv = d[d["_valid"]].reset_index(drop=True)
    dv = _build_features(dv)
    row = dv.dropna(subset=FEATURE_NAMES).tail(1)
    if len(row) == 0:
        return {"ok": False, "error": "有效特征不足"}

    x = row[FEATURE_NAMES].to_numpy(dtype=float)[0]
    mu = np.array(model["mu"], dtype=float)
    sd = np.array(model["sd"], dtype=float)
    w = np.array(model["weights"], dtype=float)
    xs = np.concatenate([[1.0], (x - mu) / sd])
    z = float(xs @ w)
    prob = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    # 与基线对比，给出「是否比瞎猜好」的判断
    base = model.get("baseline_rate") or 0.5
    edge = prob - base

    return {
        "ok": True,
        "prob_up": round(float(prob), 4),
        "prob_down": round(1 - float(prob), 4),
        "baseline_rate": base,
        "edge": round(float(edge), 4),
        "signal": ("偏多" if prob > 0.55 else ("偏空" if prob < 0.45 else "中性")),
        "confidence_note": (
            f"模型训练准确率 {model.get('train_acc')}，基线率 {base}。"
            + ("该分数比基线有信息量。" if model.get("informative")
               else "⚠️ 模型未显著优于基线，该分数参考价值有限。")
        ),
        "features_now": {n: round(float(v), 4) for n, v in zip(FEATURE_NAMES, x)},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
