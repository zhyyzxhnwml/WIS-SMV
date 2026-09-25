"""
scripts/migrate_to_pg.py — SQLite 数据迁移到 PostgreSQL
=======================================================

【功能】
  将 data/ais.db 中的数据迁移到 PostgreSQL 数据库服务器（带 pgvector）。
  将来你安装好 PostgreSQL 后，配好环境变量执行此脚本即可。

【用法】
  # 先设置数据库连接（改这里的密码）
  set PG_HOST=服务器IP
  set PG_PORT=5432
  set PG_DB=ais_accelerator
  set PG_USER=postgres
  set PG_PASSWORD=你的密码

  # 执行迁移
  python scripts/migrate_to_pg.py

【需要先安装】
  pip install psycopg2-binary
"""

import sys, os, io
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pathlib import Path

# 数据库配置（从环境变量读取，改密码只需设 PG_PASSWORD）
PG_CONFIG = {
    "host": os.environ.get("PG_HOST", "localhost"),
    "port": int(os.environ.get("PG_PORT", 5432)),
    "dbname": os.environ.get("PG_DB", "ais_accelerator"),
    "user": os.environ.get("PG_USER", "postgres"),
    "password": os.environ.get("PG_PASSWORD", ""),
}

SQLITE_PATH = Path("data/ais.db")


def migrate():
    """将 SQLite 数据迁移到 PostgreSQL。"""
    try:
        import psycopg2
    except ImportError:
        print("❌ 需要安装 psycopg2: pip install psycopg2-binary")
        sys.exit(1)

    if not SQLITE_PATH.exists():
        print(f"❌ SQLite 数据库不存在: {SQLITE_PATH}")
        print("   请先运行: python scripts/import_csv_to_sqlite.py")
        sys.exit(1)

    import sqlite3

    # 连接 PostgreSQL
    print(f"\n{'='*55}")
    print(f"  连接 PostgreSQL...")
    print(f"  主机: {PG_CONFIG['host']}:{PG_CONFIG['port']}")
    print(f"  数据库: {PG_CONFIG['dbname']}")
    print(f"  用户: {PG_CONFIG['user']}")
    print(f"{'='*55}")

    try:
        pg_conn = psycopg2.connect(**PG_CONFIG)
        pg_conn.autocommit = True
        pg_cur = pg_conn.cursor()
        print("  ✅ PostgreSQL 连接成功")
    except Exception as e:
        print(f"  ❌ 连接失败: {e}")
        print("\n  请检查环境变量:")
        print("    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD")
        sys.exit(1)

    # 执行建表
    schema_path = Path("database/schema.sql")
    if schema_path.exists():
        print("  执行建表 SQL...")
        pg_cur.execute(schema_path.read_text(encoding="utf-8"))
        print("  ✅ Schema 建表完成")

    # 连接 SQLite
    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    sqlite_conn.row_factory = sqlite3.Row
    print(f"  ✅ SQLite 连接成功 ({SQLITE_PATH})")

    # ── 迁移 ships ──
    print("\n  迁移 ships 表...")
    sqlite_cur = sqlite_conn.execute("SELECT * FROM ships")
    ships = sqlite_cur.fetchall()
    for ship in ships:
        pg_cur.execute(
            """
            INSERT INTO ships (mmsi, ship_name, ship_type, length, width, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (mmsi) DO NOTHING
            """,
            (ship["mmsi"], ship.get("ship_name"), ship.get("ship_type"),
             ship.get("length"), ship.get("width")),
        )
    print(f"    ✅ {len(ships)} 条记录")

    # ── 迁移 raw_ais ──
    print("\n  迁移 raw_ais 表...")
    offset = 0
    batch_size = 10000
    total_ais = 0
    while True:
        sqlite_cur = sqlite_conn.execute(
            "SELECT * FROM raw_ais ORDER BY id LIMIT ? OFFSET ?",
            (batch_size, offset),
        )
        rows = sqlite_cur.fetchall()
        if not rows:
            break
        for row in rows:
            pg_cur.execute(
                """
                INSERT INTO raw_ais (mmsi, timestamp, lon, lat, speed, course, heading, source_file)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (row["mmsi"], row["timestamp"], row["lon"], row["lat"],
                 row["speed"], row["course"], row["heading"], row.get("source_file")),
            )
        offset += batch_size
        total_ais += len(rows)
        print(f"    → {total_ais} 条...")
    print(f"    ✅ {total_ais} 条记录")

    # ── 迁移 cases ──
    print("\n  迁移 cases 表...")
    sqlite_cur = sqlite_conn.execute("SELECT * FROM cases")
    cases = sqlite_cur.fetchall()
    for case in cases:
        pg_cur.execute(
            """
            INSERT INTO cases (id, mmsi, source_file, ml_prediction, ml_confidence,
                               has_violation, violation_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (case["id"], case["mmsi"], case.get("source_file"),
             case.get("ml_prediction"), case.get("ml_confidence"),
             bool(case.get("has_violation")) if case.get("has_violation") else None,
             case.get("violation_type")),
        )
    print(f"    ✅ {len(cases)} 条记录")

    # ── 迁移 violations ──
    print("\n  迁移 violations 表...")
    sqlite_cur = sqlite_conn.execute("SELECT * FROM violations")
    violations = sqlite_cur.fetchall()
    for v in violations:
        pg_cur.execute(
            """
            INSERT INTO violations (mmsi, violation_type, risk_level, violation_desc,
                                    ai_suggestion, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (v["mmsi"], v["violation_type"], v.get("risk_level", "low"),
             v.get("violation_desc"), v.get("ai_suggestion"), v.get("status", "pending")),
        )
    print(f"    ✅ {len(violations)} 条记录")

    pg_conn.commit()
    pg_cur.close()
    pg_conn.close()
    sqlite_conn.close()

    print(f"\n{'='*55}")
    print(f"  🎉 迁移完成!")
    print(f"  ships:     {len(ships)} 条")
    print(f"  raw_ais:   {total_ais} 条")
    print(f"  cases:     {len(cases)} 条")
    print(f"  violations: {len(violations)} 条")
    print(f"{'='*55}")


if __name__ == "__main__":
    print(f"\n{'='*55}")
    print(f"  SQLite → PostgreSQL 数据迁移工具")
    print(f"{'='*55}")
    migrate()