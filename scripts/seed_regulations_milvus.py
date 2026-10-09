"""seed_regulations_milvus.py — 把法规从 rule/*.pdf + regulations.json 一次性导入 Docker Milvus
============================================================================================
用途：
  你启动了 Docker Milvus（localhost:19530），希望把法规存进数据库，
  之后就不需要 rule/ 文件夹了（法规的"成品"在库里，"原料"PDF 只在理论上需要）。

本脚本：
  1. 连接 Docker Milvus（http://localhost:19530）
  2. 复用 RegulationDB 的解析逻辑，把 regulations.json + rule/*.pdf 解析成法规条目
  3. 向量化（走 qwen2.5 embedding，失败自动回退哈希向量）
  4. 写入 regulation_articles 集合（自动建集合，幂等：已存在则跳过/可强制重建）

本脚本只在规则数据源头不变时跑一次。之后 run.py 首扫会走 RegulationDB.initialize()
（若 MILVUS_URI 指向 Docker，则连 Docker；否则建本地 milvus_lite.db）——
一样能得到法规，但你想"只要数据库、不要 PDF"，就用本脚本一次性把法规固化进 Docker 库。

用法：
  python scripts/seed_regulations_milvus.py            # 导入（集合不存在才建）
  python scripts/seed_regulations_milvus.py --force    # 强制重建集合后重新导入
  python scripts/seed_regulations_milvus.py --force --allow-degraded   # 明知无向量服务仍导入

前置：
  - Docker Milvus 已启动（localhost:19530）
  - 依赖：pymilvus>=2.4, fitz（PyMuPDF）/pdfminer, ollama（可选，不做则哈希向量）

⚠️ 关于"失败自动回退哈希向量"：
  get_embedding() 在连不上 Ollama 时会静默退化成哈希向量（1024 维但无语义）。
  用它建的库**检索结果全是噪声**，而调用方看不出区别 —— 比直接报错危险得多。
  本脚本因此在导入前先探针一次，发现退化就拒绝执行（除非显式 --allow-degraded）。
  最常见的踩法：在宿主机上跑本脚本 —— Ollama 只在 compose 网络内可达（ollama:11434），
  宿主机需要 OLLAMA_HOST=http://localhost:<OLLAMA_HOST_PORT>（见 .env）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymilvus import MilvusClient, DataType  # noqa: E402
from ais_accelerator.regulation_db import (  # noqa: E402
    RegulationDB, RegulationParser, get_embedding,
    apply_doc_meta, dedupe_articles, report_duplicates,
)

MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530")
COLLECTION = "regulation_articles"

# 探针文本：任意中文即可，只用来判断是否退化成哈希向量
_PROBE = "船舶应当按照有关规定开启自动识别系统并持续进行显示和记录"


def assert_embedding_alive(allow_degraded: bool) -> None:
    """导入前确认向量化可用。

    nomic-embed-text 是 768 维，regulation_db 补齐到 1024（尾 256 位恒为 0）；
    哈希向量是 1024 维全填满。用这个差异判断，比看 EMBEDDING_DEGRADED 更直接
    （后者只在本次进程真的走过回退路径时才为 True）。
    """
    import ais_accelerator.regulation_db as rdb

    vec = get_embedding(_PROBE)
    degraded = bool(getattr(rdb, "EMBEDDING_DEGRADED", False))
    hash_like = any(x != 0.0 for x in vec[768:])
    if degraded or hash_like:
        msg = (
            "向量化不可用：get_embedding 返回的是哈希向量而非语义向量。\n"
            f"  EMBEDDING_DEGRADED={degraded}  尾256位非零(哈希特征)={hash_like}\n"
            f"  OLLAMA_HOST={os.environ.get('OLLAMA_HOST', '(未设置)')}\n"
            "  这样做出来的库检索全是噪声，已中止。\n"
            "  确认要强行导入（仅供排障）请加 --allow-degraded。"
        )
        if allow_degraded:
            print("[WARN] " + msg.replace("\n", "\n[WARN] "))
            return
        print("[FATAL] " + msg)
        sys.exit(2)
    print(f"  向量化正常（dim={len(vec)}，尾 256 位为 0 → nomic 768 补齐）")


def parse_all_articles() -> list:
    """解析 sources：regulations.json + rule/**/*.pdf，返回 RegulationArticle 列表。"""
    from ais_accelerator.regulation_db import apply_doc_meta

    articles = []
    # 1. regulations.json（含 zones → 区域管制）
    reg_json = ROOT / "regulations.json"
    if reg_json.exists():
        try:
            arts = RegulationParser.parse_regulations_json(reg_json)
            articles.extend(arts)
            print(f"  从 {reg_json.name} 解析 {len(arts)} 条")
        except Exception as e:
            print(f"  [WARN] 解析 {reg_json.name} 失败: {e}")

    # 2. rule/ 下的 PDF（**递归** —— 按 violation_catalog.DOC_META 的文件性质分子目录）
    rule_dir = ROOT / "rule"
    if rule_dir.exists():
        for pdf in sorted(rule_dir.rglob("*.pdf")):
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(str(pdf))
                text = "".join(p.get_text() or "" for p in doc)
                doc.close()
            except ImportError:
                from pdfminer.high_level import extract_text
                text = extract_text(str(pdf))
            if not text.strip():
                print(f"  [SKIP] {pdf.name} 无文本")
                continue
            # parse_pdf_text_auto：先试「第X条」，不行再试 1./4.1.1 小数编号。
            # 不能写死 parse_pdf_text —— 指南类文件整篇没有「第X条」，会解析出 0 条
            # 而被静默跳过（这正是「船舶防台风指南（试行）」当初没入库的原因）。
            arts = RegulationParser.parse_pdf_text_auto(text, pdf.stem)
            if arts:
                articles.extend(arts)
                print(f"  从 {pdf.name} 解析 {len(arts)} 条")
            else:
                print(f"  [SKIP] {pdf.name} 解析出 0 条（两种编号体例都不匹配，不入库）")

    # 3. 去重（交叉引用会让同一个条号被切出多份；主键重复 upsert 会直接报错）
    articles, dups = dedupe_articles(articles)
    report_duplicates(dups)

    # 4. 盖效力层级/文件性质/适用地域
    unknown = apply_doc_meta(articles)
    if unknown:
        print(f"  [WARN] 未在 DOC_META 登记，将排末尾(priority=9)：{unknown}")
    return articles


def ensure_collection(client: MilvusClient, force: bool):
    """确保 regulation_articles 集合存在。"""
    if client.has_collection(COLLECTION):
        if force:
            client.drop_collection(COLLECTION)
            print(f"  已删除旧集合 {COLLECTION}（force）")
        else:
            cnt = client.get_collection_stats(COLLECTION).get("row_count", 0)
            print(f"  集合 {COLLECTION} 已存在，{cnt} 条。加 --force 可重建。")
            return False

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("article_id", DataType.VARCHAR, max_length=256, is_primary=True)
    schema.add_field("source", DataType.VARCHAR, max_length=256)
    schema.add_field("article_number", DataType.VARCHAR, max_length=64)
    schema.add_field("title", DataType.VARCHAR, max_length=512)
    schema.add_field("content", DataType.VARCHAR, max_length=65535)
    schema.add_field("penalty", DataType.VARCHAR, max_length=2048)
    schema.add_field("violation_codes", DataType.VARCHAR, max_length=512)
    schema.add_field("keywords", DataType.VARCHAR, max_length=1024)
    schema.add_field("chapter", DataType.VARCHAR, max_length=256)
    # ⚠ 与 ais_accelerator/regulation_db.py::_create_collection 的 schema 必须一致
    #   （两处建的是同一个集合；只改一处会出现"本地 milvus_lite 有、Docker 库没有"）
    schema.add_field("priority", DataType.INT64)
    schema.add_field("doc_type", DataType.VARCHAR, max_length=32)
    schema.add_field("region", DataType.VARCHAR, max_length=32)
    schema.add_field("content_vector", DataType.FLOAT_VECTOR, dim=1024)
    index_params = client.prepare_index_params()
    index_params.add_index("content_vector", metric_type="COSINE",
                           index_type="IVF_FLAT", params={"nlist": 64})
    client.create_collection(collection_name=COLLECTION, schema=schema,
                             index_params=index_params)
    client.load_collection(COLLECTION)
    print(f"  集合 {COLLECTION} 创建完成")
    return True


def insert(client: MilvusClient, articles: list):
    B = 50
    for i in range(0, len(articles), B):
        batch = articles[i:i + B]
        recs = []
        for a in batch:
            d = a.to_dict()
            d["content_vector"] = get_embedding(a.embedding_text())
            recs.append(d)
        # 与 ais_accelerator/regulation_db.py::_insert_articles 保持同一口径：
        # article_id 是主键，用 upsert 覆盖，重复导入不留多版本。
        # （insert 会给同主键留两个版本：row_count 翻倍、检索里同一条占多个坑。
        #   --force 会重建集合所以看不出来，但非 force 的误跑会中招。）
        client.upsert(collection_name=COLLECTION, data=recs)
        print(f"  已导入 {min(i + B, len(articles))}/{len(articles)} 条")
    client.flush(collection_name=COLLECTION)


def main():
    force = "--force" in sys.argv
    allow_degraded = "--allow-degraded" in sys.argv
    client = MilvusClient(uri=MILVUS_URI)
    print(f"=== 连接 {MILVUS_URI}，数据库 {client.list_databases()} ===")

    assert_embedding_alive(allow_degraded)

    articles = parse_all_articles()
    print(f"\n解析到 {len(articles)} 条法规条目")
    if not articles:
        print("无条目可导入，退出")
        return

    # 落库前最后一道闸：主键必须唯一。去重之后还重复，说明去重被绕过或数据坏了，
    # 这时**绝不能往下走** —— 下面 ensure_collection(--force) 会先删集合再插入，
    # 插入一旦失败就只剩一个空库（2026-10-09 正是这么把 regulation_articles 删空的）。
    ids = [a.article_id for a in articles]
    if len(ids) != len(set(ids)):
        seen: dict[str, int] = {}
        for i in ids:
            seen[i] = seen.get(i, 0) + 1
        bad = sorted(k for k, v in seen.items() if v > 1)
        print(f"[FATAL] 去重后仍有 {len(bad)} 个重复主键（示例 {bad[:5]}），"
              f"已中止，未触碰集合")
        sys.exit(2)

    if not ensure_collection(client, force):
        return  # 集合已存在且非 force，直接退出（不覆盖）

    insert(client, articles)
    cnt = client.get_collection_stats(COLLECTION).get("row_count", 0)
    print(f"\n完成！regulation_articles 共 {cnt} 条法规。")

    # 逐个校验 REG_ANCHORS 锚点 —— 锚定表是 regulation_db.get_article() 的入口，
    # 名字对不上（source 名 ≠ PDF 文件名去扩展名）就会静默全部 MISS
    try:
        from ais_accelerator.violation_catalog import REG_ANCHORS
        ids = {a.article_id for a in articles}
        miss = []
        for code, refs in REG_ANCHORS.items():
            for src, num in refs:
                aid = f"{src}:第{num}条"
                if aid not in ids:
                    miss.append(f"{code} → {aid}")
        total = sum(len(v) for v in REG_ANCHORS.values())
        if miss:
            print(f"\n[WARN] 锚点缺失 {len(miss)}/{total}：")
            for m in miss:
                print("   " + m)
        else:
            print(f"\n锚点自检：{total}/{total} 全部命中")
    except Exception as e:
        print(f"\n[WARN] 锚点自检失败：{e}")


if __name__ == "__main__":
    main()