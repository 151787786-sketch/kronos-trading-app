"""End-to-end smoke test: load Kronos-base locally and run a real prediction on CUDA."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import torch

from model import Kronos, KronosTokenizer, KronosPredictor

MODELS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

print(f"torch {torch.__version__} cuda={torch.cuda.is_available()}")
device = "cuda" if torch.cuda.is_available() else "cpu"

t0 = time.time()
tokenizer = KronosTokenizer.from_pretrained(os.path.join(MODELS, "Kronos-Tokenizer-base"))
model = Kronos.from_pretrained(os.path.join(MODELS, "Kronos-base"))
print(f"model loaded in {time.time()-t0:.1f}s, params={sum(p.numel() for p in model.parameters())/1e6:.1f}M")

predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)

df = pd.read_csv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "BTC_USDT_5min_sample.csv"))
df["timestamps"] = pd.to_datetime(df["timestamps"])

lookback, pred_len = 400, 120
x_df = df.loc[:lookback - 1, ["open", "high", "low", "close", "volume", "amount"]]
x_ts = df.loc[:lookback - 1, "timestamps"]
y_ts = df.loc[lookback:lookback + pred_len - 1, "timestamps"]

t0 = time.time()
pred = predictor.predict(
    df=x_df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=pred_len,
    T=1.0, top_p=0.9, sample_count=1, verbose=True,
)
print(f"prediction took {time.time()-t0:.1f}s on {device}")
print("forecast head:")
print(pred.head())
assert pred.shape[0] == pred_len
print("SMOKE TEST PASSED")
