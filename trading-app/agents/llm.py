"""LLM 适配器：统一转发到 deepseek.py（全项目共用一套配置与统计）。

保留本模块是为了兼容已有调用（committee/engine 等），实际逻辑已上移到 deepseek.py。
"""
from deepseek import available as _available, chat as _chat, chat_json, status, load_config, save_config  # noqa: F401


def is_available() -> bool:
    return _available()


def chat(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> dict:
    """兼容旧签名：返回 {'ok','text','error'}。"""
    r = _chat([{"role": "system", "content": system_prompt},
               {"role": "user", "content": user_prompt}], temperature=temperature)
    return {"ok": r["ok"], "text": r["text"], "error": r["error"]}
