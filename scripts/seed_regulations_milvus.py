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

前置：
  - Docker Milvus 已启动（localhost:19530）
  - 依赖：pymilvus>=2.4, fitz（PyMuPDF）/pdfminer, ollama（可选，不做则哈希向量）
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
)

MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530")
COLLECTION = "regulation_articles"


def parse_all_articles() -> list:
    """解析 sources：regulations.json + rule/*.pdf，返回 RegulationArticle 列表。"""
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

    # 2. rule/*.pdf
    rule_dir = ROOT / "rule"
    if rule_dir.exists():
        for pdf in sorted(rule_dir.glob("*.pdf")):
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(str(pdf))
                text = "".join(p.get_text() or "" for p in doc)
                doc.close()
            except ImportError:
                from pdfminer.high_level import extract_text
                text = extract_text(str(pdf))
            if text.strip():
                arts = RegulationParser.parse_pdf_text(text, pdf.stem)
                if arts:
                    articles.extend(arts)
                    print(f"  从 {pdf.name} 解析 {len(arts)} 条")
                else:
                    print(f"  [SKIP] {pdf.name} 解析出 0 条（文本无「第X条」格式，不入库）")
            else:
                print(f"  [SKIP] {pdf.name} 无文本")
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
        client.insert(collection_name=COLLECTION, data=recs)
        print(f"  已导入 {min(i + B, len(articles))}/{len(articles)} 条")
    client.flush(collection_name=COLLECTION)


def main():
    force = "--force" in sys.argv
    client = MilvusClient(uri=MILVUS_URI)
    print(f"=== 连接 {MILVUS_URI}，数据库 {client.list_databases()} ===")

    articles = parse_all_articles()
    print(f"\n解析到 {len(articles)} 条法规条目")
    if not articles:
        print("无条目可导入，退出")
        return

    if not ensure_collection(client, force):
        return  # 集合已存在且非 force，直接退出（不覆盖）

    insert(client, articles)
    cnt = client.get_collection_stats(COLLECTION).get("row_count", 0)
    print(f"\n完成！regulation_articles 共 {cnt} 条法规。")
    print("此后 can 删 rule/ 文件夹，运行时会从本集合检索法规。")


if __name__ == "__main__":
    main()