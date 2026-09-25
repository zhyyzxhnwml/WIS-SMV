"""
start.py — Web服务统一启动入口
================================

一键启动 Flask 后端 + 前端静态文件服务。
结合：三级架构智能体 + Web管理系统 + 文书上报系统

数据流：
  唯一管道 = run.py 的 main() 函数
  无论是 Web 上传还是 CLI，都完整运行 run.py：
  CSV → convert_csv → 加速器 → ML → LLM → 保存CSV + 写DB
  CascadeAgent 只做两件事：
  1. 调用 run.py 的 main()
  2. 把终端输出捕获到 cascade_logs 表（前端级联日志窗口可查看）

用法:
    python start.py                    # 启动（默认端口5000，开发模式）
    python start.py --port 8080         # 指定端口
    python start.py --prod              # 生产模式
    python start.py --cli               # CLI模式（运行三级架构全链路）
    python start.py --cli -i data/input # CLI模式指定输入目录

环境变量（数据库连接）:
    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
"""

import sys
import os
import io
import argparse

# Windows UTF-8 修复
if sys.platform == "win32":
    # 注意：不要用 io.TextIOWrapper 包 sys.stdout，因为后续 cascade_agent
    # 的 _OutputCapture 会替换 sys.stdout，TextIOWrapper 的 buffer 会被关闭导致
    # "I/O operation on closed file" 错误。改用 reconfigure 方式。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        # Python < 3.7 没有 reconfigure，fallback
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path


def start_web_server(port: int = 5000, debug: bool = True) -> None:
    """启动 Flask Web 服务。

    整合以下模块：
    1. Flask 后端 API（仪表盘/船舶监控/违规管理/文书上报/AI聊天）
    2. 三级架构引擎 — 通过 CascadeAgent 调用 run.py 的 main()
    3. Vue 前端静态文件服务

    Args:
        port: 服务端口号（默认 5000）
        debug: 是否启用调试模式（默认 True）
    """
    from frontend.frontend.backend.app import app

    print("=" * 60)
    print("  智海巡航 — 港口船舶违规行为智能识别平台")
    print("  ⚓ 三级架构智能体 + Web 管理系统")
    print("=" * 60)
    print()
    print(f"  ▶ Web服务地址: http://localhost:{port}")
    print(f"  ▶ API地址:     http://localhost:{port}/api/v1")
    print(f"  ▶ 调试模式:    {'开启' if debug else '关闭'}")
    print()
    print("  [数据流] 唯一管道 = run.py 的 main()")
    print("    CSV → convert_csv → 加速器 → ML → LLM → 保存CSV + 写DB")
    print("    CascadeAgent 捕获终端输出 → cascade_logs 表 → 前端级联日志窗口")
    print()
    print("  [系统模块]")
    print("    L1. ais_accelerator  — AIS数据加速器")
    print("    L2. triage_engine    — ML路由引擎")
    print("    L3. llm_layer        — LLM判定层")
    print("    ✅ memory/           — 智能体记忆系统")
    print("    ✅ run.py            — 唯一数据处理管道")
    print("=" * 60)
    print()

    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)


def run_pipeline(input_path: str = "data/input", max_ships: int = 0, no_llm: bool = False) -> None:
    """运行三级架构全链路（CLI模式）。

    使用 CascadeAgent（调用 run.py 的 main()），结果写入数据库，
    日志写入 cascade_logs 表，前端级联日志窗口可实时查看。

    Args:
        input_path: 输入CSV文件或目录路径
        max_ships: 最大处理船舶数（0=全部）
        no_llm: 是否跳过LLM层
    """
    from cascade_agent import CascadeAgent

    agent = CascadeAgent()

    input_dir = Path(input_path)
    if input_dir.is_file():
        agent.scan_csv(str(input_dir), max_ships=max_ships or None, user_id="cli")
    elif input_dir.is_dir():
        csv_files = sorted(input_dir.glob("*.csv"))
        if not csv_files:
            print(f"输入目录中没有 CSV 文件: {input_dir.resolve()}")
            return
        for f in csv_files:
            agent.scan_csv(str(f), max_ships=max_ships or None, user_id="cli")
    else:
        print(f"输入路径不存在: {input_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="智海巡航 — 统一启动入口")
    parser.add_argument("--port", "-p", type=int, default=5000, help="Web服务端口（默认 5000）")
    parser.add_argument("--prod", action="store_true", help="生产模式（关闭调试）")
    parser.add_argument("--cli", action="store_true", help="CLI模式（运行三级架构全链路）")
    parser.add_argument("--input", "-i", default="data/input", help="输入CSV路径（CLI模式用）")
    parser.add_argument("--max-ships", "-n", type=int, default=0, help="最大处理船舶数（CLI模式用）")
    parser.add_argument("--no-llm", action="store_true", help="跳过LLM层（CLI模式用）")

    args = parser.parse_args()

    if args.cli:
        run_pipeline(
            input_path=args.input,
            max_ships=args.max_ships,
            no_llm=args.no_llm,
        )
    else:
        start_web_server(
            port=args.port,
            debug=not args.prod,
        )
