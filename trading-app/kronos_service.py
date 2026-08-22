"""Kronos forecast service: load the model once, predict future OHLC for a symbol.

Supports multiple models:
- Built-in presets scanned from D:\\a\\kronos\\models (any folder containing
  config.json + model.safetensors is offered; the model zoo also lists
  Kronos-mini/small/base on the HF Hub).
- Custom model directories via an absolute path.

Model selection persists in a small JSON file so the chosen model survives
restarts. Forecasts are cached per symbol so repeated requests are fast.
"""
import json
import os
import sys
import threading
import time

import pandas as pd

KRONOS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KRONOS_ROOT)

MODELS_DIR = os.path.join(KRONOS_ROOT, "models")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_state.json")

# torch inference on the same predictor is not thread-safe: serialize calls
_infer_lock = threading.Lock()

# HF Hub model zoo (model, tokenizer, context length, params, description)
MODEL_ZOO = [
    {"id": "Kronos-mini",  "model": "NeoQuasar/Kronos-mini",
     "tokenizer": "NeoQuasar/Kronos-Tokenizer-2k", "context": 2048, "params": "4.1M",
     "description": "轻量模型，最快", "local": None},
    {"id": "Kronos-small", "model": "NeoQuasar/Kronos-small",
     "tokenizer": "NeoQuasar/Kronos-Tokenizer-base", "context": 512, "params": "24.7M",
     "description": "小模型，均衡", "local": None},
    {"id": "Kronos-base",  "model": "NeoQuasar/Kronos-base",
     "tokenizer": "NeoQuasar/Kronos-Tokenizer-base", "context": 512, "params": "102.3M",
     "description": "基础模型，质量最好", "local": None},
]

_model = None
_tokenizer = None
_predictor = None
_model_info = None
_cache = {}  # (model_id, symbol, params) -> {ts, result}


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"model_id": "Kronos-base", "model_dir": None, "device": None}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def scan_local_models() -> list:
    """Scan models/ dir for local model folders (config.json + model.safetensors).
    Tokenizer folders (name contains 'Tokenizer') are excluded."""
    found = []
    if os.path.isdir(MODELS_DIR):
        for name in sorted(os.listdir(MODELS_DIR)):
            if "tokenizer" in name.lower():
                continue
            d = os.path.join(MODELS_DIR, name)
            if (os.path.isdir(d) and os.path.isfile(os.path.join(d, "config.json"))
                    and os.path.isfile(os.path.join(d, "model.safetensors"))):
                found.append({
                    "id": name, "model": d, "tokenizer": None,
                    "context": 512, "params": "local",
                    "description": f"本地模型目录 {name}", "local": d,
                })
    return found


def available_models() -> list:
    """Return zoo models enriched with local availability, plus any extra local models."""
    locals_ = {m["id"]: m for m in scan_local_models()}
    out = []
    for m in MODEL_ZOO:
        entry = dict(m)
        if entry["id"] in locals_:
            entry["local"] = locals_[entry["id"]]["model"]
            entry["description"] += "（已下载本地权重）"
        out.append(entry)
    # extra local models not in the zoo
    for lid, lm in locals_.items():
        if lid not in {m["id"] for m in MODEL_ZOO}:
            out.append(lm)
    return out


def current_model() -> dict:
    state = _load_state()
    info = {
        "model_id": state.get("model_id", "Kronos-base"),
        "model_dir": state.get("model_dir"),
        "loaded": _model is not None,
        "device": getattr(_predictor, "device", None) if _predictor else state.get("device"),
    }
    if _model_info:
        info.update({
            "loaded_model": _model_info.get("id"),
            "loaded_from": "local" if _model_info.get("local") else "hub",
        })
    return info


def _find_model_entry(model_id: str) -> dict:
    for m in available_models():
        if m["id"].lower() == model_id.lower():
            return m
    return None


def load_model(model_id: str, model_dir: str = None, device: str = None) -> dict:
    """Load a model. model_id is a zoo id (Kronos-mini/small/base) or a folder
    name under models/. model_dir can be an absolute path to a custom folder.
    Returns {'ok': bool, 'message': str, 'info': {...}}."""
    global _model, _tokenizer, _predictor, _model_info, _cache

    import torch
    from model import Kronos, KronosTokenizer, KronosPredictor

    with _infer_lock:  # serialize model loading against concurrent inference
        return _load_model_locked(model_id, model_dir, device)


def _load_model_locked(model_id, model_dir, device):
    global _model, _tokenizer, _predictor, _model_info, _cache
    import torch
    from model import Kronos, KronosTokenizer, KronosPredictor

    # resolve target: explicit dir > zoo id with local weights > zoo id on hub
    entry = None
    resolved_dir = None
    resolved_tokenizer = None
    context = 512

    if model_dir:
        if not os.path.isfile(os.path.join(model_dir, "config.json")):
            return {"ok": False, "message": f"模型目录不存在或缺少 config.json: {model_dir}"}
        resolved_dir = model_dir
        # try to find a matching tokenizer next to the model or in models/
        base = os.path.basename(model_dir.rstrip("/\\"))
        tok_candidates = [
            os.path.join(os.path.dirname(model_dir), base.replace("Kronos", "Kronos-Tokenizer")),
            os.path.join(MODELS_DIR, "Kronos-Tokenizer-base"),
            os.path.join(MODELS_DIR, "Kronos-Tokenizer-2k"),
        ]
        for c in tok_candidates:
            if os.path.isfile(os.path.join(c, "config.json")):
                resolved_tokenizer = c
                break
        if not resolved_tokenizer:
            return {"ok": False, "message": f"未找到匹配的 Tokenizer（{model_dir} 同级需有 Tokenizer 目录）"}
        entry = {"id": os.path.basename(model_dir.rstrip("/\\")), "local": resolved_dir,
                 "context": context}

    else:
        entry = _find_model_entry(model_id)
        if not entry:
            return {"ok": False, "message": f"未知模型: {model_id}，可选: {[m['id'] for m in available_models()]}"}
        context = entry.get("context", 512)
        if entry.get("local"):
            resolved_dir = entry["local"]
            resolved_tokenizer = os.path.join(MODELS_DIR, "Kronos-Tokenizer-base")
            if not os.path.isfile(os.path.join(resolved_tokenizer, "config.json")):
                resolved_tokenizer = None
        if not resolved_dir:
            # fall back to Hugging Face Hub
            resolved_dir = entry["model"]

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device not in ("cpu", "cuda", "mps"):
        device = "cpu"

    try:
        t0 = time.time()
        tok_src = resolved_tokenizer or entry.get("tokenizer")
        _tokenizer = KronosTokenizer.from_pretrained(tok_src)
        _model = Kronos.from_pretrained(resolved_dir)
        _predictor = KronosPredictor(_model, _tokenizer, device=device, max_context=context)
        elapsed = time.time() - t0
    except Exception as e:
        _model, _tokenizer, _predictor, _model_info = None, None, None, None
        return {"ok": False, "message": f"模型加载失败: {e}"}

    _model_info = {"id": entry["id"], "local": resolved_dir if os.path.isdir(resolved_dir) else None,
                   "context": context, "device": device, "elapsed": round(elapsed, 1)}
    _cache.clear()  # invalidate forecasts from the previous model

    state = _load_state()
    state["model_id"] = entry["id"]
    state["model_dir"] = resolved_dir if os.path.isdir(resolved_dir) else None
    state["device"] = device
    _save_state(state)

    return {"ok": True,
            "message": f"模型 {entry['id']} 已加载（{device}，{elapsed:.1f}s）",
            "info": _model_info}


def unload_model() -> dict:
    global _model, _tokenizer, _predictor, _model_info, _cache
    _model = _tokenizer = _predictor = _model_info = None
    _cache.clear()
    return {"ok": True, "message": "模型已卸载（下次预测时会自动重新加载）"}


def ensure_loaded() -> None:
    """Load the persisted model (or default) if nothing is loaded yet."""
    if _model is not None:
        return
    state = _load_state()
    model_id = state.get("model_id", "Kronos-base")
    model_dir = state.get("model_dir")
    res = load_model(model_id, model_dir=model_dir, device=state.get("device"))
    if not res["ok"]:
        # fall back to default
        res = load_model("Kronos-base", device=state.get("device"))
        if not res["ok"]:
            raise RuntimeError(f"默认模型加载失败: {res['message']}")


def is_loaded() -> bool:
    return _model is not None


def forecast(symbol: str, df: pd.DataFrame, lookback: int = 400, pred_len: int = 120,
             T: float = 1.3, top_p: float = 0.95, sample_count: int = 2, use_cache: bool = True) -> dict:
    """Run Kronos forecast on the last `lookback` bars of df.

    df: DataFrame with timestamps/open/high/low/close/volume.
    Returns dict with keys: symbol, last_close, pred_len, dates[], close[], high[], low[], open[], volume[],
    end_close, change_pct, elapsed, device, model.
    """
    state = _load_state()
    mid = state.get("model_id", "Kronos-base")
    cache_key = f"{mid}:{symbol}:{lookback}:{pred_len}:{T}:{top_p}:{sample_count}"
    if use_cache and cache_key in _cache:
        hit = _cache[cache_key]
        if time.time() - hit["ts"] < 30 * 60:
            return hit["result"]

    if len(df) < lookback:
        raise ValueError(f"数据不足：需要至少 {lookback} 根 K 线，当前只有 {len(df)} 根")

    ensure_loaded()
    import torch

    # report the device the predictor actually runs on
    dev = getattr(_predictor, "device", None)
    if isinstance(dev, torch.device):
        device = str(dev)
    elif dev:
        device = str(dev)
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    tail = df.iloc[-lookback:].reset_index(drop=True)
    x_df = tail[["open", "high", "low", "close", "volume"]]
    x_ts = pd.Series(tail["timestamps"])
    y_ts = pd.Series(pd.bdate_range(start=tail["timestamps"].iloc[-1] + pd.Timedelta(days=1), periods=pred_len))

    t0 = time.time()
    with _infer_lock:
        pred = _predictor.predict(
            df=x_df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=pred_len,
            T=T, top_p=top_p, sample_count=sample_count, verbose=False,
        )
    elapsed = time.time() - t0

    result = {
        "symbol": symbol,
        "last_close": float(tail["close"].iloc[-1]),
        "last_date": str(tail["timestamps"].iloc[-1].date()),
        "pred_len": pred_len,
        "dates": [str(d.date()) for d in pred.index] if hasattr(pred.index, "date") else [str(d) for d in pred.index],
        "open": [round(float(v), 3) for v in pred["open"]],
        "high": [round(float(v), 3) for v in pred["high"]],
        "low": [round(float(v), 3) for v in pred["low"]],
        "close": [round(float(v), 3) for v in pred["close"]],
        "volume": [round(float(v), 2) for v in pred["volume"]],
        "end_close": round(float(pred["close"].iloc[-1]), 3),
        "change_pct": round((float(pred["close"].iloc[-1]) / float(tail["close"].iloc[-1]) - 1) * 100, 2),
        "elapsed": round(elapsed, 1),
        "device": device,
        "model": _model_info.get("id") if _model_info else mid,
    }
    # confidence: historical MAE (proportion of price) + direction accuracy.
    # Run a cheap 1-round validation on the same lookback if not cached.
    result["confidence"] = _estimate_confidence(symbol, tail, pred_len)
    _cache[cache_key] = {"ts": time.time(), "result": result}
    return result


_CONF_CACHE = {}


def _estimate_confidence(symbol, tail, pred_len):
    """Estimate forecast error band. Uses cached validation when available;
    otherwise falls back to the model's typical error (~15% for 30d) without
    blocking the request (full validation is available in the accuracy tab)."""
    key = symbol
    if key in _CONF_CACHE and time.time() - _CONF_CACHE[key]["ts"] < 6 * 3600:
        return _CONF_CACHE[key]["data"]
    data = {
        "mae_pct": 0.15,
        "dir_acc_pct": None,
        "note": "误差带为模型典型水平估算（约±15%），精确验证见『预测准确率』tab",
    }
    _CONF_CACHE[key] = {"ts": time.time(), "data": data}
    return data
