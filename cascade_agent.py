"""
cascade_agent.py — 三级级联智能体（统一入口）
=============================================

【这个文件做什么？】
  全系统唯一的数据处理入口。无论是 CLI（run.py / start.py --cli）
  还是 Web 系统（start.py → Flask），都走 run.py 的 main() 函数。

  核心思路：run.py 就是主程序，CascadeAgent 只做两件事：
  1. 调用 run.py 的 main() 函数（完整运行 run.py）
  2. 把 run.py 的终端输出捕获，写入 cascade_logs 表（前端级联日志窗口可查看）

  不自己造管道，不引用 run.py 的内部函数拼装逻辑，
  就是完完整整地跑 run.py，只是把输出从终端搬到 Web 页面。

  每次调用会：
  → 完整运行 run.py（CSV → convert_csv → 加速器 → ML → LLM → 保存CSV）
  → 捕获 run.py 的终端输出，实时写入 cascade_logs 表
  → 检测结果由 run.py 写入 detection_results 表（run.py 已有此逻辑）
"""

import sys, os, io, json, time, logging, threading
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Optional

# ── 路径 ──
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ── 日志目录 ──
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── 日志 logger ──
_logger = logging.getLogger("cascade_agent")
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_DIR / "cascade_agent.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_logger.addHandler(_fh)


class CascadeAgent:
    """三级级联智能体——全系统唯一数据处理入口。

    核心思路：直接运行 run.py 的 main()，把终端输出搬到 cascade_logs 表。
    不自己造管道，不引用 run.py 的内部函数拼装逻辑。

    用法:
        from cascade_agent import CascadeAgent
        agent = CascadeAgent()
        result = agent.scan_csv("data/input/xxx.csv")
        print(result["summary"])
    """

    def __init__(self, model_name: str = "qwen2.5:7b-instruct-q4_K_M"):
        self.model_name = model_name
        self.logs: list[dict] = []
        self._started_at: Optional[datetime] = None

    @property
    def milvus(self):
        """代理 run.py 模块的 milvus 实例（兼容旧代码）。"""
        try:
            import run as _run_mod
            return _run_mod.milvus
        except Exception:
            return None

    # ── 日志写入 cascade_logs 表 ──

    def _write_log_to_db(self, stage: str, message: str, level: str = "INFO", user_id: str = "system"):
        """将一条日志写入 cascade_logs 表（携带 user_id，按用户隔离）。"""
        now = datetime.now()
        entry = {
            # 毫秒精度：级联阶段常在同一秒内密集打日志，只有秒级会导致前端看起来时间"卡住"
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}",
            "date": now.strftime("%Y-%m-%d"),
            "stage": stage,
            "level": level,
            "message": message,
            "user_id": user_id,
        }
        self.logs.append(entry)

        # 写入 log 文件
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = LOG_DIR / f"{today}_cascade.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{entry['timestamp']} [{stage:8s}] {message}\n")

        # 写入 logger
        log_level = getattr(logging, level, logging.INFO)
        _logger.log(log_level, "[%s] %s", stage, message)

        # 实时写入 PostgreSQL cascade_logs 表（含 user_id）
        try:
            import pg_db
            conn = pg_db.connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cascade_logs (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT DEFAULT 'system',
                    timestamp TEXT,
                    date TEXT,
                    stage TEXT,
                    level TEXT,
                    message TEXT
                )
            """)
            conn.execute(
                "INSERT INTO cascade_logs (user_id, timestamp, date, stage, level, message) VALUES (?,?,?,?,?,?)",
                (user_id, entry["timestamp"], entry["date"], entry["stage"], entry["level"], entry["message"]),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ── 终端输出捕获器 ──

    class _OutputCapture:
        """捕获 run.py 的 print 输出，同时写入 cascade_logs 表。

        核心策略：完全接管 sys.stdout 和 sys.stderr，用自定义的 write/flush 对象替换。
        原始 stdout 的引用保存在 _real_stdout，用于在安全时输出到终端。
        """

        def __init__(self, agent, user_id="system"):
            self.agent = agent
            self.user_id = user_id
            self._buffer = ""
            self._real_stdout = None  # 保存原始 stdout 对象的引用
            self._real_stderr = None

        def write(self, text):
            """被 print() 调用的 write 方法。"""
            # 尝试写原始终端（安全地）
            if self._real_stdout is not None:
                try:
                    self._real_stdout.write(text)
                    self._real_stdout.flush()
                except (ValueError, AttributeError):
                    pass  # 原始 stdout 已关闭或不可用，静默忽略

            if not text:
                return

            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.strip()
                if line:
                    stage = self._parse_stage(line)
                    self.agent._write_log_to_db(stage, line, user_id=self.user_id)

        def flush(self):
            """被 print() 调用的 flush 方法。"""
            if self._real_stdout is not None:
                try:
                    self._real_stdout.flush()
                except (ValueError, AttributeError):
                    pass

        @staticmethod
        def _parse_stage(line: str) -> str:
            """从输出行中推断日志阶段。"""
            if any(kw in line for kw in ["初始化", "训练", "模型"]):
                return "init"
            elif any(kw in line for kw in ["加速器", "[1/3]", "载入", "转换"]):
                return "l1"
            elif any(kw in line for kw in ["ML Triage", "[2/3]", "路由", "分类", "置信度"]):
                return "l2"
            elif any(kw in line for kw in ["LLM", "[3/3]", "判定", "模板"]):
                return "l3"
            elif any(kw in line for kw in ["违规", "正常", "结果", "✅", "完成"]):
                return "result"
            elif any(kw in line for kw in ["错误", "失败", "❌"]):
                return "error"
            elif any(kw in line for kw in ["扫描", "文件", "发现"]):
                return "scan"
            else:
                return "info"

        def __enter__(self):
            # 保存原始 stdout/stderr 对象引用
            self._real_stdout = sys.stdout
            self._real_stderr = sys.stderr
            # 用自身替换 sys.stdout（自身有 write/flush 方法）
            sys.stdout = self
            sys.stderr = self
            return self

        def __exit__(self, *args):
            # 刷出缓冲区剩余内容
            if self._buffer.strip():
                stage = self._parse_stage(self._buffer.strip())
                self.agent._write_log_to_db(stage, self._buffer.strip(), user_id=self.user_id)
                self._buffer = ""
            # 恢复原始 stdout/stderr
            sys.stdout = self._real_stdout
            sys.stderr = self._real_stderr

    # ── 唯一核心方法：直接运行 run.py ──

    def scan_csv(self, csv_path: str, max_ships: Optional[int] = None, user_id: str = "system") -> dict:
        """扫描单个 CSV 文件——直接运行 run.py 的 main()。

        就是完完整整地跑 run.py，只是把终端输出从终端搬到 Web 页面。
        run.py 自己会做：CSV → convert_csv → 加速器 → ML → LLM → 保存CSV + 写DB

        Args:
            csv_path: CSV 文件路径
            max_ships: 最大处理船舶数
            user_id: 关联用户 ID

        Returns:
            {"summary": {...}, "results": [...], "logs": [...]}
        """
        import run as _run_module

        self._started_at = datetime.now()
        self.logs = []
        self._write_log_to_db("scan", f"开始扫描文件: {Path(csv_path).name}", user_id=user_id)

        # 保存 run.py 原始的 args，运行完后恢复
        original_argv = sys.argv.copy()

        try:
            # 构造 run.py 的命令行参数
            run_argv = ["run.py", "--input", csv_path, "--output", "data/output"]
            if max_ships:
                run_argv.extend(["--max-ships", str(max_ships)])
            sys.argv = run_argv

            # 重新解析 run.py 的 args（因为 run.py 在模块级别解析了 argparse）
            _run_module.args = _run_module.parser.parse_args(run_argv[1:])
            _run_module.INPUT = _run_module.args.input
            _run_module.OUTPUT = _run_module.args.output
            _run_module.MAX_SHIPS = _run_module.args.max_ships if _run_module.args.max_ships > 0 else None

            # 数据归属：写入 detection_results 的 user_id 用上传者
            _run_module.CURRENT_USER_ID = user_id

            # 先启动输出捕获，再初始化 run.py 组件
            # 这样 MilvusClient.__init__ 里的 print() 也会被捕获
            with self._OutputCapture(self, user_id=user_id):
                _run_module._ensure_initialized()
                _run_module.main()

        except RuntimeError as e:
            # LLM 模型不可用，记录日志并返回
            self._write_log_to_db("error", f"LLM 模型不可用: {e}", user_id=user_id)
            self._write_log_to_db("error", "级联检测暂停，请启动 Ollama 并下载模型后重试", user_id=user_id)
        except Exception as e:
            self._write_log_to_db("error", f"运行 run.py 失败: {e}", user_id=user_id)
            import traceback
            self._write_log_to_db("error", traceback.format_exc()[:500], user_id=user_id)
        finally:
            sys.argv = original_argv

        # 从 detection_results 表读取本次运行的结果（按该用户统计）
        summary = self._read_summary_from_db(user_id)
        self._write_log_to_db("done", f"完成！{summary.get('total', 0)} 艘, {summary.get('violations', 0)} 艘违规", user_id=user_id)

        return {"summary": summary, "results": [], "logs": self.logs}

    def _read_summary_from_db(self, user_id: str = "system") -> dict:
        """从 detection_results 表读取该用户（'system'=全局）的最新检测结果汇总。"""
        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            total = conn.execute(f"SELECT COUNT(*) as c FROM detection_results{(' WHERE ' + scope_sql) if scope_sql else ''}", scope_params).fetchone()["c"]
            violations = conn.execute(f"SELECT COUNT(*) as c FROM detection_results WHERE violation_detected = 1{(' AND ' + scope_sql) if scope_sql else ''}", scope_params).fetchone()["c"]
            routes = {}
            for row in conn.execute(f"SELECT routing, COUNT(*) as c FROM detection_results WHERE routing != ''{(' AND ' + scope_sql) if scope_sql else ''} GROUP BY routing", scope_params):
                routes[row["routing"]] = row["c"]
            conn.close()
            return {
                "total": total,
                "violations": violations,
                "normal": total - violations,
                "routes": routes,
                "duration_seconds": round((datetime.now() - self._started_at).total_seconds(), 2) if self._started_at else 0,
            }
        except Exception:
            return {"total": 0, "violations": 0, "normal": 0, "routes": {}, "duration_seconds": 0}


# ── 快捷入口 ──

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="级联智能体")
    ap.add_argument("--csv", help="扫描 CSV 文件")
    args = ap.parse_args()

    agent = CascadeAgent()

    if args.csv:
        result = agent.scan_csv(args.csv)
        print(f"\n{'='*50}")
        print(f"  ✅ 完成: {result['summary']['total']} 艘, {result['summary']['violations']} 违规")
    else:
        ap.print_help()
