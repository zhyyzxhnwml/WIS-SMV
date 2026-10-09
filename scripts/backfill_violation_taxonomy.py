"""
scripts/backfill_violation_taxonomy.py — 存量违规记录的"三级分类"回填
=====================================================================

【它解决什么问题】
  违规监管页的"违规类型"此前只有两个值（AIS信号异常/关闭、其他违规）。根因是
  分类的输入取自 LLM 自由输出的 violation_type 列，而 L1 已经实测到了真正的
  证据（48 个事件 100% 是 ais_gap）。本脚本按 violation_catalog.classify()
  重算每条违规记录的三级分类，并物化成列：

    violation_code     二级规范码    如 ais_gap
    violation_subtype  三级细类      如 ais_gap#suspected_off
    violation_metric   定档实测量值  如 680.32
    violation_label    中文标签      （一并按新口径刷新）

  物化成列而不是每次请求解析 JSON：detailed_result 平均 206 kB / 最大 1.2 MB，
  全表 124 MB，接口层为了取 L1 事件类型去解析它会拖垮 /violations 和 /catalog。

【为什么不复用 backfill_violations.py】
  那个脚本第 78-79 行在法规检索为空时**无条件覆盖 regulation_ref 为兜底文本**，
  且第 57 行拿 LLM 的脏 violation_type 去检索。现在再跑一次会把仅存的记录也冲掉。
  那个脚本已加警告、不再使用。

  本脚本**默认完全不碰 regulation_ref**；只有显式传 --fix-regulation 才重建，
  且要求 Milvus 可用、相似度过 --min-similarity 门槛，**低于门槛宁可留空也不写
  兜底文本**——宁可让页面显示"-"，也不写一条看起来像检索结果的假法规。

用法：
  python scripts/backfill_violation_taxonomy.py --dry-run        # 只看变更，不写库
  python scripts/backfill_violation_taxonomy.py                  # 只回填三级分类
  python scripts/backfill_violation_taxonomy.py --fix-regulation  # 连法规引用一起重算
  python scripts/backfill_violation_taxonomy.py --only 34        # 只处理某一条
"""
# -*- coding: utf-8 -*-
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pg_db  # noqa: E402
from ais_accelerator.violation_catalog import classify, build_violation_event  # noqa: E402

# Windows 控制台默认 GBK，打印中文标签会炸
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 新增列（app.py 的 _init_db_tables 里也有同样三条，保证全新部署不缺列）
NEW_COLUMNS = [
    ("violation_code", "TEXT DEFAULT ''"),
    ("violation_subtype", "TEXT DEFAULT ''"),
    ("violation_metric", "DOUBLE PRECISION DEFAULT 0"),
]


def ensure_columns(conn) -> None:
    """幂等建列 —— 容器重建后的新库也要有。"""
    for name, decl in NEW_COLUMNS:
        conn.execute(f"ALTER TABLE detection_results ADD COLUMN IF NOT EXISTS {name} {decl}")
    conn.commit()


def parse_events(detailed_result) -> list:
    """从 detailed_result 里取 L1 事件列表；解析失败按空处理（退回 LLM 口径）。"""
    try:
        detail = json.loads(detailed_result or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    return detail.get("events") or []


def load_regdb():
    """法规检索器；Milvus 不可用时返回 None（此时 --fix-regulation 会拒绝执行）。"""
    try:
        from ais_accelerator.regulation_db import RegulationDB
        db = RegulationDB()
        if db._available and db._has_collection():
            return db
    except Exception as e:
        print("  RegulationDB 初始化失败:", e)
    return None

def format_ref(arts: list) -> str:
    """法规条目列表 → 引用文本，形如「《法规名》第36条（正文首句…）相关度68%」。

    格式统一交给 violation_catalog.format_article_ref（orchestrator 写入路径用的是
    同一个函数），避免两条路径各写各的、界面文案漂移。
    """
    from ais_accelerator.violation_catalog import format_article_ref
    return "；".join(format_article_ref(a) for a in arts[:3])


def main() -> int:
    ap = argparse.ArgumentParser(description="存量违规记录三级分类回填")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印变更前后对照，不写库")
    ap.add_argument("--fix-regulation", action="store_true",
                    help="同时重建 regulation_ref / violation_event（需 Milvus 可用）")
    ap.add_argument("--min-similarity", type=float, default=None,
                    help="法规相似度门槛，低于它的结果丢弃"
                         "（默认取 regulation_db.MIN_SIMILARITY）")
    ap.add_argument("--only", type=int, default=0,
                    help="只处理指定 id（调试用）")
    args = ap.parse_args()

    if args.min_similarity is None:
        try:
            from ais_accelerator.regulation_db import MIN_SIMILARITY
            args.min_similarity = MIN_SIMILARITY
        except Exception:
            args.min_similarity = 0.45

    conn = pg_db.connect()
    if not args.dry_run:
        ensure_columns(conn)

    regdb = None
    if args.fix_regulation:
        if args.dry_run:
            print("--dry-run 下不重建法规引用（只预演三级分类）\n")
        else:
            regdb = load_regdb()
            if regdb is None:
                print("!! Milvus 法规库不可用，拒绝执行 --fix-regulation")
                print("   （法规引用会被清空或写错，宁可不跑）")
                conn.close()
                return 2
            # 向量化退化守卫：哈希伪向量与真 nomic 向量的余弦只有 0.01~0.03，
            # 那会让**每一条**结果都低于相似度门槛、42 行的 regulation_ref 被全部
            # 刷成空（实测主机上 OLLAMA_HOST 指错端口就是这个后果，且是静默的）。
            # get_embedding 走一次哈希分支就会把 EMBEDDING_DEGRADED 置位，所以先探一次。
            import ais_accelerator.regulation_db as _rdb
            _rdb.get_embedding("探针")
            if _rdb.EMBEDDING_DEGRADED:
                print("!! 向量化已退化为哈希伪向量，拒绝执行 --fix-regulation")
                print("   否则全部结果都会低于相似度门槛，42 行法规引用会被清空。")
                print("   请检查 OLLAMA_HOST（端口见 .env 的 OLLAMA_HOST_PORT，默认 11434）后重试。")
                conn.close()
                return 2
            print(f"Milvus 法规库可用，相似度门槛 {args.min_similarity:.2f}\n")

    sql = ("SELECT id, mmsi, violation_type, violation_label, regulation_ref, "
           "final_assessment, detailed_result "
           "FROM detection_results WHERE violation_detected = 1")
    params = ()
    if args.only:
        sql += " AND id = ?"
        params = (args.only,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    print(f"待处理违规记录: {len(rows)}\n")

    hdr = f"{'id':>4}  {'mmsi':<10} {'旧标签':<18} {'新类型':<12} {'新细类':<26} {'实测量值':>10}"
    print(hdr)
    print("-" * 100)

    changed = 0
    stat = {}
    ref_fixed = ref_blanked = 0

    for r in rows:
        events = parse_events(r["detailed_result"])
        info = classify(r["violation_type"], events)
        code, subtype, metric = info["code"], info["subtype"], info["metric"]
        new_label = info["label"]
        metric_val = float(metric) if metric is not None else 0.0

        old_label = r["violation_label"] or ""
        if old_label != new_label:
            changed += 1
        stat[code] = stat.get(code, 0) + 1

        metric_txt = f"{metric_val:.1f}" if metric is not None else "-"
        print(f"{r['id']:>4}  {r['mmsi']:<10} {old_label[:16]:<18} {code:<12} "
              f"{subtype or '-':<26} {metric_txt:>10}")

        if args.dry_run:
            continue

        sets = ["violation_code=?", "violation_subtype=?", "violation_metric=?", "violation_label=?"]
        vals = [code, subtype, metric_val, new_label]

        if regdb is not None:
            # 用**新规范码**检索（不是 LLM 的脏码），锚定优先，再过相似度门槛
            try:
                arts = regdb.search_by_violation(code, None, top_k=3)
            except Exception as e:
                print(f"      法规检索异常: {e}")
                arts = []
            arts = [a for a in arts if (a.get("similarity") or 0.0) >= args.min_similarity]
            if arts:
                new_ref = format_ref(arts)
                ref_fixed += 1
            else:
                # 宁可留空 —— 不写看起来像检索结果的兜底文本
                new_ref = ""
                ref_blanked += 1
            new_event = build_violation_event(
                code, r["final_assessment"] or "", new_ref, arts[0] if arts else None)
            sets += ["regulation_ref=?", "violation_event=?"]
            vals += [new_ref, new_event]

        conn.execute(
            f"UPDATE detection_results SET {', '.join(sets)} WHERE id=?", vals + [r["id"]]
        )

    if not args.dry_run:
        conn.commit()
    conn.close()

    print()
    print("规范码分布:", dict(sorted(stat.items(), key=lambda kv: -kv[1])))
    if args.dry_run:
        print(f"（dry-run，未写库）标签或类型发生变化: {changed}/{len(rows)}")
    else:
        print(f"已回填 {len(rows)} 条；标签或类型发生变化: {changed}")
        if regdb is not None:
            print(f"法规引用重建: {ref_fixed} 条命中 / {ref_blanked} 条低于门槛已清空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
