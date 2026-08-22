"""Pre-download Kronos-base + Kronos-Tokenizer-base weights from hf-mirror.com.

Uses huggingface_hub snapshot_download so the standard HF cache layout is used.
Set HF_ENDPOINT to the mirror before calling from_pretrained in the webui.
"""
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".hf"))

from huggingface_hub import snapshot_download

REPOS = [
    "NeoQuasar/Kronos-Tokenizer-base",
    "NeoQuasar/Kronos-base",
]

for repo in REPOS:
    print(f"Downloading {repo} ...")
    path = snapshot_download(repo_id=repo, endpoint=os.environ["HF_ENDPOINT"])
    print(f"  -> {path}")

print("All weights downloaded.")
print(f"HF_HOME={os.environ['HF_HOME']}")
