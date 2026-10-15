# -*- coding: utf-8 -*-
"""子进程调度：常驻 worker、超时、预算、熔断计数。

【为什么是常驻 worker，不是「每条船 spawn 一次」】
  实测（Windows 开发机，Python 3.12）：
      冷启动 spawn + 跑完一次  ≈ 118 ms
      同一进程内再跑一次       ≈ 0.6~6 ms
  差值几乎全是解释器启动。500 条船 × 1 个插件，按「每船 spawn」是 59 秒起步；
  按「常驻 worker」是 118 ms + 500 次不到 1 秒。差一百倍，所以必须常驻。

【为什么 worker 按插件名复用一个池，而不是每次新建】
  子进程里 `_LOADED` 按 sha256(源码) 缓存已装载的插件，源码一变就 dispose 老的、
  重新 apply —— 也就是 dsh 的「热重载 = 卸载 + 重新 apply」。所以回滚版本、改一版
  再跑，都不需要换进程，子进程自己会重装。

【环境变量是这里最重要的一行代码】
  _scrubbed_env() 从白名单构造 env，而不是从 os.environ 拷贝再删。
  理由：删法会随部署新增的密钥变量而漏（今天是 PG_PASSWORD，明天可能是别的）；
  白名单法只会漏掉「忘了加」，而忘了加只会让插件**少**一个变量，不会多一个。
  这一层是真正扛事的保护 —— 子进程环境里没有 SECRET_KEY，llm_providers 的
  Fernet 密钥就派生不出来（llm_providers.py:93），落盘的密文对它就是废纸。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = "plugin_runtime.sandbox_runner"

# 一次 process_file 的插件总预算：超过就整体放弃插件判定，不拖垮流水线
DEFAULT_BUDGET_SECONDS = 120.0
DEFAULT_TIMEOUT_MS = 1500
HARD_MAX_TIMEOUT_MS = 3000
MAX_WORKERS = 4                      # 常驻子进程上限（每个约 15MB）
STDERR_KEEP_LINES = 40               # 子进程 stderr 只留最后几十行用于排障

# ── 允许传给子进程的环境变量（白名单，其余一律不传）──
_ENV_PASSTHROUGH = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PYTHONHASHSEED",
)
# 万一有人往白名单里加了带密钥的变量，这里再兜一次
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")


def _scrubbed_env() -> dict:
    env = {}
    for name in _ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    # 防御性兜底：真出现疑似密钥就丢掉并吭一声，别静默传下去
    for name in list(env):
        upper = name.upper()
        if any(h in upper for h in _SECRET_HINTS):
            env.pop(name, None)
            print(f"    ⚠ 子进程环境白名单里出现疑似密钥变量 {name}，已剔除")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"      # 别让子进程往仓库里写 __pycache__
    return env


class _Worker:
    """一个常驻子进程 + 两条读取线程。

    【为什么用线程读而不是 select】
      Windows 上 select 只管 socket，管不了管道，所以拿不到「读一行超时」。
      改成：守护线程一直 readline 塞进 Queue，主线程 Queue.get(timeout=...)。
      超时就 kill 进程，读线程随之拿到 EOF 退出。跨平台一致。
    """

    def __init__(self, name: str):
        self.name = name
        self.proc = None
        self.last_used = 0.0
        self._out_q: queue.Queue = queue.Queue()
        self._err_lines: list[str] = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ── 生命周期 ──
    def spawn(self) -> bool:
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-m", RUNNER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(REPO_ROOT), env=_scrubbed_env(),
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.proc = None
            self._err_lines.append(f"spawn 失败: {e}")
            return False
        self._threads = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for t in self._threads:
            t.start()
        return True

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                self._out_q.put(line)
        except Exception:
            pass
        self._out_q.put(None)        # EOF 哨兵：告诉等待方「进程没了」

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:
                self._err_lines.append(line.rstrip("\n"))
                if len(self._err_lines) > STDERR_KEEP_LINES:
                    del self._err_lines[0]
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.proc is None:
            return
        try:
            self.proc.kill()
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        self.proc = None

    # ── 一次调用 ──
    def call(self, payload: dict, timeout_s: float) -> dict:
        if not self.alive() and not self.spawn():
            return _err("spawn", "无法启动插件子进程：" + "; ".join(self._err_lines[-3:]))
        self.last_used = time.time()
        try:
            with self._lock:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
        except Exception as e:
            self.kill()
            return _err("pipe", f"写入子进程失败：{e}")

        try:
            line = self._out_q.get(timeout=timeout_s)
        except queue.Empty:
            # 超时：这个进程已经不可信（可能在死循环里），直接杀，下次重新 spawn
            tail = "; ".join(self._err_lines[-3:]) or "无 stderr 输出"
            self.kill()
            return _err("timeout", f"插件执行超过 {timeout_s * 1000:.0f}ms 被终止（{tail}）")

        if line is None:
            tail = "; ".join(self._err_lines[-3:]) or "无 stderr 输出"
            self.kill()
            return _err("crash", f"插件子进程异常退出（{tail}）")

        try:
            resp = json.loads(line)
        except Exception as e:
            self.kill()
            return _err("protocol", f"子进程返回的不是合法 JSON：{e}")
        resp.setdefault("ok", False)
        resp.setdefault("stage", "ok" if resp["ok"] else "unknown")
        resp.setdefault("findings", [])
        resp.setdefault("logs", [])
        resp.setdefault("tools", [])
        resp.setdefault("error", "")
        resp.setdefault("elapsed_ms", 0)
        return resp


def _err(stage: str, message: str) -> dict:
    return {"ok": False, "stage": stage, "findings": [], "logs": [], "tools": [],
            "error": message, "elapsed_ms": 0}


class Sandbox:
    """插件执行的门面。进程内单例（app 与 run.py 各持一份）。"""

    def __init__(self, budget_seconds: float = DEFAULT_BUDGET_SECONDS):
        self.budget_seconds = budget_seconds
        self._workers: dict[str, _Worker] = {}
        self._spent = 0.0
        self._calls = 0
        self._lock = threading.Lock()

    # ── 预算 ──
    def reset_budget(self):
        """每次 process_file 开头调一次。不重置的话第一批跑完，后面永远「预算耗尽」。"""
        self._spent = 0.0
        self._calls = 0

    def budget_left(self) -> float:
        return max(0.0, self.budget_seconds - self._spent)

    def stats(self) -> dict:
        return {"calls": self._calls, "spent_seconds": round(self._spent, 3),
                "budget_seconds": self.budget_seconds,
                "workers": sorted(self._workers)}

    # ── 执行 ──
    def run(self, plugin: dict, facts: dict, hook: str = "adjudicate") -> dict:
        """在子进程里跑一次插件。**永不抛异常** —— 插件失败不该影响主判定。"""
        name = str(plugin.get("name") or "")
        source = plugin.get("source") or ""
        if not name or not source:
            return _err("manifest", "插件缺少 name 或 source")

        timeout_ms = int(plugin.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        timeout_ms = max(100, min(HARD_MAX_TIMEOUT_MS, timeout_ms))
        # 超时上限还要受剩余预算约束：不然「预算剩 0.2 秒但插件要 3 秒」
        # 会让一批的收尾拖到预算之外
        timeout_s = min(timeout_ms / 1000.0, self.budget_left())
        if timeout_s <= 0.05:
            return _err("budget", f"本批插件预算已用尽（{self.budget_seconds:.0f}s）")

        payload = {"op": "run", "id": 1, "name": name, "source": source,
                   "config": plugin.get("config") or {},
                   "hooks": plugin.get("hooks") or [hook],
                   "hook": hook, "facts": facts}

        t0 = time.time()
        worker = self._acquire(name)
        try:
            resp = worker.call(payload, timeout_s)
        finally:
            self._spent += time.time() - t0
            self._calls += 1
        return resp

    def _acquire(self, name: str) -> _Worker:
        with self._lock:
            worker = self._workers.get(name)
            if worker is None:
                if len(self._workers) >= MAX_WORKERS:
                    self._evict_one()
                worker = _Worker(name)
                self._workers[name] = worker
            return worker

    def _evict_one(self):
        """淘汰最久没用的 worker。调用时已持锁。"""
        if not self._workers:
            return
        oldest = min(self._workers.items(), key=lambda kv: kv[1].last_used)[0]
        self._workers.pop(oldest).kill()

    def drop(self, name: str):
        """插件被停用/删除时调用：杀掉它的 worker，别让改过的代码留在内存里。"""
        with self._lock:
            worker = self._workers.pop(name, None)
        if worker:
            worker.kill()

    def shutdown(self):
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.kill()


_sandbox: Sandbox | None = None


def get_sandbox() -> Sandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox()
    return _sandbox
