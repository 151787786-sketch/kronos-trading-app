"""LLM 适配器（预留）：配置 API key 后可用大模型生成 Agent 观点。

无 key 时上层自动回退到规则引擎（agents/engine.py），保证本地可跑。

支持 DeepSeek（OpenAI 兼容接口）。配置方式（任选）：
  1) 环境变量：DEEPSEEK_API_KEY=sk-xxx
  2) 配置文件：trading-app/llm_config.json  {"provider":"deepseek","api_key":"sk-xxx","model":"deepseek-chat"}
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "llm_config.json")

DEFAULT_CONFIG = {
    "enabled": False,
    "provider": "deepseek",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "timeout": 60,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # env override
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if key:
        cfg["api_key"] = key
        cfg["enabled"] = True
    if cfg.get("api_key"):
        cfg["enabled"] = cfg.get("enabled", True)
    return cfg


def save_config(**kw) -> dict:
    cfg = load_config()
    cfg.update({k: v for k, v in kw.items() if v is not None})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def is_available() -> bool:
    cfg = load_config()
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def chat(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> dict:
    """Call the LLM. Returns {'ok': bool, 'text': str, 'error': str}."""
    cfg = load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")):
        return {"ok": False, "error": "LLM 未配置（回退规则引擎）", "text": ""}
    try:
        import requests
        r = requests.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}",
                     "Content-Type": "application/json"},
            json={
                "model": cfg.get("model", "deepseek-chat"),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "stream": False,
            },
            timeout=cfg.get("timeout", 60),
        )
        r.raise_for_status()
        j = r.json()
        text = j["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "error": ""}
    except Exception as e:
        return {"ok": False, "error": str(e), "text": ""}
