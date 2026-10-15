"""
scripts/refresh_l1_violations.py — 用重算后的 L1 证据刷新存量记录的违规分类
================================================================================

【它解决什么问题】
  违规分类的唯一依据是 L1（加速器层）的实测事件。2026-10-10 补了三个单船/多船
  检测器（违规追越 / 逆向航行 / 越界锚泊）并修好了多船接线（碰撞风险复活），
  但库里 617 行的 `detailed_result.events` 是**旧管道**写的——那时既没有这三个
  检测器，多船规则也因为传了空 dict 从未运行过。所以直接跑
  `backfill_violation_taxonomy.py` 只会把旧事件重新分一遍，**分不出新类型**。

  本脚本把 L1 那一层**重新算一遍**（确定性、无 LLM 成本），再按
  `violation_catalog.classify()` 重物化分类列：

      violation_detected   L1 硬证据可立违规时置 1（与 orchestrator 的提升同口径）
      violation_code       二级规范码
      violation_subtype    三级细类
      violation_metric     定档实测量值
      violation_label      中文标签
      detailed_result      其中的 events 数组换成重算后的实测事件（证据面板读它）

【为什么不重跑整条分析流程】
  整条流程的时间全在第三层 LLM 上：实测 fzais1009 是 529 船、每船中位 27.8 秒，
  串行约 4 小时（`--workers` 只是解析了参数、从未真的并行）。而分类结果由
  **L1 侧决定**（`classify()` 的优先级就是 L1 事件的 candidate_type 高于 LLM 的
  violation_type），重跑 4 小时换来的是同一批分类列 + 一份重新生成的 LLM 结语。
  所以这里只重算 L1 与分类列，**LLM 的产出不动**：

      violation_type / confidence / recommended_action / risk_score
      / llm_latency_seconds  —— 一律保持库里的原值

  唯一被写的 LLM 侧字段是 final_assessment，且只在 L1 提升成立时**前置一行**
  说明（`[确定性检测] ...`）——这与 `orchestrator.py` 真实运行时的写法逐字一致，
  否则界面上会出现"违规成立但结语说没问题"的自相矛盾行。

【幂等】
  重复跑结果相同（重算的事件与旧事件同源同法）；final_assessment 的前置说明
  带前缀判重，不会叠加。

用法：
  python scripts/refresh_l1_violations.py --source data/input/_fzais1009_converted.csv \
      --user-id 0df2eb08 --dry-run
  python scripts/refresh_l1_violations.py --source ... --user-id 0df2eb08
"""
# -*- coding: utf-8 -*-
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pg_db  # noqa: E402
from ais_accelerator.config import AcceleratorConfig  # noqa: E402
from ais_accelerator.pipeline import AcceleratorPipeline  # noqa: E402
from ais_accelerator.violation_catalog import (  # noqa: E402
    CANONICAL, FALLBACK_REG, build_violation_event, classify,
    deterministic_violation, format_article_ref,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NOTE_PREFIX = "[确定性检测]"  # 与 orchestrator.py 的提升说明同前缀，用于判重


def label_of(code: str) -> str:
    return (CANONICAL.get(code) or {}).get("label", code)


def refresh_one(annotated, row) -> dict:
    """一条记录的重算结果 —— 只算，不写库。

    Returns: {"detected", "code", "subtype", "metric", "label", "events", "note"}
    """
    events = [ev.model_dump(mode="json") for ev in (annotated.events or [])]

    detected = bool(row["violation_detected"])
    note = ""
    if not detected:
        # 与 orchestrator 的 L1 提升同口径：LLM 说没问题，但硬证据立得住就立
        hard = deterministic_violation(annotated.events or [])
        if hard:
            detected = True
            metric_txt = f"，{hard['feature']}={hard['value']:.2f}" if hard["feature"] else ""
            note = (f"{NOTE_PREFIX} L1 实测命中「{hard['code']}」"
                    f"（{hard['count']} 项硬证据{metric_txt}），未经 LLM 复核。")

    if not detected:
        return {"detected": 0, "code": "", "subtype": "", "metric": 0.0,
                "label": row["violation_label"] or "", "events": events, "note": ""}

    info = classify(row["violation_type"], events)
    return {
        "detected": 1,
        "code": info["code"],
        "subtype": info["subtype"] or "",
        "metric": float(info["metric"]) if info["metric"] is not None else 0.0,
        "label": info["label"],
        "events": events,
        "note": note,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="用重算的 L1 证据刷新违规分类列")
    ap.add_argument("--source", required=True, help="转换后的 CSV（pipeline.store.ingest 的入参）")
    ap.add_argument("--user-id", required=True, help="只刷这个用户的记录")
    ap.add_argument("--dry-run", action="store_true", help="只打印变更前后，不写库")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 船（调试用）")
    args = ap.parse_args()

    config = AcceleratorConfig()
    pipeline = AcceleratorPipeline(config=config)

    t0 = time.time()
    stats = pipeline.store.ingest(args.source)
    print(f"[ingest] ships={stats.ship_count} rows={stats.valid_rows} "
          f"reused={stats.reused_cache} {time.time()-t0:.1f}s", flush=True)

    conn = pg_db.connect()
    rows = {r["mmsi"]: r for r in conn.execute(
        "SELECT id, mmsi, violation_detected, violation_type, violation_label, "
        "violation_code, risk_level, final_assessment, regulation_ref "
        "FROM detection_results WHERE user_id = ?", (args.user_id,)).fetchall()}
    print(f"[db] {len(rows)} rows for user={args.user_id}\n", flush=True)
    if not rows:
        conn.close()
        return 1

    regdb = load_regdb()
    print(f"[regdb] {'Milvus 可用，法规检索走向量库' if regdb else 'Milvus 不可用，法规引用退回目录兜底文本'}")

    t0 = time.time()
    fleet = pipeline.build_fleet_context()
    print(f"[context] {time.time()-t0:.1f}s  water={len(fleet.water.corridor) if fleet.water else 0} "
          f"overtaking={sum(len(v) for v in fleet.overtakings.values())} "
          f"give_way={sum(len(v) for v in fleet.give_ways.values())} "
          f"towing={sum(len(v) for v in fleet.towings.values())} "
          f"on {len(fleet.overtakings)} ships in {fleet.pairwise_seconds:.1f}s\n", flush=True)

    before = Counter()
    after = Counter()
    promoted = 0
    changed = 0
    missing = 0
    done = 0
    errors = []

    print(f"{'mmsi':<10} {'旧码':<22} {'新码':<22} {'新细类':<26} {'实测量值':>10}  {'提升':<4}")
    print("-" * 108)

    for ship, raw_points in pipeline.store.iter_ship_tracks():
        row = rows.get(ship.mmsi)
        if row is None:  # 库里没有这条船（不是本批分析的）——不碰
            missing += 1
            continue
        try:
            annotated = pipeline.annotate_track(
                ship, raw_points,
                water=fleet.water,
                field=fleet.field,
                nearby=fleet.nearby_for(raw_points, ship.mmsi, config),
                extra_events=fleet.events_for(ship.mmsi),
            )
        except Exception as e:
            errors.append(f"{ship.mmsi}: {type(e).__name__}: {e}")
            continue

        res = refresh_one(annotated, row)
        done += 1

        old_code = (row["violation_code"] or "") or ("(空)" if not row["violation_detected"] else "")
        before[old_code or "(空)"] += 1
        after[res["code"] or "(空)"] += 1
        if res["detected"] and not row["violation_detected"]:
            promoted += 1
        if res["code"] != (row["violation_code"] or ""):
            changed += 1

        if row["violation_detected"] or res["detected"]:
            m = f"{res['metric']:.1f}" if res["metric"] else "-"
            print(f"{ship.mmsi:<10} {old_code:<22} {res['code']:<22} "
                  f"{res['subtype'] or '-':<26} {m:>10}  {'★' if res['note'] else '':<4}", flush=True)

        if args.dry_run:
            if args.limit and done >= args.limit:
                break
            continue

        # ── 写库：只动分类列 + events；LLM 列保持原值 ──
        try:
            dr = json.loads(row_dr(conn, row["id"]) or "{}")
        except Exception:
            dr = {}
        dr["events"] = res["events"]
        dr["violation_class"] = {"code": res["code"], "subtype": res["subtype"],
                                 "label": res["label"], "metric": res["metric"],
                                 "source": "l1_refresh"}

        sets = ["violation_detected=?", "violation_code=?", "violation_subtype=?",
                "violation_metric=?", "violation_label=?", "detailed_result=?"]
        vals = [res["detected"], res["code"], res["subtype"], res["metric"],
                res["label"], json.dumps(dr, ensure_ascii=False)]

        if res["note"]:
            prev = (row["final_assessment"] or "").strip()
            if not prev.startswith(NOTE_PREFIX):  # 幂等：不叠加
                sets.append("final_assessment=?")
                vals.append((f"{res['note']} {prev}" if prev else res["note"])[:500])
            # 词表是 "mid" 不是 "middle"：risk_level 全链路用 llm_layer/handler.py 与
            # risk_engine.py 的 {high, mid, low}；"middle" 是事件优先级
            # （models.py EventPriority）的词表，只是恰好同名。与 orchestrator.py
            # 的提升分支保持逐字一致。
            if (row["risk_level"] or "").lower() not in {"mid", "medium", "high"}:
                sets.append("risk_level=?")
                vals.append("mid")
        if res["code"] != (row["violation_code"] or ""):
            # 规范码换了，法规引用就得跟着换 —— 否则界面上会出现
            #「违规追越」配着「AIS 信号缺失」那条法规。检索口径与 orchestrator
            # 一致：先用规范码检索，为空则退回 FALLBACK_REG（那是"该违规适用什么
            # 规则"的通用陈述，不是伪造条号；orchestrator 真实写入路径就是这么写的）。
            new_ref, best = reg_ref(res["code"], regdb)
            new_event = build_violation_event(
                res["code"], row["final_assessment"] or "", new_ref, best)
            sets += ["regulation_ref=?", "violation_event=?"]
            vals += [new_ref, new_event]

        conn.execute(f"UPDATE detection_results SET {', '.join(sets)} WHERE id=?",
                     vals + [row["id"]])
        conn.commit()

        if args.limit and done >= args.limit:
            break

    if not args.dry_run:
        conn.commit()
    conn.close()

    print("\n" + "=" * 108)
    print(f"处理 {done} 船；库中无对应记录（跳过）{missing}；分类码变化 {changed}；L1 提升为违规 {promoted}")
    if errors:
        print(f"!! {len(errors)} 船抛出异常（run.py 裸调同一处，真实运行会崩）：")
        for e in errors[:10]:
            print(f"   {e}")
    print(f"\n变更前 规范码分布: {dict(sorted(before.items(), key=lambda kv: -kv[1]))}")
    print(f"变更后 规范码分布: {dict(sorted(after.items(), key=lambda kv: -kv[1]))}")
    print(f"变更后非空类型数: {len([k for k in after if k not in ('(空)', 'unknown')])}")
    if args.dry_run:
        print("（dry-run，未写库）")
    return 0


def load_regdb():
    """法规检索器；Milvus 不可用返回 None（此时只用 FALLBACK_REG，与生产一致）。"""
    try:
        from ais_accelerator.regulation_db import RegulationDB
        db = RegulationDB()
        if db._available and db._has_collection():
            return db
    except Exception:
        pass
    return None


def reg_ref(code: str, regdb):
    """规范码 → (引用文本, 最相关条目)。检索口径逐字对齐 orchestrator.py:420-437。"""
    arts = []
    if regdb is not None:
        try:
            arts = regdb.search_by_violation(code, None, top_k=3)
        except Exception:
            arts = []
    if arts:
        return "；".join(format_article_ref(a) for a in arts[:3]), arts[0]
    return FALLBACK_REG.get(code, FALLBACK_REG.get("unknown", "")), None


def row_dr(conn, row_id: int) -> str:
    """单独取一行的 detailed_result（平均 206 kB / 最大 1.2 MB，不整表载入）。"""
    r = conn.execute("SELECT detailed_result FROM detection_results WHERE id=?", (row_id,)).fetchone()
    return (r["detailed_result"] if r else "") or ""


if __name__ == "__main__":
    sys.exit(main())
