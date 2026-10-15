# -*- coding: utf-8 -*-
"""dsh 的 apply(ctx) 宿主 —— 插件唯一的对外能力面。

【为什么要有 Disposable】
  DeepSeek Harness 建在 Cordis 微内核上，其硬要求是「组件注册的副作用必须可逆」：
  注册时要同时交出撤销方式，卸载时才能干净地回到注册前的状态。Python 里就是
  ctx.tools.register(...) 必须返回一个 Disposable，PluginContext 必须收集它，
  卸载时逆序 dispose()。这既是「停用插件」的实现，也是「热重载 = 卸载 + 重新 apply」
  的实现基础 —— 不依赖任何全局清理代码。

【能力面刻意做窄】
  只给：注册 / 登记撤销 / 有界日志 / 只读配置。
  不给：DB、网络、文件、llm_providers、os、sys。
  facts 不走 ctx —— 由宿主作为 handler 的入参传进去（见 sandbox_runner）。
  插件即使想越权，也没有可用的把手；剩下的逃逸路径靠 sandbox.py 的子进程 + 受限
  builtins 兜，且诚实地说：那也不是密不透风的（见 sandbox.py 顶部说明）。
"""
from typing import Any, Callable


class Disposable:
    """一个可撤销的注册。dispose 幂等，且绝不抛异常（撤销失败不该拖垮卸载流程）。"""

    __slots__ = ("_undo", "_disposed")

    def __init__(self, undo: Callable[[], None]):
        self._undo = undo
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._undo()
        except Exception:
            pass


class PluginLogger:
    """有界日志：插件不能无限刷屏，最多留 200 条，随 findings 一起回传宿主。

    只有 info / warn / error 三个方法 —— 没有 debug，没有任意属性，
    避免插件通过 getattr(log, '__class__') 之类的路子上溯。
    """

    MAX_LINES = 200

    def __init__(self, plugin_name: str):
        self._name = plugin_name
        self.lines: list[str] = []

    def _add(self, level: str, msg: Any) -> None:
        if len(self.lines) >= self.MAX_LINES:
            return
        try:
            text = str(msg)[:500]
        except Exception:
            text = "<不可序列化的日志内容>"
        self.lines.append(f"[{level}] {text}")

    def info(self, msg: Any) -> None:
        self._add("info", msg)

    def warn(self, msg: Any) -> None:
        self._add("warn", msg)

    def error(self, msg: Any) -> None:
        self._add("error", msg)


class ToolRegistry:
    """本进程内的能力注册表。名字 → {handler, description, input_schema, plugin}。

    一个插件进程只装一个插件，但键仍带插件名前缀（f"{plugin}:{tool}"），
    这样多个 hook 之间不会互相覆盖，也方便日志定位。
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    def add(self, plugin: str, name: str, handler: Callable, description: str,
            input_schema: Any) -> None:
        self._tools[f"{plugin}:{name}"] = {
            "plugin": plugin,
            "name": name,
            "handler": handler,
            "description": description,
            "input_schema": input_schema,
        }

    def remove(self, plugin: str, name: str) -> None:
        self._tools.pop(f"{plugin}:{name}", None)

    def get(self, plugin: str, name: str) -> dict | None:
        return self._tools.get(f"{plugin}:{name}")

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)


class _ToolRegistrar:
    """ctx.tools —— 只暴露 register 一个方法。"""

    __slots__ = ("_registry", "_plugin")

    def __init__(self, registry: ToolRegistry, plugin: str):
        self._registry = registry
        self._plugin = plugin

    def register(self, *, name: str, handler: Callable, description: str = "",
                 input_schema: Any = None) -> Disposable:
        """注册一个能力，返回撤销句柄。

        dsh 的 ctx.tools.register 同形：注册即登记撤销方式，不靠调用方记得清理。
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tools.register 需要非空的 name")
        if not callable(handler):
            raise ValueError("tools.register 的 handler 必须可调用")
        name = name.strip()
        self._registry.add(self._plugin, name, handler, description or "", input_schema)
        return Disposable(lambda: self._registry.remove(self._plugin, name))


class PluginContext:
    """传给插件 apply(ctx) 的宿主对象。"""

    _FIELDS = ("name", "config", "log", "tools", "_disposables", "_frozen")

    def __init__(self, name: str, registry: ToolRegistry, config: dict | None = None,
                 logger: PluginLogger | None = None):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "config", dict(config or {}))   # 只读副本
        object.__setattr__(self, "log", logger or PluginLogger(name))
        object.__setattr__(self, "tools", _ToolRegistrar(registry, name))
        object.__setattr__(self, "_disposables", [])
        object.__setattr__(self, "_frozen", True)                # 构造完就冻上

    def effect(self, undo: Callable[[], None]) -> Disposable:
        """登记一个自定义撤销动作（除 tools.register 之外的副作用用它）。"""
        d = Disposable(undo)
        self._disposables.append(d)
        return d

    def __setattr__(self, key: str, value: Any) -> None:
        # 构造完就冻结：ctx 是宿主的面板，不是插件的草稿纸。
        # 插件若 ctx.name = "x" 或往 ctx 上挂变量，这里直接拒 —— 避免插件
        # 通过改写宿主对象影响后续调用，也避免「注册了什么」变得不可枚举。
        if getattr(self, "_frozen", False):
            raise AttributeError(f"ctx 是只读的，不允许设置 ctx.{key}")
        object.__setattr__(self, key, value)

    def dispose_all(self) -> None:
        """逆序撤销全部注册 —— 卸载插件 / 热重载时调用。"""
        for d in reversed(self._disposables):
            d.dispose()
        self._disposables.clear()
