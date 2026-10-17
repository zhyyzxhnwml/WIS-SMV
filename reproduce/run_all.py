# -*- coding: utf-8 -*-
"""论文实验一键复现编排器（两条主线：① 违规判断准确性 ② 级联是否又快又准）。

用法：
    python reproduce/run_all.py --list             # 看全部阶段与命令
    python reproduce/run_all.py --stage det        # 只跑确定性部分（不要 Ollama，约 15 分钟）
    python reproduce/run_all.py --stage llm        # 只跑大模型部分（需要 Ollama，约 1~2 小时）
    python reproduce/run_all.py --stage report     # 汇总出表出图
    python reproduce/run_all.py --stage check      # 与新基准快照比对
    python reproduce/run_all.py --stage all        # 全流程（det -> llm -> report -> check）
    python reproduce/run_all.py --stage all --dry-run

设计要点：
  · 每一步都是**独立进程**，可中断、可单独重跑；已完成的步骤产物存在就不必再跑。
  · 大模型相关脚本逐条落盘（data/logs/paper/llm_cache/*.jsonl），kill 掉重启同命令即续跑。
  · `cascade_pareto` / `rule_coverage` **复用已缓存的大模型判决**，零额外 token 成本。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = next(p for p in _HERE.parents if (p / "run.py").exists())

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

INJ = "data/logs/inject_v1"

STAGES = {
    "det": [
        ("生成注入集（240 场景 = 120 违规 / 120 合规）",
         ["inject_violations.py", "--scenes", "240", "--out", INJ]),
        ("我们的 L1 在注入集上（表 1 我们那一行）",
         ["eval_inject.py", "--gt", INJ + ".gt.json"]),
        ("8 个文献基线（表 1 其余各行；脚本被生产包依赖，留在 benchmark/）",
         ["benchmark/baselines.py", "--only", "all",
          "--out", "data/logs/baselines_inject.json"]),
        ("门槛敏感度扫描（表 4）",
         ["sweep_inject.py"]),
        ("L1 vs 独立参照实现对拍",
         ["compare_colreg.py"]),
    ],
    "llm": [
        ("9 个「谁来判」对照臂（表 3，实测 token，可续跑）",
         ["llm_arms.py", "--arms", "A", "B", "C", "D", "E", "F", "G", "H", "I"]),
        ("级联帕累托前沿（表 2 + 图 2，复用缓存，零额外调用）",
         ["cascade_pareto.py"]),
        ("规则覆盖消融（表 5，零额外调用）",
         ["rule_coverage.py"]),
        ("门控判据对比（优化文档 O1）",
         ["gate_design.py"]),
    ],
    "report": [
        ("McNemar + 配对自助 CI（论文/显著性检验.md）", ["stats_tests.py"]),
        ("汇总成论文表（论文/实验结果_汇总.md）", ["make_tables.py"]),
        ("出图（data/logs/paper/figures/）", ["figures.py"]),
        ("最终对比（论文/最终对比_我们的vs论文.md）", ["final_compare.py"]),
    ],
    "check": [
        ("与基准快照比对", None),  # 特殊：直接跑 check_results.py
    ],
}


def cmd_of(item) -> list[str]:
    """extra[0] 是脚本名：不含 "/" 表示在本包内，含 "/" 表示仓库根下的相对路径
    （例：`benchmark/baselines.py` —— 它被生产包依赖，留在原地）。"""
    name, extra = item
    if extra is None:
        return [sys.executable, str(_HERE.parent / "check_results.py")]
    script = extra[0]
    base = ROOT if "/" in script else _HERE.parent
    return [sys.executable, str(base / script)] + list(extra[1:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["det", "llm", "report", "check", "all"])
    ap.add_argument("--list", action="store_true", help="只打印阶段与命令")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不执行")
    ap.add_argument("--keep-going", action="store_true", help="某步失败也继续")
    args = ap.parse_args()

    order = ["det", "llm", "report", "check"] if args.stage == "all" else [args.stage]

    if args.list:
        for st in order:
            print(f"\n=== 阶段 {st} ===")
            for item in STAGES[st]:
                print("  " + item[0])
                print("    " + " ".join(cmd_of(item)))
        return 0

    print(f"仓库根：{ROOT}")
    total_fail = 0
    for st in order:
        print(f"\n{'=' * 70}\n阶段 {st}\n{'=' * 70}")
        for item in STAGES[st]:
            cmd = cmd_of(item)
            print(f"\n>>> {item[0]}\n    " + " ".join(cmd))
            if args.dry_run:
                continue
            t0 = time.time()
            r = subprocess.run(cmd, cwd=str(ROOT))
            dt = time.time() - t0
            status = "OK" if r.returncode == 0 else f"FAIL(rc={r.returncode})"
            print(f"    <- {status}  用时 {dt:.1f}s")
            if r.returncode != 0:
                total_fail += 1
                if st == "check" or not args.keep_going:
                    print("\n中止：用 --keep-going 可跳过失败步骤继续。")
                    return r.returncode or 1
    print("\n全流程结束。" if not total_fail else f"\n结束，{total_fail} 步失败。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
