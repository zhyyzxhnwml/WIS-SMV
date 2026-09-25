"""
三级级联架构收集器 —— 调用 run.py
===================================

通过 subprocess 独立运行项目根目录下的 run.py，
不导入任何 ais_accelerator/triage_engine/llm_layer 内部模块。

输出实时打印到终端，同时捕获 stdout/stderr。
"""
from __future__ import annotations

import subprocess
import time
import csv
import re
import sys
import os
from pathlib import Path


class CascadeCollector:
    """
    通过 subprocess 运行 run.py（三级级联架构的 CLI 入口）。
    """

    def __init__(self, project_dir: str = "."):  # project_dir: 项目根目录路径，默认当前目录
        self.project_dir = Path(project_dir).resolve()  # 项目根目录的绝对路径（Path对象）
        self.main_script = self.project_dir / "run.py"  # 级联架构CLI主入口脚本路径

        if not self.main_script.exists():
            raise FileNotFoundError(
                f"Cascade CLI 不存在: {self.main_script}\n"
                f"请确认在项目根目录运行"
            )

    @property
    def name(self) -> str:
        return "three-tier-cascade"  # 收集器标识名称

    def run_file(
        self,
        csv_path: str,  # 输入CSV文件路径
        output_dir: str,  # 输出目录路径
        max_ships: int = 0,  # 最大处理船舶数量，0表示不限
        timeout: int = 3600,  # 子进程超时时间（秒），默认3600秒
        workers: int = 4,  # 并发工作线程数，默认4
        no_llm: bool = False,  # 是否禁用LLM调用（仅使用规则引擎），默认False
    ) -> dict:
        """
        调用三级架构处理单个 CSV 文件。

        输出实时打印到终端，同时捕获完整输出。
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)  # 确保输出目录存在

        cmd = [  # 构建子进程命令列表
            "python", str(self.main_script),
            "--input", str(Path(csv_path).resolve()),
            "--output", str(Path(output_dir).resolve()),
            "--max-ships", str(max_ships),
            "--workers", str(workers),
        ]
        if no_llm:
            cmd.append("--no-llm")  # 追加禁用LLM的标志

        print(f"\n{'─'*50}")
        print(f"  [三级架构] 启动: {Path(csv_path).name} (最多 {max_ships} 艘)")
        print(f"  命令: {' '.join(cmd)}")
        print(f"{'─'*50}")

        start = time.perf_counter()  # 记录启动时间戳
        timed_out = False  # 是否超时标志
        stdout_lines = []  # 收集标准输出行列表
        stderr_lines = []  # 收集标准错误行列表

        try:
            # 强制子进程无缓冲输出
            env = dict(os.environ)  # 复制当前环境变量
            env["PYTHONUNBUFFERED"] = "1"  # 设置Python无缓冲输出环境变量

            process = subprocess.Popen(  # 启动子进程
                cmd,
                cwd=str(self.project_dir),  # 子进程工作目录
                stdout=subprocess.PIPE,  # 捕获标准输出
                stderr=subprocess.PIPE,  # 捕获标准错误
                env=env,  # 使用修改后的环境变量
            )

            # 实时读取 stdout（flush=True 保证立即显示）
            for line in iter(process.stdout.readline, b''):  # line: 逐行读取标准输出
                decoded = line.decode("utf-8", errors="replace").rstrip()  # decoded: 解码后的文本行
                if decoded:
                    print(f"  [Cascade] {decoded}", flush=True)
                    stdout_lines.append(decoded)

            # 实时读取 stderr
            for line in iter(process.stderr.readline, b''):  # line: 逐行读取标准错误
                decoded = line.decode("utf-8", errors="replace").rstrip()  # decoded: 解码后的错误文本行
                if decoded:
                    print(f"  [Cascade:err] {decoded}", file=sys.stderr, flush=True)
                    stderr_lines.append(decoded)

            process.wait(timeout=timeout)  # 等待子进程结束，最多等待timeout秒
            exit_code = process.returncode  # 子进程退出码

        except subprocess.TimeoutExpired:
            process.kill()  # 超时后强制终止子进程
            exit_code = -1  # 超时退出码设为-1
            timed_out = True  # 标记为已超时
            print(f"  [Cascade] ⚠ 超时（{timeout}s）")

        total_time = time.perf_counter() - start  # 计算总耗时

        output_files = sorted(  # 输出目录中所有结果CSV文件路径列表（已排序）
            str(f) for f in Path(output_dir).glob("results_*.csv")
        )

        ship_count = self._parse_ship_count("\n".join(stdout_lines))  # 从标准输出中解析处理的船舶数量
        routing_stats = self._parse_routing_stats("\n".join(stdout_lines))  # 从标准输出中解析路由分布统计

        print(f"\n  [Cascade] 完成: exit_code={exit_code}, "
              f"耗时={total_time:.1f}s, 船数={ship_count}")

        return {
            "system": self.name,  # 系统名称
            "exit_code": exit_code,  # 退出码
            "total_time": total_time,  # 总耗时（秒）
            "stdout": "\n".join(stdout_lines),  # 完整标准输出
            "stderr": "\n".join(stderr_lines),  # 完整标准错误
            "output_files": output_files,  # 输出文件路径列表
            "timed_out": timed_out,  # 是否超时
            "ship_count": ship_count,  # 处理船舶数量
            "routing_stats": routing_stats,  # 路由分布统计字典
            "error": None if exit_code == 0 else (stderr_lines[-1] if stderr_lines else "exit code != 0"),  # 错误信息，成功时为None
        }

    def _parse_ship_count(self, stdout: str) -> int:  # stdout: 要解析的标准输出文本
        for line in stdout.split("\n"):  # line: 逐行遍历输出文本
            m = re.search(r"共处理\s*(\d+)\s*条船舶", line)  # m: 匹配"共处理X条船舶"格式
            if m:
                return int(m.group(1))  # 返回捕获的船舶数量
        return 0  # 未匹配到任何格式时返回0

    def _parse_routing_stats(self, stdout: str) -> dict:  # stdout: 要解析的标准输出文本
        stats = {}  # 路由统计字典
        capturing = False  # 是否正在捕获路由分布数据的标志
        for line in stdout.split("\n"):  # line: 逐行遍历输出文本
            if "路由分布:" in line:
                capturing = True  # 遇到"路由分布:"标记开始捕获
                continue
            if capturing:
                m = re.match(r"\s*(\w+)\s+.*?(\d+)\s*\(", line)  # m: 匹配路由名称和数量
                if m:
                    stats[m.group(1)] = int(m.group(2))  # 将路由名称映射到数量
                elif not line.strip():
                    continue  # 空行跳过
                else:
                    break  # 非路由数据行则停止捕获
        return stats

    def parse_results_csv(self, output_dir: str) -> list[dict]:  # output_dir: 包含结果CSV文件的目录路径
        results = []  # 解析结果列表
        for csv_file in Path(output_dir).glob("results_*.csv"):  # csv_file: 遍历匹配的结果CSV文件
            with open(csv_file, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)  # reader: CSV字典读取器
                for row in reader:  # row: 逐行读取CSV记录
                    results.append(row)
        return results

    def count_llm_calls_from_stdout(self, stdout: str) -> int:  # stdout: 标准输出文本
        return stdout.count("[3/3] LLM 判定")  # 统计LLM判定标记出现次数

    def count_mock_indicators(self, stdout: str) -> int:  # stdout: 标准输出文本
        return stdout.count("Mock") + stdout.count("mock")  # 统计"Mock"或"mock"字符串出现次数