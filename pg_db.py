"""
pg_db.py — psycopg2 之上的 sqlite3 兼容层（Docker PostgreSQL）
================================================================

【为什么存在？】
  Web 数据层从 SQLite(data/ais.db) 迁移到 Docker PostgreSQL 后，
  app.py / run.py / cascade_agent.py / shipclaw 里大量形如
      conn.execute("... ? ...", params).fetchone()["col"]
  的调用保持零改动。本模块在 psycopg2 之上提供 sqlite3 风格的连接外壳：
    - execute(sql, params)  → 返回 RealDictCursor（row["col"] / row[0] 都能用）
    - executescript(sql)    → 按分号切分逐条执行（CREATE TABLE ...; CREATE INDEX ...;）
    - commit() / close()    → 透传
    - ? 占位符自动翻译为 %s；SQL 里字面量 % 自动转义为 %%

【? 与 % 的翻译规则】
  翻译分两步（\x00 作占位保护）：
    1. 把 sql 里所有 ? 替换为 \x00
    2. 把剩余 % 全部转义为 %% （psycopg2 的 pyformat 要求字面量 % 写 %%，否则当作格式化符）
    3. 把 \x00 还原为 %s
  这样既保留调用方熟悉的 ? 占位符，又不破坏 LIKE '%x%' 这类字面量。
  注意：execute 时统一传 () 作为空参数，保证 psycopg2 一定会执行 % 格式化。

【通用用户过滤】
  user_scope(conn, user_id)：级联/skill 等非 Web 侧使用，全员数据隔离：
    - user_id 为空或 'system'/'default' → 全局（系统内部操作）
    - 其它（超级管理员/管理员/用户 一律） → 只自己（user_id = ?）
  注意：app.py 的 _user_scope() 逻辑与本函数一致（均为全员隔离），
  两处需保持同步。
"""

import re

import psycopg2
import psycopg2.extras

from config import pg_config


def connect():
    """建立到 Docker PostgreSQL 的兼容连接（每次调用独立连接）。"""
    c = pg_config()
    raw = psycopg2.connect(
        host=c.host,
        port=c.port,
        dbname=c.db,
        user=c.user,
        password=c.password,
    )
    return _Compat(raw)


def _translate(sql: str) -> str:
    """? → %s；字面量 % 转义为 %%（psycopg2 pyformat 规则）。"""
    out = sql.replace("?", "\x00").replace("%", "%%").replace("\x00", "%s")
    return out


def _split_statements(sql: str) -> list:
    """按分号切分多语句脚本（忽略注释行与空段）。"""
    stmts = []
    for part in sql.split(";"):
        part = part.strip()
        # 去掉行注释（-- 开头到行尾）
        cleaned = "\n".join(
            line for line in part.splitlines()
            if not line.strip().startswith("--")
        ).strip()
        if cleaned:
            stmts.append(cleaned)
    return stmts


def user_scope(conn, user_id):
    """通用用户过滤（全员数据隔离）：返回 (where_sql, params)。

    'system'/'default'/空 → 全局（系统内部操作）；
    其它账号（超级管理员/管理员/用户 一律）→ 只自己 + 系统全局数据
    （user_id = ? OR user_id = 'system'）：CLI 扫描（run.py 以 user_id='system' 写入）
    的结果对所有登录用户可见，自己的数据仍只有自己能看到。
    """
    if not user_id or user_id in ("system", "default"):
        return "", ()
    return "(user_id = ? OR user_id = 'system')", (user_id,)


class _Compat:
    """sqlite3.Connection 的兼容外壳。"""

    def __init__(self, raw):
        self._c = raw
        self.row_factory = None  # 可写属性；RealDictCursor 已提供 dict 行访问，无需实际设置

    def execute(self, sql, params=None):
        cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_translate(sql), params if params is not None else ())
        return cur

    def executescript(self, sql):
        for stmt in _split_statements(sql):
            cur = self._c.cursor()
            cur.execute(_translate(stmt))
        return None

    def commit(self):
        return self._c.commit()

    def rollback(self):
        return self._c.rollback()

    def close(self):
        return self._c.close()

    @property
    def connection(self):
        return self._c
