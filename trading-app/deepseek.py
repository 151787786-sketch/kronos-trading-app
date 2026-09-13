"""DeepSeek LLM 核心适配层 —— 全项目统一调用入口。

配置来源（按优先级）：
  1) 环境变量 DEEPSEEK_API_KEY / LLM_API_KEY
  2) trading-app/llm_config.json
  3) ~/.dsh/.credentials.yaml（本机已存在的 DeepSeek 凭据，自动发现）

能力：
  * chat()        —— 单轮对话，支持 JSON 模式、温度、max_tokens、重试
  * chat_json()   —— 强制返回 JSON 并解析（失败自动重试一次精简提示）
  * available()   —— 是否可用（无 key 时所有上层自动回退规则引擎）
  * 内存缓存      —— 相同 prompt 不重复计费
  * 并发闸门      —— 限制并发请求数，避免打爆限流
  * 用量统计      —— 累计调用次数 / token / 失败次数，页面可见
"""
import hashlib
import json
import os
import re
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "llm_config.json")
DSH_CRED = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")

DEFAULT_CONFIG = {
    "enabled": True,
    "provider": "deepseek",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "timeout": 90,
    "max_tokens": 2000,
    "temperature": 0.3,
    "concurrency": 4,
    "cache_ttl": 900,
}

_lock = threading.Lock()
_cache = {}
_sem = None
_sem_size = 0
_stats = {"calls": 0, "ok": 0, "fail": 0, "cache_hits": 0, "prompt_tokens": 0,
          "completion_tokens": 0, "last_error": "", "last_latency": 0.0}
_stats_lock = threading.Lock()


# ---------------------------------------------------------------- config

def _read_dsh_credential() -> str:
    """从 DSH 凭据文件读取 DEEPSEEK_API_KEY（本机已登录 DeepSeek 时可直接复用）。"""
    try:
        if not os.path.exists(DSH_CRED):
            return ""
        with open(DSH_CRED, encoding="utf-8") as f:
            for line in f:
                if "DEEPSEEK_API_KEY" in line:
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key and not cfg.get("api_key"):
        key = _read_dsh_credential()
        if key:
            cfg["_key_source"] = "dsh"
    if key:
        cfg["api_key"] = key
        cfg["enabled"] = True
    elif cfg.get("api_key"):
        cfg.setdefault("_key_source", "file")
    return cfg


def save_config(**kw) -> dict:
    cfg = load_config()
    cfg.update({k: v for k, v in kw.items() if v is not None})
    cfg.pop("_key_source", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _cache.clear()
    return cfg


def available() -> bool:
    cfg = load_config()
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def status() -> dict:
    cfg = load_config()
    with _stats_lock:
        st = dict(_stats)
    key = cfg.get("api_key") or ""
    return {
        "available": available(),
        "provider": cfg.get("provider", "deepseek"),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "key_masked": (key[:6] + "***" + key[-4:]) if len(key) > 12 else ("已配置" if key else ""),
        "key_source": cfg.get("_key_source", "env/file" if key else "none"),
        "concurrency": cfg.get("concurrency", 4),
        "cache_ttl": cfg.get("cache_ttl", 900),
        **st,
    }


# ---------------------------------------------------------------- internals

def _semaphore(n: int):
    global _sem, _sem_size
    with _lock:
        if _sem is None or _sem_size != n:
            _sem = threading.BoundedSemaphore(max(1, int(n)))
            _sem_size = n
        return _sem


def _cache_get(key):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < load_config().get("cache_ttl", 900):
            return hit[1]
        if hit:
            _cache.pop(key, None)
    return None


def _cache_put(key, val):
    with _lock:
        if len(_cache) > 800:
            _cache.clear()
        _cache[key] = (time.time(), val)


def _bump(**kw):
    with _stats_lock:
        for k, v in kw.items():
            if k in ("last_error", "last_latency"):
                _stats[k] = v
            else:
                _stats[k] = _stats.get(k, 0) + v


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


# ---------------------------------------------------------------- chat

def chat(messages, temperature=None, max_tokens=None, json_mode=False,
         retries=2, use_cache=True, tag=""):
    """调用 DeepSeek。messages 可为 [{'role':..,'content':..}] 或 (system, user) 元组。

    也兼容简写 chat("system 提示", "user 提示")：第二个位置参数若是字符串，
    自动识别为 user 消息（历史上这个位置参数是 temperature，容易误传）。
    返回 {'ok': bool, 'text': str, 'error': str, 'cached': bool, 'latency': float}
    """
    cfg = load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")):
        return {"ok": False, "text": "", "error": "LLM 未配置", "cached": False, "latency": 0.0}

    # 简写容错：chat(system, user) —— temperature 位置收到了字符串
    if isinstance(temperature, str):
        sys_msg = messages if isinstance(messages, str) else str(messages)
        messages = [{"role": "system", "content": sys_msg},
                    {"role": "user", "content": temperature}]
        temperature = None

    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    elif isinstance(messages, (tuple, list)) and messages and isinstance(messages[0], str):
        if len(messages) == 2:
            messages = [{"role": "system", "content": messages[0]},
                        {"role": "user", "content": messages[1]}]
        else:
            messages = [{"role": "user", "content": messages[0]}]

    payload = {
        "model": cfg.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.3) if temperature is None else temperature,
        "max_tokens": cfg.get("max_tokens", 2000) if max_tokens is None else max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    ckey = hashlib.md5((tag + "|" + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
    if use_cache:
        hit = _cache_get(ckey)
        if hit is not None:
            _bump(cache_hits=1)
            return {"ok": True, "text": hit, "error": "", "cached": True, "latency": 0.0}

    import requests
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    timeout = cfg.get("timeout", 90)
    sem = _semaphore(cfg.get("concurrency", 4))

    last_err = ""
    for attempt in range(max(1, retries + 1)):
        t0 = time.time()
        try:
            with sem:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            dt = time.time() - t0
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                _bump(calls=1, fail=1, last_error=last_err[:300], last_latency=round(dt, 2))
                return {"ok": False, "text": "", "error": last_err, "cached": False, "latency": dt}
            j = r.json()
            text = (j.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            usage = j.get("usage") or {}
            _bump(calls=1, ok=1,
                  prompt_tokens=usage.get("prompt_tokens", 0),
                  completion_tokens=usage.get("completion_tokens", 0),
                  last_error="", last_latency=round(dt, 2))
            if use_cache and text:
                _cache_put(ckey, text)
            return {"ok": True, "text": text, "error": "", "cached": False, "latency": dt}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
    _bump(calls=1, fail=1, last_error=last_err[:300])
    return {"ok": False, "text": "", "error": last_err, "cached": False, "latency": 0.0}


def chat_json(system_prompt, user_prompt, temperature=0.2, max_tokens=2000,
              retries=1, tag="json", use_cache=True):
    """要求模型返回 JSON 并解析为 dict/list。失败返回 None（调用方回退规则引擎）。"""
    res = chat([{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}],
               temperature=temperature, max_tokens=max_tokens,
               json_mode=True, retries=retries, tag=tag, use_cache=use_cache)
    if not res["ok"]:
        return None
    raw = _strip_fence(res["text"])
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def chat_json_list(system_prompt, user_prompt, temperature=0.2, max_tokens=2000,
                   retries=1, tag="jsonlist", use_cache=True):
    """要求模型返回 JSON 数组并解析。模型常把数组包在 {"xxx":[...]} 里，这里自动拆包。"""
    data = chat_json(system_prompt, user_prompt, temperature=temperature,
                     max_tokens=max_tokens, retries=retries, tag=tag, use_cache=use_cache)
    return unwrap_list(data)


def unwrap_list(data):
    """从 dict 包装中取出数组（suggestions/items/data/results 或唯一的 list 值）。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("suggestions", "items", "data", "results", "list", "array", "proposals"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


def unwrap_list(data):
    """从 dict 包装中取出数组（suggestions/items/data/results 或唯一的 list 值）。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("suggestions", "items", "data", "results", "list", "array", "proposals"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


def parallel(jobs, workers=None):
    """并发执行多个 chat 任务。jobs: [(tag, messages, kwargs), ...] -> [(tag, result), ...]"""
    from concurrent.futures import ThreadPoolExecutor
    n = workers or load_config().get("concurrency", 4)

    def run(job):
        tag, messages = job[0], job[1]
        kwargs = job[2] if len(job) > 2 else {}
        return tag, chat(messages, tag=tag, **kwargs)

    with ThreadPoolExecutor(max_workers=max(1, n)) as ex:
        return list(ex.map(run, jobs))
