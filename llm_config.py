"""
llm_config.py — 全局 LLM 模型与参数配置单例
==============================================

【这个文件做什么？】
  集中管理所有 AI 链路的模型名、Ollama 地址与生成参数。
  消费点在「调用时」读取本模块，改完即时生效，无需重启。

【覆盖链路】
  - 网页 AI 对话（shipclaw/core.py:_call_ollama_chat）
  - 智能体闲聊兜底（backend/app.py:_call_ollama）
  - 级联判定 LLMHandler（llm_layer/handler.py）
  - 加速器缓存 key（ais_accelerator/pipeline.py）

【明确不覆盖】
  - embedding 模型/维度（regulation_db.py）：向量已强制 pad/截断到 1024 且哈希兜底，
    改动无收益且换维度会撑爆向量集合。
  - cascade_agent.model_name：死代码。

【持久化】
  配置写入 data/llm_config.json，重启后自动加载。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = ROOT_DIR / "data" / "llm_config.json"

DEFAULT_CONFIG = {
    "base_url": "http://localhost:11434",
    "model": "qwen2.5:7b-instruct-q4_K_M",
    "temperature": 0.7,
    "num_predict": 600,
    "top_p": None,      # 可空
    "top_k": None,      # 可空
    "system_prompt": "",  # 可空：非空时前置追加到各链路内置人格之前
}

_lock = threading.Lock()
_cache = dict(DEFAULT_CONFIG)


def _load():
    """启动时从 JSON 文件加载（不存在则用默认）。

    容器部署优先：环境变量 OLLAMA_BASE_URL / OLLAMA_MODEL 覆盖持久化配置，
    保证 docker-compose 里设的地址/模型不会被宿主机遗留的 data/llm_config.json 顶掉。
    """
    global _cache
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULT_CONFIG:
                    if k in raw:
                        cfg[k] = raw[k]
    except Exception:
        # 文件损坏/读不到 → 回退默认，不阻塞启动
        cfg = dict(DEFAULT_CONFIG)
    # env 覆盖持久化值（docker-compose 设置，容器内必用）
    env_url = os.environ.get("OLLAMA_BASE_URL")
    if env_url:
        cfg["base_url"] = env_url
    env_model = os.environ.get("OLLAMA_MODEL")
    if env_model:
        cfg["model"] = env_model
    _cache = cfg


def validate(new: dict) -> tuple[bool, str]:
    """校验配置。返回 (ok, 错误信息)。"""
    if not isinstance(new, dict):
        return False, "配置必须是对象"
    base_url = new.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return False, "base_url 不能为空"
    model = new.get("model")
    if not isinstance(model, str) or not model.strip():
        return False, "model 不能为空"
    try:
        temperature = float(new.get("temperature", DEFAULT_CONFIG["temperature"]))
    except (TypeError, ValueError):
        return False, "temperature 必须是数字"
    if not (0 <= temperature <= 2):
        return False, "temperature 必须在 0~2 之间"
    try:
        num_predict = int(new.get("num_predict", DEFAULT_CONFIG["num_predict"]))
    except (TypeError, ValueError):
        return False, "num_predict 必须是整数"
    if num_predict < 1:
        return False, "num_predict 必须 ≥ 1"
    top_p = new.get("top_p")
    if top_p is not None:
        try:
            top_p = float(top_p)
        except (TypeError, ValueError):
            return False, "top_p 必须是数字或留空"
        if not (0 < top_p <= 1):
            return False, "top_p 必须在 0~1 之间"
    top_k = new.get("top_k")
    if top_k is not None:
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            return False, "top_k 必须是整数或留空"
        if top_k < 1:
            return False, "top_k 必须 ≥ 1"
    if not isinstance(new.get("system_prompt", ""), str):
        return False, "system_prompt 必须是字符串"
    return True, ""


def get_config() -> dict:
    """返回当前配置的深拷贝副本。"""
    with _lock:
        return copy.deepcopy(_cache)


def get(key: str):
    """读取单个配置项（调用时读取，改完即时生效）。"""
    with _lock:
        return _cache.get(key)


def get_ollama_options() -> dict:
    """构造 Ollama options 参数（num_predict + 非空 top_p/top_k）。"""
    with _lock:
        opts: dict = {"num_predict": int(_cache.get("num_predict", 600))}
        if _cache.get("top_p") is not None:
            opts["top_p"] = float(_cache["top_p"])
        if _cache.get("top_k") is not None:
            opts["top_k"] = int(_cache["top_k"])
        return opts


def save(new: dict) -> tuple[bool, str]:
    """校验并保存配置（落盘 + 更新内存，带锁）。返回 (ok, 错误信息)。"""
    ok, err = validate(new)
    if not ok:
        return False, err
    with _lock:
        global _cache
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: new.get(k, _cache.get(k)) for k in DEFAULT_CONFIG})
        merged["temperature"] = float(new.get("temperature", merged["temperature"]))
        merged["num_predict"] = int(new.get("num_predict", merged["num_predict"]))
        top_p = new.get("top_p")
        merged["top_p"] = float(top_p) if top_p not in (None, "") else None
        top_k = new.get("top_k")
        merged["top_k"] = int(top_k) if top_k not in (None, "") else None
        merged["system_prompt"] = str(new.get("system_prompt", merged["system_prompt"]))
        _cache = merged
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            # 落盘失败不应阻止内存生效，但返回警告
            return True, f"已生效，但写入文件失败：{e}"
    return True, ""


def reset() -> dict:
    """恢复默认配置并落盘。"""
    with _lock:
        global _cache
        _cache = dict(DEFAULT_CONFIG)
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return copy.deepcopy(_cache)


_load()
