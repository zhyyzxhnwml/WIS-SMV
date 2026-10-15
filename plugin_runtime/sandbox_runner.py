# -*- coding: utf-8 -*-
"""子进程入口 —— 插件代码真正被执行的地方。这里就是那道「兜底」。

【这个进程里有什么】
  只有一个 Python 解释器、一份插件源码、一份 facts，以及受限的内置函数。

【保护到底靠哪一层（按实际权重排序，别搞错主次）】
  1. **环境变量被洗干净** —— 这是真正扛事的一层。子进程的 env 里没有 SECRET_KEY、
     没有 PG_PASSWORD、没有 *_API_KEY、没有 OLLAMA_BASE_URL。而 llm_providers 的
     Fernet 密钥是从 SECRET_KEY 派生的（llm_providers.py:93），PG 口令也走环境变量
     （config.py:53）。所以即使插件完全逃出下面的所有限制，它拿到的也只是「一个
     没有任何凭据的进程」——数据库进不去、密钥解不开。
  2. **独立进程** —— 崩溃/死循环杀的是子进程，判定链不受影响。
  3. 受限 builtins + AST 白名单 + import 闸门 —— 提高顺手为之的门槛，
     **不是安全边界**（见下）。
  4. **没有网络** —— socket 在 Python 层被堵；逃逸可绕，但绕完仍无凭据、无目标。
  5. 审计 + 失败熔断 —— 「谁在什么时候启用了什么代码」有账。

【必须诚实说清的一点：Python 层沙箱做不成安全边界】
  试过一条硬化路，失败了，记在这里免得后人重走：把模块命名空间里指向危险模块的
  引用摘掉（`import enum` 之后 `enum._sys` 就是 sys，两步拿到 os.system）。实测
  **摘了就把标准库自己弄坏** —— enum 运行时要 `_sys.intern` / `_sys.modules`，
  摘掉后 NameError。也就是说：stdlib 自己就把 sys/os 递给插件，你要么保留
  re/datetime/json 这些插件必需的模块（于是递出去），要么把它们全砍掉。
  两者不可兼得 —— 这就是「Python 层沙箱不成立」的具体形态，不是设计疏漏。
  dsh 官方对创造模式同样警告「不建议在生产业务直接用」。

  真正的兜底是第一层：**子进程的环境变量里没有任何凭据**。所以即使插件走通了
  上述逃逸、拿到 os.system，它拿到的是一个「没有 SECRET_KEY、没有 PG_PASSWORD、
  没有 API key、没有网络」的进程。本系统的取舍是接受「AI 生成的代码会被真的执行」，
  把后果约束在「进程内自由 + 全程留痕」，而不是假装沙箱牢不可破。

  仍未覆盖的风险，明写在 plugins 页给超管看：逃逸后可读写同 UID 的文件系统，
  包括**本仓库源码**。若插件改写 plugin_runtime/*.py，应用重启后即在 Flask 进程内
  执行。要堵这条得靠部署层的只读挂载（容器 read_only + tmpfs），不是这里能解决的。

【协议】
  一行一个 JSON，stdin 进 stdout 出。插件自己的 print / 异常都不许污染这条流：
  print 被接到 logger，sys.stdout 被换成 sink。真正的输出只走 _OUT。
"""
import json
import sys
import time
from hashlib import sha256

# 协议专用句柄：在任何东西有机会动 sys.stdout 之前抓住它
_OUT = sys.stdout
_REAL_IMPORT = __import__

from plugin_runtime.manifest import parse_plugin_file  # noqa: E402
from plugin_runtime.context import PluginContext, ToolRegistry, PluginLogger  # noqa: E402
from plugin_runtime.validate import ALLOWED_IMPORTS, check_source, ValidationError  # noqa: E402

# ── 受限内置函数白名单 ──
# 与 validate.FORBIDDEN_NAMES 严格互补：那边禁的名字这边一律不给，两边不能有缝。
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable", "chr",
    "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "hasattr", "hash", "hex", "int", "isinstance", "issubclass", "iter", "len",
    "list", "map", "max", "min", "next", "oct", "ord", "pow", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    # 异常类：插件要能 raise / except，否则只能吞掉一切
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
    "Exception", "IndexError", "KeyError", "LookupError", "NameError",
    "NotImplementedError", "OverflowError", "RuntimeError", "StopIteration",
    "TypeError", "ValueError", "ZeroDivisionError",
)
# 注意 type / object / super / getattr / setattr / vars / globals / locals /
# eval / exec / compile / open / __import__ / memoryview 都不给；
# validate.FORBIDDEN_NAMES 里同样禁掉了它们，静态和运行时两道一致。

# ── 运行时 import 黑名单：提供「文件 / 进程 / 网络 / 内省」能力的标准库模块 ──
# 白名单（ALLOWED_IMPORTS）管的是插件**自己写的 import**，AST 在 parse 阶段就拒了；
# 这个黑名单管的是**标准库内部**触发的 import —— 那种走的是真 builtins，
# AST 看不见，只能在这里拦。
#
# 只收「能力模块」：文件 / 进程 / 网络 / 内省。像 pprint / warnings / unittest
# 这种本身无害的模块不进黑名单 —— 禁得越宽越容易误伤标准库内部的正常 import
# （实测过：砍宽了首先坏的是 datetime.strptime）。
_DENY_IMPORT_ROOTS = frozenset({
    # 文件系统与 IO
    "os", "io", "_io", "codecs", "pathlib", "shutil", "tempfile", "glob",
    "fileinput", "linecache", "tokenize", "stat", "mmap", "zipfile", "tarfile",
    "gzip", "bz2", "lzma", "zipimport", "sqlite3", "dbm", "shelve", "pickle",
    "marshal", "ctypes", "_ctypes",
    # 进程与系统
    "sys", "subprocess", "signal", "resource", "multiprocessing", "concurrent",
    "asyncio", "threading", "fcntl", "termios", "tty", "pty", "pipes", "posix",
    "nt", "msvcrt", "winreg", "_winapi", "grp", "pwd", "atexit", "faulthandler",
    "sysconfig", "platform", "site",
    # 网络
    "socket", "socketserver", "ssl", "select", "selectors", "http", "urllib",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "nntplib", "xmlrpc",
    "webbrowser", "wsgiref", "smtpd", "cgi", "cgitb", "uuid", "secrets",
    # 导入机制与内省（逃逸链的主力）
    "importlib", "imp", "runpy", "pkgutil", "modulefinder", "builtins",
    "__builtin__", "inspect", "gc", "traceback", "dis", "opcode", "ast",
    "symtable", "code", "codeop", "pdb", "bdb",
    # 会自己 import 上面这些、且对纯计算插件毫无用处的脚手架
    "ensurepip", "venv", "distutils", "pip", "setuptools", "pydoc", "idlelib",
    "turtledemo", "lib2to3", "test", "this", "antigravity", "readline",
    "rlcompleter", "compileall", "py_compile", "getpass",
})
_STDLIB_PATHS = None
_ORIGIN_CACHE: dict = {}


def _stdlib_paths():
    global _STDLIB_PATHS
    if _STDLIB_PATHS is None:
        import os
        import sysconfig
        paths = sysconfig.get_paths()
        _STDLIB_PATHS = tuple(
            os.path.normcase(os.path.realpath(paths[k]))
            for k in ("stdlib", "platstdlib") if paths.get(k)
        )
    return _STDLIB_PATHS


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """运行时的 import 闸门。

    【为什么不能只用白名单】
      已知问题：datetime.strptime 在 C 层调 PyImport_ImportModule("_strptime")，
      而 C 层取 builtins 用的是**当前帧**的 builtins —— 也就是插件那一帧，
      于是它会撞上这个闸门。白名单里没有 _strptime，strptime 就报
      「插件不允许导入 '_strptime'」。同类还有 _json、_sre、_decimal 之类。
      所以闸门必须放行「标准库自己的内部模块」。

    【这道闸门真正挡住的是什么】
      挡的是**插件主动去 import 本仓库的模块或第三方包**。
      `_is_stdlib_origin` 要求来源在 stdlib 目录内，而 llm_providers / pg_db /
      config 在仓库根、psycopg2 / cryptography 在 site-packages —— 一律拒。
      这才是关键：插件**没有**任何途径把持有 Fernet 密钥和数据库口令的代码拉进来。
      插件自己写的 `import os` 另有 AST 白名单在 parse 阶段就拒了（validate 第 9 条），
      这里是第二道，拦动态拼接出来的名字。

    【它挡不住什么】
      属性访问形成的引用链（`enum._sys.modules["os"]`）。那不是 import，闸门看不见。
      见本文件顶部「必须诚实说清的一点」。
    """
    root = (name or "").split(".")[0]
    if level != 0:
        raise ImportError(f"插件不允许相对导入 {name!r}")
    if root in _DENY_IMPORT_ROOTS:
        raise ImportError(f"插件不允许导入 {name!r}")
    if root in ALLOWED_IMPORTS or _is_stdlib_origin(root):
        return _REAL_IMPORT(name, globals, locals, fromlist, level)
    raise ImportError(f"插件不允许导入 {name!r}（仅限标准库的纯计算模块）")


def _is_stdlib_origin(root: str) -> bool:
    """模块是否来自标准库（builtin / frozen / stdlib 目录内）。

    用 find_spec 而不是先 import 再查 —— 后者会执行模块体，而模块体可能有副作用。
    带缓存避免每次 find_spec 的开销。
    """
    cached = _ORIGIN_CACHE.get(root)
    if cached is not None:
        return cached
    ok = False
    try:
        import importlib.util
        import os
        spec = importlib.util.find_spec(root)
        origin = getattr(spec, "origin", None) or ""
        if origin in ("built-in", "frozen"):
            ok = True
        elif origin and origin != "namespace":
            real = os.path.normcase(os.path.realpath(origin))
            ok = any(real.startswith(p + os.sep) for p in _stdlib_paths())
    except Exception:
        ok = False
    _ORIGIN_CACHE[root] = ok
    return ok


def _make_logged_print(log):
    def _print(*args, **kwargs):
        try:
            log.info(" ".join(str(a) for a in args))
        except Exception:
            pass
    return _print


def _restricted_builtins(log):
    import builtins as _b
    safe = {}
    for n in _SAFE_BUILTIN_NAMES:
        if hasattr(_b, n):
            safe[n] = getattr(_b, n)
    safe["__import__"] = _safe_import
    # class 语句在编译期会用到它；没有它，插件里任何 class 都会 NameError。
    # 给它不构成额外逃逸面——从类上拿到 object 再 __subclasses__ 需要双下划线
    # 属性访问，那条路已被 AST 校验堵死。
    safe["__build_class__"] = _b.__build_class__
    safe["print"] = _make_logged_print(log)
    return safe


class _Sink:
    """顶掉 sys.stdout：插件若直接 sys.stdout.write（虽然它 import 不到 sys），
    或间接让某个库去写，都不会破坏 JSON 协议。"""

    def __init__(self, log):
        self._log = log

    def write(self, s):
        if s and s.strip():
            self._log.info(str(s).strip()[:500])
        return len(s) if s else 0

    def flush(self):
        pass

    def isatty(self):
        return False


def _block_network():
    """进程级堵网络 —— 只影响本子进程，宿主不受影响。

    放在这里（而不是进程启动时）是为了让 zipimport / encodings 等已完成初始化，
    否则替换 socket.socket 可能连带影响解释器自身的启动路径。
    """
    try:
        import socket

        def _deny(*a, **k):
            raise OSError("插件不允许网络访问")

        class _BlockedSocket(socket.socket):
            def __init__(self, *a, **k):
                raise OSError("插件不允许网络访问")

        socket.socket = _BlockedSocket
        socket.create_connection = _deny
        socket.socketpair = _deny
    except Exception:
        pass


def _limit_resources():
    """Linux 容器内限制地址空间与 CPU；Windows 无 resource，降级为只靠父进程超时。

    RLIMIT_AS 取 512MB 而不是 256MB：CPython 的虚拟地址空间含大量 arena 预留，
    卡太紧会让正常插件偶发 MemoryError，那种误报比放宽一倍更难查。
    真正的硬闸是父进程的 join(timeout) + kill，这里只是防「一个插件吃光内存」。
    """
    try:
        import resource
        cap = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))     # 父进程 3s 硬顶会先到
    except Exception:
        pass


# ── 已装载的插件缓存：key = sha256(name + source) ──
# 同一个 worker 反复调同一插件时不必重跑 apply()。源码一变 key 就变，
# 老的 ctx 会被 dispose_all() 卸载 —— 这就是 dsh 的「热重载 = 卸载 + 重新 apply」。
_LOADED: dict = {}


def _load(req: dict) -> dict:
    """解析 → 静态校验 → exec → apply(ctx)，返回可复用的装载结果。"""
    name = str(req.get("name") or "")
    source = req.get("source") or ""
    key = sha256((name + "\x00" + source).encode("utf-8")).hexdigest()

    cached = _LOADED.get(key)
    if cached is not None:
        return cached

    logger = PluginLogger(name)
    sys.stdout = _Sink(logger)          # 换了 stdout 之后，任何误写都进日志

    parsed = parse_plugin_file(source)          # 可能抛 ValueError（面向用户的中文消息）
    meta = parsed["meta"]
    if meta["name"] != name:
        raise ValueError(f"manifest 里的 name={meta['name']} 与请求的 {name} 不一致")
    body = parsed["body"]

    # 子进程里再校验一次：父进程那道是主闸，这道保证「即使有人绕过父进程直接喂源码
    # 给 runner，也 exec 不了危险代码」。AST 解析是毫秒级，值得。
    check_source(body, meta["hooks"])

    registry = ToolRegistry()
    ctx = PluginContext(name, registry, config=req.get("config") or {}, logger=logger)
    globals_dict = {
        "__builtins__": _restricted_builtins(logger),
        "__name__": f"plugin_{name}",
        "__doc__": None,
        "__file__": f"<plugin:{name}>",
    }
    exec(compile(body, f"<plugin:{name}>", "exec"), globals_dict)

    apply_fn = globals_dict.get("apply")
    if not callable(apply_fn):
        raise ValueError("模块里没有可调用的 apply(ctx)")
    apply_fn(ctx)

    # 每个声明的 hook 都必须真的注册上了，否则启用后永远返回空——最难查的一种失效
    handlers = {}
    for hook in meta["hooks"]:
        entry = registry.get(name, hook)
        if entry is None:
            raise ValueError(f"hooks 声明了 {hook}，但 apply(ctx) 里没有 "
                             f"ctx.tools.register(name={hook!r}, ...)")
        handlers[hook] = entry["handler"]

    # 换 key 了：把同一插件的上一个版本逆序撤销，才叫卸载干净
    for old_key in [k for k, v in _LOADED.items() if v["name"] == name and k != key]:
        try:
            _LOADED.pop(old_key)["ctx"].dispose_all()
        except Exception:
            pass

    loaded = {"name": name, "meta": meta, "ctx": ctx, "registry": registry,
              "logger": logger, "handlers": handlers}
    _LOADED[key] = loaded
    return loaded


def _handle(req: dict) -> dict:
    t0 = time.time()
    name = str(req.get("name") or "")
    hooks = req.get("hooks") or ["adjudicate"]
    hook = str(req.get("hook") or hooks[0])
    logger = None
    stage = "ok"
    try:
        loaded = _load(req)
        logger = loaded["logger"]
        handler = loaded["handlers"].get(hook)
        if handler is None:
            stage = "apply"
            raise ValueError(f"插件没有注册 hook {hook!r}（已注册："
                             f"{loaded['registry'].names()}）")
        stage = "call"
        findings = handler(req.get("facts") or {})
        if findings is None:
            findings = []
        if not isinstance(findings, list):
            stage = "call"
            raise TypeError(f"handler 必须返回 list，收到 {type(findings).__name__}")
        return {"ok": True, "stage": "ok", "findings": findings,
                "tools": loaded["registry"].names(),
                "logs": _drain(logger), "error": "",
                "elapsed_ms": int((time.time() - t0) * 1000)}
    except ValidationError as e:
        return _fail(name, "validate", str(e), logger, t0)
    except ValueError as e:
        return _fail(name, stage if stage != "ok" else "manifest", str(e), logger, t0)
    except SyntaxError as e:
        return _fail(name, "exec", f"语法错误：第 {e.lineno} 行 {e.msg}", logger, t0)
    except BaseException as e:       # 插件 raise SystemExit / KeyboardInterrupt 也要挡住
        return _fail(name, stage, f"{type(e).__name__}: {e}", logger, t0)


def _drain(logger) -> list:
    if logger is None:
        return []
    lines, logger.lines = list(logger.lines), []
    return lines


def _fail(name, stage, error, logger, t0) -> dict:
    return {"ok": False, "stage": stage, "findings": [], "tools": [],
            "logs": _drain(logger), "error": str(error)[:2000],
            "elapsed_ms": int((time.time() - t0) * 1000)}


def main() -> None:
    _limit_resources()
    _block_network()
    stdin = sys.stdin
    while True:
        try:
            line = stdin.readline()
        except Exception:
            break
        if not line:
            break                                   # 父进程关掉了 stdin → 收工
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _respond({"id": None, "ok": False, "stage": "protocol",
                      "error": f"请求不是合法 JSON: {e}", "findings": [],
                      "logs": [], "tools": [], "elapsed_ms": 0})
            continue
        req_id = req.get("id")
        if req.get("op") == "ping":
            _respond({"id": req_id, "ok": True, "stage": "ok", "findings": [],
                      "logs": [], "tools": [], "error": "", "elapsed_ms": 0})
            continue
        if req.get("op") == "shutdown":
            break
        resp = _handle(req)
        resp["id"] = req_id
        _respond(resp)


def _respond(obj: dict) -> None:
    # default=str：插件返回 datetime 之类的对象时不该让整个响应炸掉协议
    _OUT.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    _OUT.flush()


if __name__ == "__main__":
    main()
