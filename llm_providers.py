"""
llm_providers.py — 模型提供商注册表 + 用途绑定
================================================

【这个文件做什么？】
  把「用哪个模型」从「一份全局配置」升级为「多个提供商 + 按用途绑定」：

    providers  一个可用的模型服务（本地 Ollama / 云厂商 OpenAI 兼容网关）
    bindings   用途 → (提供商, 模型)。三个用途：
                 chat  对话与智能体（shipclaw / 后端兜底对话）
                 judge 级联判定 L3（llm_layer/handler.py）
                 draft AI 起草规则包（阶段 2 接入，建议绑强模型）

【为什么要有它？】
  原来只有一份 base_url + model（llm_config.py），换模型就是换全局。判定要用
  便宜的模型、起草要用强的，做不到；本地与云端也没法并存。

【api_key 的三条规矩】
  1. GET 接口永不返回原文 —— 只给 has_key + 末 4 位（key_hint）
  2. 落盘前用 SECRET_KEY 派生的 Fernet 加密（data/llm_providers.json 里只有密文）
  3. 支持按 provider 读环境变量覆盖密钥（如 DEEPSEEK_API_KEY），env 优先于落盘值
  密钥变了导致解不开时，当作「没配密钥」处理并置 key_error，不抛异常、不静默用错值。

【明确不覆盖】
  - embedding 模型/维度（regulation_db.py）：向量已钉死 1024，不在 UI 暴露。

【持久化】
  data/llm_providers.json  提供商档案
  data/llm_bindings.json   用途绑定

【零配置】
  两个文件都不存在时，自动从 data/llm_config.json 迁出一个本地 Ollama 提供商，
  并把三个用途都绑到它 —— 所以「什么都不配」的行为与改造前逐字一致。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
PROVIDERS_FILE = DATA_DIR / "llm_providers.json"
BINDINGS_FILE = DATA_DIR / "llm_bindings.json"
LEGACY_CONFIG_FILE = DATA_DIR / "llm_config.json"

KINDS = ("ollama", "openai_compat", "anthropic")
KIND_LABELS = {
    "ollama": "本地 Ollama",
    "openai_compat": "OpenAI 兼容（DeepSeek / 通义 / vLLM / one-api）",
    "anthropic": "Anthropic（预留，未实装）",
}
PURPOSES = ("chat", "judge", "draft")
PURPOSE_LABELS = {"chat": "对话与智能体", "judge": "级联判定", "draft": "规则包起草"}

# 与 llm_config.DEFAULT_CONFIG 保持一致：零配置时的本地 Ollama 档案
DEFAULT_OLLAMA = {
    "name": "本地 Ollama",
    "kind": "ollama",
    "base_url": "http://localhost:11434",
    "model": "qwen2.5:7b-instruct-q4_K_M",
}
DEFAULT_PARAMS = {
    "temperature": 0.7,
    "num_predict": 600,
    "top_p": None,   # 可空
    "top_k": None,   # 可空
    "system_prompt": "",  # 可空：非空时前置追加到各链路内置人格之前
}

_lock = threading.RLock()
_providers: list[dict] = []
_bindings: dict[str, dict] = {}
_loaded = False


# ═══════════════════════════════════════════════════════════
# 密钥加密（Fernet，密钥派生自 SECRET_KEY）
# ═══════════════════════════════════════════════════════════

def _secret() -> str:
    """与 app.py:54 同源。容器里由 docker-compose 注入 SECRET_KEY。"""
    return os.environ.get("SECRET_KEY", "zhihaixunhang-dev-secret")


def _fernet():
    """惰性导入 cryptography —— 缺依赖时给一句能照着做的话，而不是 ImportError 栈。

    requirements.txt 里已钉 cryptography==47.0.0；这样写是为了让「镜像没重建」
    这种常见失误表现为一句可读的提示。
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少 cryptography 依赖，无法读写 API 密钥。"
            "请确认 requirements.txt 里的 cryptography 已安装并重建镜像。"
        ) from e
    key = base64.urlsafe_b64encode(hashlib.sha256(_secret().encode("utf-8")).digest())
    return Fernet(key)


def encrypt_key(plain: str) -> str:
    """明文 → 密文（写盘用）。空串原样返回空串。"""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_key(token: str) -> tuple[str, str]:
    """密文 → (明文, 错误信息)。

    解不开（换了 SECRET_KEY、密文被手改、依赖缺失）时返回 ('', 原因)，
    调用方据此提示「密钥需要重新填写」。绝不让异常冒到请求处理里。
    """
    if not token:
        return "", ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8"), ""
    except Exception as e:
        return "", f"密钥无法解密（{type(e).__name__}）。若更换过 SECRET_KEY，请重新填写密钥。"


def key_hint(plain: str) -> str:
    """给界面看的掩码：只露末 4 位。"""
    if not plain:
        return ""
    tail = plain[-4:] if len(plain) >= 4 else plain
    return f"****{tail}"


def mask_key(plain: str) -> str:
    """兼容旧调用名。"""
    return key_hint(plain)


# ═══════════════════════════════════════════════════════════
# 环境变量覆盖（密钥与地址）
# ═══════════════════════════════════════════════════════════

def _env_slug(pid: str) -> str:
    """provider id → 环境变量前缀。fy-gw_2 → FY_GW_2"""
    return re.sub(r"[^A-Za-z0-9]+", "_", pid or "").strip("_").upper()


def env_api_key(pid: str) -> str:
    """按 provider 读 API 密钥环境变量，env 优先于落盘值。

    两种写法都认：<ID>_API_KEY（如 DEEPSEEK_API_KEY）与 LLM_API_KEY_<ID>。
    """
    slug = _env_slug(pid)
    if not slug:
        return ""
    for name in (f"{slug}_API_KEY", f"LLM_API_KEY_{slug}"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return ""


def env_base_url(pid: str) -> str:
    slug = _env_slug(pid)
    if not slug:
        return ""
    for name in (f"{slug}_BASE_URL", f"LLM_BASE_URL_{slug}"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return ""


# ═══════════════════════════════════════════════════════════
# 加载 / 落盘
# ═══════════════════════════════════════════════════════════

def _read_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 文件损坏 → 当没有，走默认/迁移，不阻塞启动
        return None
    return None


def _write_json(path: Path, obj) -> str:
    """落盘；失败返回错误信息（内存已生效，不阻塞）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return ""
    except Exception as e:
        return f"已生效，但写入文件失败：{e}"


def _normalize_params(raw: dict | None) -> dict:
    """参数归一：非法值退回默认，空串视为 None。"""
    out = dict(DEFAULT_PARAMS)
    if not isinstance(raw, dict):
        return out
    try:
        out["temperature"] = float(raw.get("temperature", out["temperature"]))
    except (TypeError, ValueError):
        pass
    try:
        out["num_predict"] = int(raw.get("num_predict", out["num_predict"]))
    except (TypeError, ValueError):
        pass
    tp = raw.get("top_p")
    out["top_p"] = float(tp) if tp not in (None, "") else None
    tk = raw.get("top_k")
    out["top_k"] = int(tk) if tk not in (None, "") else None
    if isinstance(raw.get("system_prompt"), str):
        out["system_prompt"] = raw["system_prompt"]
    return out


def _new_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24]
    return f"{slug or 'provider'}-{uuid.uuid4().hex[:4]}"


def _normalize_provider(raw: dict) -> dict | None:
    """把落盘/入参的一条档案规整成内部形状；无法修复的返回 None。"""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip() or _new_id(str(raw.get("name") or ""))
    kind = str(raw.get("kind") or "ollama").strip()
    if kind not in KINDS:
        kind = "ollama"
    models = [str(m).strip() for m in (raw.get("models") or []) if str(m).strip()]
    # capabilities 只能取 PURPOSES —— 必须与 validate_provider 同一套口径。
    # 否则 GET 把档案原样吐给前端、前端改个名字再 PUT 回来会被自己拒掉。
    caps = [c for c in (str(c).strip() for c in (raw.get("capabilities") or [])) if c in PURPOSES]
    if not caps:
        caps = list(PURPOSES)   # 没写或写了个都不认识的 → 按「三种用途都能用」处理
    return {
        "id": pid,
        "name": str(raw.get("name") or pid),
        "kind": kind,
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key_enc": str(raw.get("api_key_enc") or ""),
        "models": models,
        "enabled": bool(raw.get("enabled", True)),
        "capabilities": sorted(set(caps)),
        "params": _normalize_params(raw.get("params")),
    }


def _migrate_from_legacy() -> tuple[list[dict], dict]:
    """零配置迁移：把 data/llm_config.json 变成「一个本地 Ollama 提供商 + 三个绑定」。

    直接读文件而不是 import llm_config —— 那边是本模块的消费者，反向 import 会成环。
    """
    legacy = _read_json(LEGACY_CONFIG_FILE) or {}
    base_url = DEFAULT_OLLAMA["base_url"]
    model = DEFAULT_OLLAMA["model"]
    if isinstance(legacy, dict):
        if isinstance(legacy.get("base_url"), str) and legacy["base_url"].strip():
            base_url = legacy["base_url"].strip()
        if isinstance(legacy.get("model"), str) and legacy["model"].strip():
            model = legacy["model"].strip()
    pid = "ollama-local"
    provider = _normalize_provider({
        "id": pid,
        "name": DEFAULT_OLLAMA["name"],
        "kind": "ollama",
        "base_url": base_url,
        "models": [model],
        "enabled": True,
        "capabilities": list(PURPOSES),
        "params": _normalize_params(legacy if isinstance(legacy, dict) else None),
    })
    bindings = {p: {"provider_id": pid, "model": model, "params": {}} for p in PURPOSES}
    return [provider], bindings


def _load():
    """启动/重载：读两个 JSON；缺 providers 文件时走迁移。"""
    global _providers, _bindings, _loaded
    migrated = False
    raw = _read_json(PROVIDERS_FILE)
    if isinstance(raw, dict) and isinstance(raw.get("providers"), list):
        provs = [p for p in (_normalize_provider(x) for x in raw["providers"]) if p]
        bnd = raw.get("bindings") if isinstance(raw.get("bindings"), dict) else None
    elif isinstance(raw, list):  # 容错：整份就是数组
        provs = [p for p in (_normalize_provider(x) for x in raw) if x] if raw else []
        bnd = None
    else:
        provs, bnd = _migrate_from_legacy()
        migrated = True

    if not provs:
        provs, bnd = _migrate_from_legacy()
        migrated = True

    if bnd is None:
        bnd = _read_json(BINDINGS_FILE) if isinstance(_read_json(BINDINGS_FILE), dict) else {}
    clean: dict[str, dict] = {}
    ids = {p["id"] for p in provs}
    for purpose in PURPOSES:
        b = bnd.get(purpose) if isinstance(bnd, dict) else None
        if isinstance(b, dict) and b.get("provider_id") in ids:
            clean[purpose] = {
                "provider_id": b["provider_id"],
                "model": str(b.get("model") or "").strip(),
                "params": b.get("params") if isinstance(b.get("params"), dict) else {},
            }
    # 没有绑定的用途不写死 —— 留给 resolve() 按能力回落，避免迁移期就锁死
    _providers, _bindings, _loaded = provs, clean, True

    if migrated:
        # 迁移结果立刻落盘：让「系统现在认哪些提供商」这件事在磁盘上看得见，
        # 而不是每次启动都重新推一遍。写失败不阻塞启动（内存已可用）。
        save_all()


def _ensure_loaded():
    if not _loaded:
        with _lock:
            if not _loaded:
                _load()


def save_all() -> str:
    """把当前状态落盘。返回错误信息（空串=成功）。"""
    with _lock:
        err1 = _write_json(PROVIDERS_FILE, {
            "version": 1,
            "providers": copy.deepcopy(_providers),
        })
        err2 = _write_json(BINDINGS_FILE, copy.deepcopy(_bindings))
        return err1 or err2


def reload():
    """从磁盘重载（测试与排查用）。"""
    global _loaded
    with _lock:
        _loaded = False
        _load()


# ═══════════════════════════════════════════════════════════
# 读：给界面看的形状（永不含密钥原文）
# ═══════════════════════════════════════════════════════════

def public_view(p: dict) -> dict:
    """一条档案的对外形状：密钥只给 has_key / key_hint / key_error。"""
    plain, err = decrypt_key(p.get("api_key_enc") or "")
    from_env = env_api_key(p["id"])
    effective = from_env or plain
    return {
        "id": p["id"],
        "name": p["name"],
        "kind": p["kind"],
        "kind_label": KIND_LABELS.get(p["kind"], p["kind"]),
        "base_url": p["base_url"],
        "effective_base_url": env_base_url(p["id"]) or p["base_url"],
        "models": list(p["models"]),
        "enabled": p["enabled"],
        "capabilities": list(p["capabilities"]),
        "params": copy.deepcopy(p["params"]),
        "has_key": bool(effective),
        "key_hint": key_hint(effective),
        "key_from_env": bool(from_env),
        "key_error": err,
        "needs_key": p["kind"] in ("openai_compat", "anthropic") and not effective,
        "implemented": p["kind"] != "anthropic",
    }


def list_providers(include_disabled: bool = True) -> list[dict]:
    _ensure_loaded()
    with _lock:
        return [public_view(p) for p in _providers
                if include_disabled or p["enabled"]]


def get_provider(pid: str) -> dict | None:
    _ensure_loaded()
    with _lock:
        for p in _providers:
            if p["id"] == pid:
                return copy.deepcopy(p)
    return None


def get_bindings() -> dict[str, dict]:
    """用途绑定（含解析后的实际生效模型，界面要显示「最终用哪个」）。"""
    _ensure_loaded()
    out = {}
    for purpose in PURPOSES:
        r = resolve(purpose)
        out[purpose] = {
            "purpose": purpose,
            "label": PURPOSE_LABELS[purpose],
            "provider_id": r["provider_id"],
            "provider_name": r["provider_name"],
            "model": r["model"],
            "explicit": purpose in _bindings,
            "params": copy.deepcopy(r["params"]),
        }
    return out


def _legacy_ollama_env() -> tuple[str, str]:
    """老的两个环境变量：OLLAMA_BASE_URL / OLLAMA_MODEL。

    它们是「容器部署优先」那次的修复（见 llm_config.py 旧注释）：compose 里设的
    地址/模型不该被宿主机遗留的 json 顶掉。放在这里而不是兼容层里，是为了让
    新出口 complete() 和老的 llm_config.get_config() 看到同一个答案 ——
    否则会出现「dev 页显示 A 模型、实际请求打了 B 模型」这种最难查的错。
    """
    return (os.environ.get("OLLAMA_BASE_URL", "").strip(),
            os.environ.get("OLLAMA_MODEL", "").strip())


def resolve(purpose: str = "chat") -> dict:
    """解析某个用途最终用哪个提供商与模型。

    回落顺序：显式绑定 → 第一个启用且声明该能力的提供商 → 第一个启用的提供商
    → 本地 Ollama 默认（保证任何情况下都有答案，调用方不必判空）。
    """
    _ensure_loaded()
    with _lock:
        provs = list(_providers)
        b = _bindings.get(purpose) or {}
        chosen = next((p for p in provs if p["id"] == b.get("provider_id")), None)
        if chosen is None:
            chosen = next((p for p in provs if p["enabled"] and purpose in p["capabilities"]), None)
        if chosen is None:
            chosen = next((p for p in provs if p["enabled"]), None) or (provs[0] if provs else None)

        if chosen is None:  # 档案全被删光：退回本地 Ollama 默认，不报错
            env_url, env_model = _legacy_ollama_env()
            return {
                "provider_id": "", "provider_name": DEFAULT_OLLAMA["name"], "kind": "ollama",
                "base_url": env_url or DEFAULT_OLLAMA["base_url"], "api_key": "",
                "model": env_model or DEFAULT_OLLAMA["model"], "params": dict(DEFAULT_PARAMS),
                "capabilities": list(PURPOSES), "system_prompt": "",
            }

        model = str(b.get("model") or "").strip() or (chosen["models"][0] if chosen["models"] else "")
        params = copy.deepcopy(chosen["params"])
        # 绑定的参数覆盖提供商默认（只覆盖显式给出的键）
        for k, v in (b.get("params") or {}).items():
            if k in DEFAULT_PARAMS:
                params[k] = v
        plain, _err = decrypt_key(chosen.get("api_key_enc") or "")
        base_url = env_base_url(chosen["id"]) or chosen["base_url"]
        if chosen["kind"] == "ollama":
            env_url, env_model = _legacy_ollama_env()
            base_url = env_url or base_url
            model = env_model or model
        return {
            "provider_id": chosen["id"],
            "provider_name": chosen["name"],
            "kind": chosen["kind"],
            "base_url": base_url,
            "api_key": env_api_key(chosen["id"]) or plain,
            "model": model or DEFAULT_OLLAMA["model"],
            "params": params,
            "capabilities": list(chosen["capabilities"]),
            "system_prompt": str(params.get("system_prompt") or ""),
        }


# ═══════════════════════════════════════════════════════════
# 写：档案与绑定
# ═══════════════════════════════════════════════════════════

def validate_provider(data: dict, *, pid: str | None = None) -> tuple[bool, str]:
    """校验一条档案。返回 (ok, 错误信息)。"""
    if not isinstance(data, dict):
        return False, "提供商必须是对象"
    name = str(data.get("name") or "").strip()
    if not name:
        return False, "名称不能为空"
    kind = str(data.get("kind") or "").strip()
    if kind not in KINDS:
        return False, f"类型必须是 {' / '.join(KINDS)} 之一"
    base_url = str(data.get("base_url") or "").strip()
    if not base_url:
        return False, "base_url 不能为空"
    if not re.match(r"^https?://", base_url):
        return False, "base_url 必须以 http:// 或 https:// 开头"
    models = data.get("models")
    if models is not None and not isinstance(models, list):
        return False, "models 必须是数组"
    caps = data.get("capabilities")
    if caps is not None:
        if not isinstance(caps, list):
            return False, "capabilities 必须是数组"
        bad = [c for c in caps if c not in PURPOSES]
        if bad:
            return False, f"未知用途：{'、'.join(map(str, bad))}（可选 {'/'.join(PURPOSES)}）"
    ok_p, err = _validate_params(data.get("params") or {})
    if not ok_p:
        return False, err
    return True, ""


def _validate_params(params: dict) -> tuple[bool, str]:
    if not isinstance(params, dict):
        return False, "params 必须是对象"
    if "temperature" in params:
        try:
            t = float(params["temperature"])
        except (TypeError, ValueError):
            return False, "temperature 必须是数字"
        if not (0 <= t <= 2):
            return False, "temperature 必须在 0~2 之间"
    if "num_predict" in params and params["num_predict"] is not None:
        try:
            n = int(params["num_predict"])
        except (TypeError, ValueError):
            return False, "num_predict 必须是整数"
        if n < 1:
            return False, "num_predict 必须 ≥ 1"
    if params.get("top_p") not in (None, ""):
        try:
            p = float(params["top_p"])
        except (TypeError, ValueError):
            return False, "top_p 必须是数字或留空"
        if not (0 < p <= 1):
            return False, "top_p 必须在 0~1 之间"
    if params.get("top_k") not in (None, ""):
        try:
            k = int(params["top_k"])
        except (TypeError, ValueError):
            return False, "top_k 必须是整数或留空"
        if k < 1:
            return False, "top_k 必须 ≥ 1"
    if not isinstance(params.get("system_prompt", ""), str):
        return False, "system_prompt 必须是字符串"
    return True, ""


def upsert_provider(data: dict, *, pid: str | None = None) -> tuple[bool, str, dict | None]:
    """新建或修改一条档案。返回 (ok, 错误信息, 对外形状)。

    **改（给了 pid）是部分更新，不是整体覆盖**：缺席的字段一律沿用原值。
    api_key 的语义（重要，避免「改别的字段把密钥清空」）：
      字段缺席     → 保持原密钥不动
      ""          → 清除密钥
      非空字符串   → 覆盖为新密钥（加密后落盘）

    注意「先合并、后校验」：早先是先校验原始载荷再合并，于是「只把名字改一下」
    这种最普通的请求会因为载荷里没有 kind/base_url 而被自己的校验拒掉（400），
    而对外的 api_key 语义又明确允诺了缺席即保持 —— 两套口径打架。现在统一成一套：
    合并出完整的候选档案，再拿它去校验。新建（无 pid）仍要求 name/kind/base_url 齐全。
    """
    if not isinstance(data, dict):
        return False, "提供商必须是对象", None
    _ensure_loaded()
    with _lock:
        target = None
        if pid:
            target = next((p for p in _providers if p["id"] == pid), None)
            if target is None:
                return False, "提供商不存在", None

        merged = dict(data)
        if target:
            for key in ("name", "kind", "base_url", "models", "enabled",
                        "capabilities", "params"):
                merged.setdefault(key, target[key])
        ok_v, err = validate_provider(merged, pid=pid)
        if not ok_v:
            return False, err, None
        new_id = pid or _new_id(str(data.get("name")))
        if not pid and any(p["id"] == new_id for p in _providers):
            new_id = _new_id(str(data.get("name")))

        prev_enc = target["api_key_enc"] if target else ""
        if "api_key" in data:
            raw_key = data.get("api_key")
            if raw_key in (None, ""):
                enc = ""
            else:
                try:
                    enc = encrypt_key(str(raw_key))
                except RuntimeError as e:
                    return False, str(e), None
        else:
            enc = prev_enc

        record = _normalize_provider({
            "id": new_id,
            "name": merged.get("name"),
            "kind": merged.get("kind"),
            "base_url": merged.get("base_url"),
            "api_key_enc": enc,
            "models": merged.get("models") or [],
            "enabled": merged.get("enabled", True),
            "capabilities": merged.get("capabilities"),
            "params": merged.get("params"),
        })
        if record is None:
            return False, "提供商档案无法解析", None
        if target:
            target.clear()
            target.update(record)
        else:
            _providers.append(record)
        err = save_all()
        return True, err, public_view(record)


def delete_provider(pid: str) -> tuple[bool, str]:
    """删除档案。被绑定引用时拒绝 —— 否则那个用途会静默换成别的模型。"""
    _ensure_loaded()
    with _lock:
        used = [p for p, b in _bindings.items() if b.get("provider_id") == pid]
        if used:
            labels = '、'.join(PURPOSE_LABELS.get(p, p) for p in used)
            return False, f"该提供商正被用于「{labels}」，请先改绑到别的提供商再删除"
        before = len(_providers)
        _providers[:] = [p for p in _providers if p["id"] != pid]
        if len(_providers) == before:
            return False, "提供商不存在"
        save_all()
        return True, ""


def set_bindings(data: dict) -> tuple[bool, str]:
    """更新用途绑定。只接受 PURPOSES 里的键；未给出的用途保持不变。"""
    if not isinstance(data, dict):
        return False, "绑定必须是对象"
    _ensure_loaded()
    for purpose, b in data.items():
        if purpose not in PURPOSES:
            return False, f"未知用途：{purpose}"
        if b is None:      # 传 null = 取消显式绑定，回到自动回落
            with _lock:
                _bindings.pop(purpose, None)
            continue
        if not isinstance(b, dict):
            return False, f"{purpose} 的绑定必须是对象"
        pid = str(b.get("provider_id") or "").strip()
        with _lock:
            prov = next((p for p in _providers if p["id"] == pid), None)
        if prov is None:
            return False, f"{purpose} 指定的提供商不存在：{pid or '(空)'}"
        if not prov["enabled"]:
            return False, f"{purpose} 指定的提供商已停用：{prov['name']}"
        model = str(b.get("model") or "").strip()
        if model and prov["models"] and model not in prov["models"]:
            # 不拦 —— 用户可能刚拉了个新模型还没刷新列表。但记在返回值里让界面提示。
            pass
        with _lock:
            _bindings[purpose] = {
                "provider_id": pid,
                "model": model,
                "params": b.get("params") if isinstance(b.get("params"), dict) else {},
            }
    err = save_all()
    return True, err


def delete_provider_force(pid: str) -> tuple[bool, str]:
    """删除档案并解除所有引用它的绑定（测试/应急用）。"""
    _ensure_loaded()
    with _lock:
        for purpose in list(_bindings):
            if _bindings[purpose].get("provider_id") == pid:
                del _bindings[purpose]
        _providers[:] = [p for p in _providers if p["id"] != pid]
        save_all()
        return True, ""


# ═══════════════════════════════════════════════════════════
# 适配器：探测模型列表 / 测连通 / 发一次补全
# ═══════════════════════════════════════════════════════════

def _norm_openai_base(base_url: str) -> str:
    """openai 兼容网关的 /v1 前缀补全。

    三种写法都要认：
      https://api.deepseek.com             → 根地址，补成 .../v1（最常见）
      https://api.openai.com/v1            → 已经带了，原样
      https://open.bigmodel.cn/api/paas/v4 → 自带版本段，**原样**（补成 .../v4/v1 是不存在的地址）

    判据是「有没有路径」，不是「结尾是不是 /v1」—— 早先按后者写，凡是用自有版本段
    的服务（智谱 /api/paas/v4、千帆 /v2、火山方舟 /api/v3、Gemini /v1beta/openai）
    都会被补出一个 /v1 后缀，于是预设里这些厂商**必然测试连接失败**，报的还是 404 这种
    看不出原因的错。宁可让「根地址」这一种情况补 /v1，也不要替用户猜带路径的地址。
    """
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return b
    rest = b.split("://", 1)[-1]
    path = rest.split("/", 1)[1] if "/" in rest else ""
    return f"{b}/v1" if not path else b


def _http_json(url: str, *, method: str = "GET", payload: dict | None = None,
               headers: dict | None = None, api_key: str = "", timeout: float = 15.0):
    """发一个 JSON 请求，返回 (ok, 数据或错误信息)。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json"}
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return True, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            return False, f"鉴权失败（HTTP {e.code}）：密钥无效或权限不足 {detail}".strip()
        return False, f"HTTP {e.code}：{detail or e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def list_models(pid: str, timeout: float = 15.0) -> tuple[bool, dict]:
    """探测某个提供商的可用模型。返回 (ok, {online, models, error})。"""
    prov = get_provider(pid)
    if prov is None:
        return False, {"online": False, "models": [], "error": "提供商不存在"}
    kind = prov["kind"]
    base = env_base_url(pid) or prov["base_url"]
    plain, derr = decrypt_key(prov.get("api_key_enc") or "")
    api_key = env_api_key(pid) or plain

    if kind == "anthropic":
        return False, {"online": False, "models": [],
                       "error": "Anthropic 适配器是预留骨架，尚未实装"}
    if kind == "openai_compat":
        if not api_key:
            return False, {"online": False, "models": [], "error": "尚未填写 API 密钥"}
        okr, data = _http_json(f"{_norm_openai_base(base)}/models",
                               api_key=api_key, timeout=timeout)
        if not okr:
            return False, {"online": False, "models": [], "error": data}
        models = [{"name": m.get("id"), "size": 0, "modified_at": ""}
                  for m in (data.get("data") or []) if m.get("id")]
        return True, {"online": True, "models": models, "error": ""}

    # ollama
    okr, data = _http_json(f"{base}/api/tags", timeout=timeout)
    if not okr:
        return False, {"online": False, "models": [], "error": data}
    models = [{"name": m.get("name"), "size": m.get("size", 0),
               "modified_at": m.get("modified_at", "")}
              for m in (data.get("models") or [])]
    if derr:
        return True, {"online": True, "models": models, "error": derr}
    return True, {"online": True, "models": models, "error": ""}


def _ollama_options(params: dict) -> dict:
    opts: dict = {"num_predict": int(params.get("num_predict") or 600)}
    if params.get("top_p") is not None:
        opts["top_p"] = float(params["top_p"])
    if params.get("top_k") is not None:
        opts["top_k"] = int(params["top_k"])
    return opts


def complete(prompt: str, system: str = "", *, purpose: str = "chat",
             timeout: float = 60.0, model: str | None = None) -> tuple[str, str]:
    """按用途发一次补全，返回 (文本, 错误信息)。

    这是所有链路的统一出口：ollama 走 /api/generate，openai 兼容走
    /v1/chat/completions。调用方只管拿到字符串，不必知道背后是哪家。
    """
    r = resolve(purpose)
    kind = r["kind"]
    mdl = model or r["model"]
    params = r["params"]
    sys_text = system or r.get("system_prompt") or ""

    if kind == "anthropic":
        return "", "Anthropic 适配器是预留骨架，尚未实装"

    if kind == "openai_compat":
        if not r["api_key"]:
            return "", f"提供商「{r['provider_name']}」尚未配置 API 密钥"
        messages = []
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": mdl,
            "messages": messages,
            "temperature": float(params.get("temperature") or 0.7),
            "max_tokens": int(params.get("num_predict") or 600),
            "stream": False,
        }
        if params.get("top_p") is not None:
            payload["top_p"] = float(params["top_p"])
        okr, data = _http_json(f"{_norm_openai_base(r['base_url'])}/chat/completions",
                               method="POST", payload=payload,
                               api_key=r["api_key"], timeout=timeout)
        if not okr:
            return "", data
        try:
            return (data["choices"][0]["message"]["content"] or "").strip(), ""
        except (KeyError, IndexError, TypeError):
            return "", f"响应格式不符预期：{str(data)[:200]}"

    # ollama
    payload = {
        "model": mdl,
        "prompt": prompt,
        "stream": False,
        "temperature": float(params.get("temperature") or 0.7),
        "options": _ollama_options(params),
    }
    if sys_text:
        payload["system"] = sys_text
    okr, data = _http_json(f"{r['base_url'].rstrip('/')}/api/generate",
                           method="POST", payload=payload, timeout=timeout)
    if not okr:
        return "", data
    return str(data.get("response") or "").strip(), ""


def test(pid: str, model: str = "", timeout: float = 60.0) -> tuple[bool, dict]:
    """用短 prompt 验证某个提供商连通与模型可用。返回 (ok, {reply, elapsed_ms}|{error})。"""
    prov = get_provider(pid)
    if prov is None:
        return False, {"error": "提供商不存在"}
    mdl = model or (prov["models"][0] if prov["models"] else DEFAULT_OLLAMA["model"])
    started = time.perf_counter()
    # 临时绑定到该提供商再发 —— 复用同一条出口，避免测的路径与实际用的路径不一致
    global _bindings
    with _lock:
        saved = copy.deepcopy(_bindings)
        _bindings["chat"] = {"provider_id": pid, "model": mdl, "params": {}}
    try:
        text, err = complete("请用一句话回复：服务正常。", purpose="chat", timeout=timeout)
    finally:
        with _lock:
            _bindings = saved
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if err:
        return False, {"error": err, "elapsed_ms": elapsed_ms, "model": mdl}
    return True, {"reply": text, "elapsed_ms": elapsed_ms, "model": mdl}


_ensure_loaded()
