"""
llm_config.py — 全局 LLM 模型与参数配置（兼容层）
==================================================

【这个文件现在做什么？】
  它是「阶段 1 之前」那套单一全局配置的**兼容入口**，实现已搬到 llm_providers.py。
  下面的函数签名与返回值一字未改，内部改为「解析 chat 用途绑定」：

    get_config()           → 7 字段 dict（base_url/model/temperature/num_predict/
                             top_p/top_k/system_prompt），来自 chat 绑定的提供商
    get(key)               → 单个字段
    get_ollama_options()   → Ollama options（num_predict + 非空 top_p/top_k）
    save(new) / reset()    → 写回 chat 绑定所属的提供商档案
    validate(new)          → 校验（与原实现逐字相同）

【为什么保留这一层？】
  现有消费点一处不动就能继续工作：
    - shipclaw/core.py:_call_ollama_chat（对话与智能体）
    - backend/app.py:_call_ollama（闲聊兜底）
    - llm_layer/handler.py（级联判定 L3）
    - ais_accelerator/pipeline.py（缓存 key 用 model 名，换模型自动失效旧缓存）
  零配置（没有任何提供商档案）时行为与改造前逐字一致 —— 见 llm_providers._migrate_from_legacy。

【覆盖链路】
  同上四处。新增的按用途解析（judge / draft 可用不同模型）走 llm_providers.resolve()。

【明确不覆盖】
  - embedding 模型/维度（regulation_db.py）：向量已强制 pad/截断到 1024 且哈希兜底，
    改动无收益且换维度会撑爆向量集合。
  - cascade_agent.model_name：死代码。

【持久化】
  配置现在写在 data/llm_providers.json + data/llm_bindings.json。
  data/llm_config.json 仅在首次启动、档案文件还不存在时被读一次（迁移来源），此后不再写入。
"""

from __future__ import annotations

import copy

import llm_providers

# 保持与原实现同名同义：零配置（无任何提供商档案）时的本地 Ollama 默认值
DEFAULT_CONFIG = {
    "base_url": llm_providers.DEFAULT_OLLAMA["base_url"],
    "model": llm_providers.DEFAULT_OLLAMA["model"],
    "temperature": 0.7,
    "num_predict": 600,
    "top_p": None,      # 可空
    "top_k": None,      # 可空
    "system_prompt": "",  # 可空：非空时前置追加到各链路内置人格之前
}


def _shape(resolved: dict) -> dict:
    """llm_providers.resolve() 的结果 → 原 7 字段形状。"""
    p = resolved.get("params") or {}
    return {
        "base_url": resolved.get("base_url") or DEFAULT_CONFIG["base_url"],
        "model": resolved.get("model") or DEFAULT_CONFIG["model"],
        "temperature": p.get("temperature", DEFAULT_CONFIG["temperature"]),
        "num_predict": p.get("num_predict", DEFAULT_CONFIG["num_predict"]),
        "top_p": p.get("top_p"),
        "top_k": p.get("top_k"),
        "system_prompt": str(p.get("system_prompt") or ""),
    }


def validate(new: dict) -> tuple[bool, str]:
    """校验配置。返回 (ok, 错误信息)。（与原实现逐字相同）"""
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
    """返回当前配置的深拷贝副本（解析 chat 用途绑定）。"""
    return _shape(llm_providers.resolve("chat"))


def get(key: str):
    """读取单个配置项（调用时读取，改完即时生效）。"""
    return get_config().get(key)


def get_ollama_options() -> dict:
    """构造 Ollama options 参数（num_predict + 非空 top_p/top_k）。"""
    cfg = get_config()
    opts: dict = {"num_predict": int(cfg.get("num_predict") or 600)}
    if cfg.get("top_p") is not None:
        opts["top_p"] = float(cfg["top_p"])
    if cfg.get("top_k") is not None:
        opts["top_k"] = int(cfg["top_k"])
    return opts


def save(new: dict) -> tuple[bool, str]:
    """校验并保存配置。返回 (ok, 错误信息)。

    写的是「对话与智能体」绑定所属那条提供商档案 —— 原来只有一份全局配置，
    现在它落在 chat 绑定的提供商上。落盘失败不影响内存生效（与原实现一致）。
    """
    ok, err = validate(new)
    if not ok:
        return False, err

    cur = get_config()
    merged = copy.deepcopy(cur)
    for k in DEFAULT_CONFIG:
        if k in new and new[k] is not None:
            merged[k] = new[k]
    merged["temperature"] = float(new.get("temperature", merged["temperature"]))
    merged["num_predict"] = int(new.get("num_predict", merged["num_predict"]))
    top_p = new.get("top_p")
    merged["top_p"] = float(top_p) if top_p not in (None, "") else None
    top_k = new.get("top_k")
    merged["top_k"] = int(top_k) if top_k not in (None, "") else None
    merged["system_prompt"] = str(new.get("system_prompt", merged["system_prompt"]))
    merged["base_url"] = str(new.get("base_url", merged["base_url"]))
    merged["model"] = str(new.get("model", merged["model"]))

    target = llm_providers.resolve("chat")
    pid = target.get("provider_id")
    if not pid:
        return False, "当前没有可用的模型提供商，请先在「模型与提供商」里添加一个"

    prov = llm_providers.get_provider(pid) or {}
    models = list(prov.get("models") or [])
    if merged["model"] not in models:
        models.insert(0, merged["model"])
    patch = {
        "name": prov.get("name"),
        "kind": prov.get("kind"),
        "base_url": merged["base_url"],
        "models": models,
        "enabled": prov.get("enabled", True),
        "capabilities": prov.get("capabilities"),
        "params": {
            "temperature": merged["temperature"],
            "num_predict": merged["num_predict"],
            "top_p": merged["top_p"],
            "top_k": merged["top_k"],
            "system_prompt": merged["system_prompt"],
        },
    }
    ok_u, err_u, _ = llm_providers.upsert_provider(patch, pid=pid)
    if not ok_u:
        return False, err_u
    llm_providers.set_bindings({"chat": {"provider_id": pid, "model": merged["model"]}})
    return True, err_u


def reset() -> dict:
    """恢复默认（复位 chat 绑定所属提供商的模型与参数）并落盘。"""
    target = llm_providers.resolve("chat")
    pid = target.get("provider_id")
    if pid:
        prov = llm_providers.get_provider(pid) or {}
        llm_providers.upsert_provider({
            "name": prov.get("name"),
            "kind": prov.get("kind"),
            "base_url": llm_providers.DEFAULT_OLLAMA["base_url"],
            "models": [llm_providers.DEFAULT_OLLAMA["model"]],
            "enabled": prov.get("enabled", True),
            "capabilities": prov.get("capabilities"),
            "params": dict(llm_providers.DEFAULT_PARAMS),
        }, pid=pid)
        llm_providers.set_bindings({
            "chat": {"provider_id": pid, "model": llm_providers.DEFAULT_OLLAMA["model"]}
        })
    else:
        llm_providers.save_all()
    return get_config()
