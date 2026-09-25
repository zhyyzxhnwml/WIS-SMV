"""
scripts/reembed_regulations.py — 用真实 embedding 模型重新向量化现有法规库
============================================================================

【为什么需要】
  旧法规库是用 qwen2.5:7b-instruct 做 embedding，而该模型不支持 embeddings，
  全部退化为哈希伪向量 → 语义检索"相关度"是假的（任何查询都 ~0.98-0.99）。
  现在 EMBEDDING_MODEL 已切到 nomic-embed-text（768 维，补 0 到 1024），
  需要把 Milvus 里已有的 273 条法规重新向量化。

【与 seed 脚本的区别】
  seed_regulations_milvus.py 依赖 rule/*.pdf 重建集合；rule/ 目录已删除，
  --force 会丢法规。本脚本直接读取集合内现有条目 → 重新向量化 → upsert，
  不清空、不依赖源文件。

用法：
  python scripts/reembed_regulations.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ais_accelerator.regulation_db import (  # noqa: E402
    RegulationDB, REGULATION_COLLECTION, get_embedding, EMBEDDING_MODEL,
)

OUTPUT_FIELDS = [
    "article_id", "source", "article_number", "title", "content",
    "penalty", "violation_codes", "keywords", "chapter",
]


def rebuild_embedding_text(r: dict) -> str:
    """与 RegulationArticle.embedding_text() 保持一致（不实例化对象）。"""
    parts = []
    if r.get("chapter"):
        parts.append(f"【{r['chapter']}】")
    parts.append(f"第{r.get('article_number', '')}条 {r.get('title', '')}")
    parts.append(r.get("content", ""))
    if r.get("penalty"):
        parts.append(f"罚则：{r['penalty']}")
    keywords = r.get("keywords") or []
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except Exception:
            keywords = []
    if keywords:
        parts.append("关键词：" + "、".join(keywords))
    return "\n".join(parts)


def main() -> int:
    db = RegulationDB()
    if not db._available or not db._has_collection():
        print("[ERROR] 无法连接 Milvus 或 regulation_articles 集合不存在")
        return 1

    c = db.client
    c.load_collection(REGULATION_COLLECTION)

    # 一次读全量（Milvus query limit 上限 16384，法规库几百条足够）
    articles = c.query(
        collection_name=REGULATION_COLLECTION,
        filter="",
        output_fields=OUTPUT_FIELDS,
        limit=2000,
    )

    total = len(articles)
    print(f"读取现有法规条目: {total} 条 | embedding 模型: {EMBEDDING_MODEL}")
    if total == 0:
        print("[WARN] 集合为空，无需重向量化")
        return 0

    records = []
    for i, art in enumerate(articles, 1):
        text = rebuild_embedding_text(art)
        vec = get_embedding(text)
        rec = dict(art)
        rec["content_vector"] = vec
        # violation_codes / keywords 保持字符串（集合里是 VARCHAR）
        if isinstance(rec.get("violation_codes"), (list, tuple)):
            rec["violation_codes"] = json.dumps(list(rec["violation_codes"]), ensure_ascii=False)
        if isinstance(rec.get("keywords"), (list, tuple)):
            rec["keywords"] = json.dumps(list(rec["keywords"]), ensure_ascii=False)
        records.append(rec)
        if i % 50 == 0:
            print(f"  向量化 {i}/{total} ...")

    # 分批 upsert（按主键 article_id 覆盖旧向量）
    BATCH = 50
    for i in range(0, len(records), BATCH):
        batch = records[i:i + BATCH]
        c.upsert(collection_name=REGULATION_COLLECTION, data=batch)
        print(f"  upsert {i + len(batch)}/{total}")

    c.load_collection(REGULATION_COLLECTION)

    # 验证：同义/反义查询的相关度应明显区分
    print("\n== 重向量化后验证（余弦相似度，越大越相关）==")
    for q in ["关闭AIS信号设备逃避监控", "在限速区域超过规定航速", "在禁渔区禁渔期进行捕捞作业", "船舶碰撞风险"]:
        hits = db.search(q, top_k=2)
        for h in hits[:2]:
            print("  query=%.18s...  sim=%.3f  [%s] 第%s条 %s" % (
                q, h.get("similarity", 0), h.get("source", ""),
                h.get("article_number", ""), (h.get("title") or "")[:22]))
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
