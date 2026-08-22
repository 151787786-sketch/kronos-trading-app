"""WeChat push notifications via ServerChan (Server酱) or PushPlus.

Both channels push to WeChat through their official service — you need a free
token: 
- Server酱: https://sct.ftqq.com/  -> SendKey
- PushPlus: https://www.pushplus.plus/ -> token

Settings are stored in a JSON file (notify_config.json) next to this module.
"""
import json
import os
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "notify_config.json")

DEFAULT_CONFIG = {
    "enabled": False,
    "channel": "serverchan",          # serverchan | pushplus
    "serverchan_key": "",
    "pushplus_token": "",
    "test_sent_at": None,
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def update_config(channel=None, serverchan_key=None, pushplus_token=None, enabled=None) -> dict:
    cfg = load_config()
    if channel is not None:
        cfg["channel"] = channel
    if serverchan_key is not None:
        cfg["serverchan_key"] = serverchan_key.strip()
    if pushplus_token is not None:
        cfg["pushplus_token"] = pushplus_token.strip()
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    save_config(cfg)
    return cfg


def is_configured() -> bool:
    cfg = load_config()
    if not cfg["enabled"]:
        return False
    if cfg["channel"] == "serverchan":
        return bool(cfg.get("serverchan_key"))
    return bool(cfg.get("pushplus_token"))


def send(title: str, content: str) -> dict:
    """Send a WeChat push. Returns {'ok': bool, 'message': str}."""
    cfg = load_config()
    channel = cfg.get("channel", "serverchan")

    if channel == "serverchan":
        key = cfg.get("serverchan_key", "")
        if not key:
            return {"ok": False, "message": "Server酱 SendKey 未配置"}
        try:
            r = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title[:32], "desp": content},
                timeout=15,
            )
            j = r.json()
            if j.get("code") == 0:
                return {"ok": True, "message": f"Server酱推送成功: {j.get('message', '')}"}
            return {"ok": False, "message": f"Server酱返回: {j.get('message', r.text[:100])}"}
        except Exception as e:
            return {"ok": False, "message": f"Server酱请求失败: {e}"}

    elif channel == "pushplus":
        token = cfg.get("pushplus_token", "")
        if not token:
            return {"ok": False, "message": "PushPlus token 未配置"}
        try:
            r = requests.post(
                "https://www.pushplus.plus/send",
                json={"token": token, "title": title[:100], "content": content, "template": "txt"},
                timeout=15,
            )
            j = r.json()
            if j.get("code") == 200:
                return {"ok": True, "message": "PushPlus 推送成功"}
            return {"ok": False, "message": f"PushPlus 返回: {j.get('msg', r.text[:100])}"}
        except Exception as e:
            return {"ok": False, "message": f"PushPlus 请求失败: {e}"}

    return {"ok": False, "message": "未知推送渠道"}


def test_push() -> dict:
    title = "Kronos 交易助手通知测试"
    content = (f"✅ 微信通知配置成功！\n\n"
               f"渠道: {load_config().get('channel')}\n"
               f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
               f"以后买卖点触发提醒会推送到这里。")
    result = send(title, content)
    if result["ok"]:
        cfg = load_config()
        cfg["test_sent_at"] = time.time()
        save_config(cfg)
    return result
