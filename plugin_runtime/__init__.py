# -*- coding: utf-8 -*-
"""plugin_runtime —— dsh 风格的可执行插件系统。

【导入必须轻】
  `python -m plugin_runtime.sandbox_runner` 会先执行本文件，再跑子进程入口。
  子进程里绝不该出现 pydantic / psycopg2 / ollama —— 那既是性能问题（每次起子进程
  都要重新导入），也是安全问题（导入链上可能碰到持有密钥的模块）。
  所以这里不 import 任何子模块，改用 PEP 562 的 __getattr__ 惰性取。
"""
__all__ = [
    "run_adjudicators", "preload_registry", "reset_budget",
    "build_facts", "sample_facts", "SAMPLE_FACTS", "FACTS_VERSION", "FEATURE_DOC",
    "parse_plugin_file", "validate_source", "ValidationError", "check_findings",
    "get_sandbox", "Sandbox",
    "forge_plugin", "detect_forge_intent", "trial_plugin", "ForgeError",
    "store", "registry", "manifest", "context", "facts", "validate", "sandbox", "forge",
]

# 名字 → (子模块, 该模块内的属性名)
_LAZY = {
    "run_adjudicators": ("registry", "run_adjudicators"),
    "preload_registry": ("registry", "preload_registry"),
    "reset_budget": ("registry", "reset_budget"),
    "build_facts": ("facts", "build_facts"),
    "sample_facts": ("facts", "sample_facts"),
    "SAMPLE_FACTS": ("facts", "SAMPLE_FACTS"),
    "FACTS_VERSION": ("facts", "FACTS_VERSION"),
    "FEATURE_DOC": ("facts", "FEATURE_DOC"),
    "parse_plugin_file": ("manifest", "parse_plugin_file"),
    "validate_source": ("validate", "check_source"),
    "check_findings": ("validate", "check_findings"),
    "ValidationError": ("validate", "ValidationError"),
    "get_sandbox": ("sandbox", "get_sandbox"),
    "Sandbox": ("sandbox", "Sandbox"),
    "forge_plugin": ("forge", "forge_plugin"),
    "detect_forge_intent": ("forge", "detect_forge_intent"),
    "trial_plugin": ("forge", "trial"),
    "ForgeError": ("forge", "ForgeError"),
}
# 直接可 import 的子模块（`from plugin_runtime import store` 这种用法）
_MODULES = frozenset({"store", "registry", "manifest", "context", "facts",
                      "validate", "sandbox", "forge"})


def __getattr__(name):
    if name in _MODULES:
        import importlib
        mod = importlib.import_module("." + name, __name__)
        globals()[name] = mod          # 缓存，后续不再走 __getattr__
        return mod
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    mod = importlib.import_module("." + target[0], __name__)
    value = getattr(mod, target[1])
    globals()[name] = value            # 缓存
    return value


def __dir__():
    return sorted(set(__all__) | set(globals()))
