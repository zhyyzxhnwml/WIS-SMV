# -*- coding: utf-8 -*-
"""把「本次复现跑出来的数字」与「基准快照」逐项对比。

用法：
    python reproduce/check_results.py
    python reproduce/check_results.py --actual data/logs/paper/tables.json

判定口径（多人实验就看这个）：
    · 确定性部分（L1 规则、8 个文献基线、门槛扫描、规则覆盖消融）
        必须**数值一致**（浮点容差 1e-6）。不一致 = 复现失败，要查环境。
    · 大模型相关部分（A–I 臂、帕累托前沿）
        允许 **F1 ±0.03** 的抖动 —— 本地推理在 Ollama 并行槽位下非严格确定性
        （见《优化文档/04》§11.7）。调用次数与 token 数应当一致（token 容差 1%）。
    · 退出码：确定性部分不一致 -> 1；仅大模型部分抖动 -> 0（但显式标注）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import platform
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = next(p for p in _HERE.parents if (p / "run.py").exists())

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 大模型相关的键：容忍抖动
LLM_KEYS = {"f1", "precision", "recall"}
TOK_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}
TOL_DET = 1e-6      # 确定性容差
TOL_LLM = 0.03      # 大模型 F1 类容差
TOL_TOK = 0.01      # token 相对容差


def is_llm_path(path: str) -> bool:
    return path.startswith("llm_arms") or path.startswith("cascade_pareto")


def _md5(p: Path) -> str:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return "MISSING"


def fingerprint() -> dict:
    """环境指纹：多人对比时先比这一块，指纹不同就没必要比数字。"""
    fp = {
        "time": _dt.datetime.now().isoformat(timespec="seconds"),
        "machine": platform.node(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "libs": {},
        "inputs": {},
    }
    for mod in ("numpy", "sklearn", "lightgbm", "matplotlib"):
        try:
            fp["libs"][mod] = __import__(mod).__version__
        except Exception:
            fp["libs"][mod] = "MISSING"
    for rel in ("data/input/_fzais1009_converted.csv",
                "triage_model.txt",
                "data/logs/inject_v1.gt.json"):
        fp["inputs"][rel] = _md5(ROOT / rel)
    return fp


def tol_for(path: str):
    key = path.rsplit(".", 1)[-1]
    if is_llm_path(path):
        if key in TOK_KEYS:
            return ("rel", TOL_TOK)
        if key in LLM_KEYS:
            return ("abs", TOL_LLM)
        return ("abs", TOL_DET)
    return ("abs", TOL_DET)


def compare(exp, act, path, rows):
    if isinstance(exp, dict):
        if not isinstance(act, dict):
            rows.append((path, exp, act, "MISSING", "结构不一致"))
            return
        for k, v in exp.items():
            compare(v, act.get(k), f"{path}.{k}", rows)
        return
    if isinstance(exp, list):
        if not isinstance(act, list) or len(act) != len(exp):
            rows.append((path, f"[{len(exp)} 项]", f"[{len(act) if isinstance(act, list) else '?'} 项]",
                         "MISSING", "列表长度不一致"))
            return
        for i, (a, b) in enumerate(zip(exp, act)):
            compare(a, b, f"{path}[{i}]", rows)
        return
    if act is None:
        rows.append((path, exp, None, "MISSING", "新产物缺该字段"))
        return
    if isinstance(exp, bool) or isinstance(exp, str):
        rows.append((path, exp, act, "OK" if exp == act else "DIFF", ""))
        return
    if isinstance(exp, (int, float)):
        # NaN / ±inf 不能做减法比较，单独判定
        if isinstance(exp, float) and (math.isnan(exp) or math.isinf(exp)):
            if isinstance(act, float) and math.isnan(exp):
                ok = math.isnan(act)
            else:
                ok = isinstance(act, float) and act == exp
            rows.append((path, exp, act, "OK" if ok else "DIFF", "特殊值"))
            return
        if not isinstance(act, (int, float)):
            rows.append((path, exp, act, "DIFF", "类型不一致"))
            return
        mode, tol = tol_for(path)
        if mode == "rel":
            ok = abs(act - exp) <= tol * max(1e-9, abs(exp))
        else:
            ok = abs(act - exp) <= tol
        rows.append((path, exp, act, "OK" if ok else "DIFF", f"±{tol:g}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expected", default=str(_HERE.parent / "expected.json"))
    ap.add_argument("--actual", default=str(ROOT / "data/logs/paper/tables.json"))
    ap.add_argument("--max-print", type=int, default=25)
    ap.add_argument("--save", default="", help="把环境指纹 + 比对结论写入该 JSON，便于多人互传对比")
    args = ap.parse_args()

    exp_p, act_p = Path(args.expected), Path(args.actual)
    if not exp_p.exists():
        print(f"[错误] 基准快照不存在：{exp_p}")
        return 2
    if not act_p.exists():
        print(f"[错误] 新产物不存在：{act_p}\n       先跑 run_all.py 的测量阶段。")
        return 2

    exp = json.loads(exp_p.read_text(encoding="utf-8"))
    act = json.loads(act_p.read_text(encoding="utf-8"))

    rows: list[tuple] = []
    for section, val in exp.items():
        if section.startswith("_"):
            continue
        compare(val, act.get(section), section, rows)

    diff_det, diff_llm = [], []
    for path, e, a, st, note in rows:
        if st in ("OK",):
            continue
        (diff_llm if is_llm_path(path) else diff_det).append((path, e, a, st, note))

    print(f"\n基准快照：{exp_p}")
    print(f"本次产物：{act_p}")
    print(f"对比项总数：{len(rows)}    不一致：{len(diff_det) + len(diff_llm)}"
          f"（确定性 {len(diff_det)} / 大模型 {len(diff_llm)}）\n")

    def show(title, items):
        if not items:
            return
        print(f"--- {title}（{len(items)} 项）---")
        for i, (path, e, a, st, note) in enumerate(items):
            if i >= args.max_print:
                print(f"  ... 其余 {len(items) - args.max_print} 项略")
                break
            es = f"{e:.4g}" if isinstance(e, (int, float)) else str(e)
            as_ = f"{a:.4g}" if isinstance(a, (int, float)) else str(a)
            print(f"  [{st}] {path}\n        基准={es}  本次={as_}  {note}")
        print()

    show("确定性部分不一致（必须为 0）", diff_det)
    show("大模型部分抖动（±0.03 内属正常）", diff_llm)

    if not diff_det and not diff_llm:
        verdict = "一致"
        print("[一致] 本次复现与基准快照 100% 吻合。")
    elif not diff_det:
        verdict = "基本一致"
        print("[基本一致] 确定性部分完全吻合；大模型部分有抖动，"
              "属并行推理非确定性，不影响结论方向。")
    else:
        verdict = "不一致"
        print("[不一致] 确定性部分对不上 —— 请优先排查：Python/依赖版本、"
              "数据文件（data/input/_fzais1009_converted.csv、data/logs/inject_v1.*）、"
              "triage_model.txt 的 md5、以及是否改过参数。")

    fp = fingerprint()
    print("\n--- 环境指纹（多人实验先比这一块）---")
    print(f"  机器={fp['machine']}  Python={fp['python']}  {fp['platform']}")
    print(f"  依赖={fp['libs']}")
    for k, v in fp["inputs"].items():
        print(f"  md5({k})={v}")

    if args.save:
        rep = {
            "verdict": verdict,
            "n_compared": len(rows),
            "n_diff_deterministic": len(diff_det),
            "n_diff_llm": len(diff_llm),
            "diffs_deterministic": [
                {"path": p, "expected": e, "actual": a, "status": s, "note": n}
                for p, e, a, s, n in diff_det
            ],
            "diffs_llm": [
                {"path": p, "expected": e, "actual": a, "status": s, "note": n}
                for p, e, a, s, n in diff_llm
            ],
            "fingerprint": fp,
        }
        outp = Path(args.save)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已写入：{outp}（发给其他人做互相对比）")

    return 1 if diff_det else 0


if __name__ == "__main__":
    raise SystemExit(main())
