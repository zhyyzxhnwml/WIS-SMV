"""
config.py — 项目统一配置中心（Milvus + PostgreSQL/pgvector 连接）
================================================================

【Milvus 连接模式】
  本项目【不支持 Lite 模式】，只连外部 Milvus 服务（Docker）。
  - MILVUS_URI 环境变量覆盖地址；MILVUS_ENABLE 可显式关闭（默认开启）。

【PostgreSQL / pgvector】
  本项目也用 PostgreSQL + pgvector 存结构化案例/训练集/反馈。
  连接参数统一在此，支持环境变量覆盖（PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD）。
"""

import os
from dataclasses import dataclass

# ── Milvus 默认配置（想改地址只改这里） ─────────────────────────────
MILVUS_DEFAULT_URI = os.environ.get("MILVUS_URI", "http://localhost:19530")
# 是否启用 Milvus：'0'/'off'/'false' 显式关闭才禁用；默认开启
MILVUS_ENABLE = os.environ.get("MILVUS_ENABLE", "on").strip().lower() not in ("0", "off", "false", "no")


def milvus_uri() -> str | None:
    """返回应使用的 Milvus 连接地址；未启用则返回 None。"""
    env_uri = os.environ.get("MILVUS_URI")
    if env_uri:
        return env_uri
    if MILVUS_ENABLE:
        return MILVUS_DEFAULT_URI
    return None


# ── PostgreSQL / pgvector 连接参数（可用环境变量覆盖） ──────────────
@dataclass
class PGConfig:
    """PostgreSQL 连接配置。"""
    host: str
    port: int
    db: str
    user: str
    password: str


def pg_config() -> PGConfig:
    """返回 PostgreSQL/pgvector 连接配置（环境变量优先）。
    默认值对应你的 pgvector 容器：localhost:5432, postgres / wmlwxhn520
    """
    return PGConfig(
        host=os.environ.get("PG_HOST", "localhost"),
        port=int(os.environ.get("PG_PORT", "5432")),
        db=os.environ.get("PG_DB", "postgres"),
        user=os.environ.get("PG_USER", "postgres"),
        password=os.environ.get("PG_PASSWORD", "wmlwxhn520"),
    )


def pg_conninfo() -> str:
    """返回可用的连接 DSN（供 psycopg2 直接 connect 使用）。"""
    c = pg_config()
    return f"host={c.host} port={c.port} dbname={c.db} user={c.user} password={c.password}"