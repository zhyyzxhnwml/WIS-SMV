# -*- coding: utf-8 -*-
"""插件静态校验与 findings 校验。

【这道闸门挡的是什么】
  挡 AI 的**无心之失**，不挡有心人。黑名单提高门槛，但不是沙箱 ——
  Python 的逃逸面很大（str.format 的 {0.__class__}、__reduce__ 链等都不经过
  Attribute 节点，静态看不见）。真正的兜底在 sandbox.py 的独立子进程：
  那个进程不 import app / llm_providers / pg_db，环境变量里没有密钥，socket 被堵。
  静态校验负责让「明显危险的代码根本进不了库」，仅此而已。

【为什么连 while True 都要拦】
  静态挡死循环是廉价的（覆盖最常见的写法），挡不住的靠运行时超时。
  两层都要有：只有静态会漏，只有超时会让整批数据等满 3 秒。
"""
import ast

# ── 尺寸上限 ──
MAX_SOURCE_BYTES = 32 * 1024
MAX_MANIFEST_BYTES = 4 * 1024
MAX_AST_NODES = 4000

# ── 导入白名单：纯计算/文本处理，没有任何 IO 或进程能力 ──
ALLOWED_IMPORTS = frozenset({
    "math", "statistics", "decimal", "json", "re", "datetime",
    "collections", "itertools", "functools", "typing", "dataclasses", "enum",
})

# ── 禁用名字：出现在 Name 或 Attribute.attr 上即拒 ──
FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "__import__", "breakpoint", "memoryview",
    "exit", "quit", "help", "object", "type", "super",
})

# ── 允许的模块级语句（之后不许有其它语句，防导入即执行）──
_MODULE_LEVEL_ALLOWED = (ast.Import, ast.ImportFrom, ast.FunctionDef,
                         ast.AsyncFunctionDef, ast.ClassDef, ast.Assign,
                         ast.AnnAssign, ast.Expr)
_SEVERITY_OK = ("low", "mid", "high")


class ValidationError(Exception):
    """校验失败。消息面向用户，会回灌给 AI 起草器，也会显示在开发者页。"""


def _rule(no: int, msg: str) -> ValidationError:
    return ValidationError(f"[静态校验 {no}] {msg}")


def check_source(source: str, expected_hooks: list[str] | None = None) -> dict:
    """静态校验插件源码。通过返回统计信息，不通过抛 ValidationError。

    expected_hooks: manifest 里声明的 hooks。声明了就必须真的注册得上，
    否则是「说了不做」—— 这种插件启用后永远返回空，比报错更难察觉。
    """
    if not isinstance(source, str) or not source.strip():
        raise _rule(1, "源码为空")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise _rule(2, f"源码超过 {MAX_SOURCE_BYTES // 1024}KB")

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise _rule(3, f"语法错误：第 {e.lineno} 行 {e.msg}")

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise _rule(4, f"AST 节点数 {len(nodes)} 超过 {MAX_AST_NODES}")

    # ── 必须提供 PLUGIN 与 apply ──
    top_assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)}
    top_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "PLUGIN" not in top_assigns:
        raise _rule(5, "缺少模块级 PLUGIN = {...} 声明")
    if "apply" not in top_funcs:
        raise _rule(6, "缺少 def apply(ctx) 入口")
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "apply":
            if len(n.args.args) != 1 or n.args.vararg or n.args.kwarg:
                raise _rule(7, "apply 必须恰有一个位置参数：def apply(ctx)")

    # ── 模块级不得有副作用（导入即执行）──
    for n in tree.body:
        if isinstance(n, ast.Expr):
            if not (isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)):
                raise _rule(8, f"模块级第 {n.lineno} 行有可执行语句（只允许文档字符串）")
            continue
        if not isinstance(n, _MODULE_LEVEL_ALLOWED):
            raise _rule(8, f"模块级第 {n.lineno} 行不允许 {type(n).__name__}（导入即执行）")
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            value = n.value
            if value is not None and any(isinstance(x, ast.Call) for x in ast.walk(value)):
                raise _rule(8, f"模块级第 {n.lineno} 行的赋值里有函数调用（导入即执行）")

    # ── 逐节点检查 ──
    registered: set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise _rule(9, f"第 {n.lineno} 行不允许导入 {a.name}")
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                raise _rule(9, f"第 {n.lineno} 行不允许相对导入")
            if (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                raise _rule(9, f"第 {n.lineno} 行不允许导入 {n.module}")
            if any(a.name == "*" for a in n.names):
                raise _rule(9, f"第 {n.lineno} 行不允许 from ... import *")
        elif isinstance(n, ast.Name):
            if n.id in FORBIDDEN_NAMES:
                raise _rule(10, f"第 {n.lineno} 行使用了禁用名字 {n.id}")
        elif isinstance(n, ast.Attribute):
            if n.attr in FORBIDDEN_NAMES:
                raise _rule(10, f"第 {n.lineno} 行使用了禁用属性 .{n.attr}")
            if n.attr.startswith("__") and n.attr.endswith("__"):
                raise _rule(11, f"第 {n.lineno} 行访问了双下划线属性 .{n.attr}"
                                "（沙箱逃逸的常用路径）")
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            raise _rule(12, f"第 {n.lineno} 行使用了 {type(n).__name__}")
        elif isinstance(n, ast.While):
            if isinstance(n.test, ast.Constant) and n.test.value is True:
                raise _rule(13, f"第 {n.lineno} 行 while True（挡不住的靠运行时超时）")
        elif isinstance(n, ast.Call):
            fn = n.func
            if isinstance(fn, ast.Attribute) and fn.attr == "register":
                for kw in n.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        registered.add(str(kw.value.value))

    # ── hooks 声明了就必须注册 ──
    missing = [h for h in (expected_hooks or []) if h not in registered]
    if missing:
        raise _rule(14, "hooks 声明了 " + ", ".join(missing)
                        + " 但 apply() 里没有 ctx.tools.register(name=...) 注册它")

    return {"ast_nodes": len(nodes), "registered_tools": sorted(registered),
            "bytes": len(source.encode("utf-8"))}


def check_findings(findings, max_findings: int = 10) -> tuple[list[dict], list[str]]:
    """校验插件返回的 findings，返回 (清洗后的列表, 错误列表)。

    不合法的单条被丢弃并记错，而不是整批失败 —— 一条坏数据不该让整个插件成果作废。

    【code 与 catalog_code 为什么要分两个字段】
      插件用自己发明的码（fishing_in_restricted_zone），violation_catalog 认不出它，
      normalize_violation_code 返回 'unknown'。如果把它当 code 存下去，页面上所有
      插件判定都会显示成同一个 "unknown"，插件自己的语义就丢了。
      所以：code 原样保留（页面上要看得懂），catalog_code 只在**认出**时才填，
      给「跨插件按规范码聚合」用；认不出就是空串，而不是 'unknown' ——
      空串的语义是「这条不属于任何现有规范码」，比 'unknown' 准确。
    """
    errors: list[str] = []
    if not isinstance(findings, list):
        return [], [f"handler 必须返回 list，收到 {type(findings).__name__}"]
    if len(findings) > max_findings:
        errors.append(f"返回 {len(findings)} 条，超过上限 {max_findings}，多余部分丢弃")
        findings = findings[:max_findings]

    try:
        from ais_accelerator.violation_catalog import normalize_violation_code
    except Exception:
        def normalize_violation_code(x):
            return "unknown"

    clean: list[dict] = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            errors.append(f"第 {i + 1} 条不是字典，已丢弃")
            continue
        code = str(f.get("code") or "").strip()[:80]
        if not code:
            errors.append(f"第 {i + 1} 条缺 code，已丢弃")
            continue
        catalog_code = normalize_violation_code(code)
        if catalog_code == "unknown":
            catalog_code = ""
        try:
            conf = float(f.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        sev = str(f.get("severity") or "").lower()
        if sev and sev not in _SEVERITY_OK:
            errors.append(f"第 {i + 1} 条 severity={sev!r} 非法，置空")
            sev = ""
        raise_to = str(f.get("raise_risk_to") or "").lower()
        if raise_to and raise_to not in _SEVERITY_OK:
            errors.append(f"第 {i + 1} 条 raise_risk_to={raise_to!r} 非法，忽略")
            raise_to = ""

        span = f.get("span")
        if not isinstance(span, list):
            span = []
        try:
            metric = float(f.get("metric") or 0)
        except (TypeError, ValueError):
            metric = 0.0

        clean.append({
            "code": code,
            "catalog_code": catalog_code,
            "label": str(f.get("label") or "")[:200],
            "confidence": max(0.0, min(1.0, conf)),
            "severity": sev,
            "evidence": str(f.get("evidence") or "")[:1000],
            "metric": metric,
            "metric_unit": str(f.get("metric_unit") or "")[:20],
            "span": [str(x)[:40] for x in span[:2]],
            "regulation_ref": str(f.get("regulation_ref") or "")[:200],
            "raise_risk_to": raise_to,
        })
    return clean, errors
