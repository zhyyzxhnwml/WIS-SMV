"""
scripts/migrate_sqlite_to_pg.py — 业务数据迁移：SQLite → Docker PostgreSQL
============================================================================

【迁移内容】Web 数据层的 3 张业务表：
  users, detection_results, cascade_logs

【规则】
  1. 管理员确定：users 有行 → 最早创建者提升为 role='管理员'；
     无行 → 创建 admin / admin123（提示尽快改密）。
  2. 存量 detection_results / cascade_logs 的 user_id 全部重写为管理员 id
     （对应「存量数据归首个/管理员账号」的用户决策）。
  3. 幂等：每次运行先 DROP 这 3 张 PG 表再重建，避免重复迁移。

【用法】在项目根目录运行：
  python scripts/migrate_sqlite_to_pg.py
"""
import shutil
import sys
import io
from datetime import datetime
from pathlib import Path

# Windows 终端 UTF-8 修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import pg_config  # noqa: E402

SQLITE_PATH = ROOT / "data" / "ais.db"

# ── PG DDL（与 SQLite 同构，字符串处理查询零改动） ───────────────
USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT DEFAULT '用户',
    avatar TEXT DEFAULT '👤',
    created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS'))
)
"""

DETECTION_DDL = """
CREATE TABLE IF NOT EXISTS detection_results (
    id SERIAL PRIMARY KEY,
    mmsi TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source_file TEXT,
    analysis_time TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')),
    track_points INTEGER DEFAULT 0,
    segments INTEGER DEFAULT 0,
    annotations TEXT DEFAULT '[]',
    avg_speed DOUBLE PRECISION DEFAULT 0,
    max_speed DOUBLE PRECISION DEFAULT 0,
    quality_level TEXT DEFAULT '',
    ml_category TEXT DEFAULT '',
    ml_confidence DOUBLE PRECISION DEFAULT 0,
    routing TEXT DEFAULT '',
    ml_probabilities TEXT DEFAULT '{}',
    violation_detected INTEGER DEFAULT 0,
    violation_type TEXT DEFAULT '',
    risk_level TEXT DEFAULT 'low',
    confidence DOUBLE PRECISION DEFAULT 0,
    recommended_action TEXT DEFAULT '',
    final_assessment TEXT DEFAULT '',
    regulation_ref TEXT DEFAULT '',
    llm_latency_seconds DOUBLE PRECISION DEFAULT 0,
    detailed_result TEXT DEFAULT '{}'
)
"""

CASCADE_DDL = """
CREATE TABLE IF NOT EXISTS cascade_logs (
    id SERIAL PRIMARY KEY,
    user_id TEXT DEFAULT 'system',
    timestamp TEXT,
    date TEXT,
    stage TEXT,
    level TEXT,
    message TEXT
)
"""

DDLS = [USERS_DDL, DETECTION_DDL, CASCADE_DDL]


def main():
    if not SQLITE_PATH.exists():
        print(f"✗ 未找到 SQLite 数据库: {SQLITE_PATH}")
        sys.exit(1)

    # 1. 备份
    bak = SQLITE_PATH.with_suffix(".db.bak")
    shutil.copy2(SQLITE_PATH, bak)
    print(f"✓ 已备份 SQLite 到 {bak}")

    import sqlite3
    src = sqlite3.connect(str(SQLITE_PATH))
    src.row_factory = sqlite3.Row

    c = pg_config()
    pg = psycopg2.connect(host=c.host, port=c.port, dbname=c.db,
                          user=c.user, password=c.password)
    pg.autocommit = False
    pgc = pg.cursor()

    # 2. 重置 PG 三张业务表（幂等）
    pgc.execute("DROP TABLE IF EXISTS cascade_logs, detection_results, users CASCADE")
    for ddl in DDLS:
        pgc.execute(ddl)
    pg.commit()
    print("✓ PG 三张业务表已重建")

    # 3. 迁移 users
    users = src.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    if users:
        admin = users[0]  # 最早创建者
        admin_id, admin_name = admin["id"], admin["username"]
        for u in users:
            pgc.execute(
                "INSERT INTO users (id, username, password, role, avatar, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (u["id"], u["username"], u["password"],
                 ("管理员" if u["id"] == admin_id else u["role"]),
                 u["avatar"] or "👤", u["created_at"] or ""),
            )
        pg.commit()
        print(f"✓ 迁移 users {len(users)} 条；最早创建者「{admin_name}」已提升为管理员")
    else:
        import hashlib, uuid
        admin_id = str(uuid.uuid4())[:8]
        admin_name = "admin"
        pw_hash = hashlib.sha256("admin123".encode()).hexdigest()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pgc.execute(
            "INSERT INTO users (id, username, password, role, avatar, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (admin_id, admin_name, pw_hash, "管理员", "👤", now),
        )
        pg.commit()
        print(f"✓ 无存量用户，已创建管理员 admin/admin123（请尽快登录修改密码）")

    # 4. 迁移 detection_results（user_id 全部归管理员）
    src_det = src.execute("SELECT * FROM detection_results").fetchall()
    det_cols = [d[0] for d in src.execute("SELECT * FROM detection_results LIMIT 0").description]
    if src_det:
        cols = [c0 for c0 in det_cols if c0 != "id"]  # id 由 SERIAL 生成
        ph = ",".join(["%s"] * len(cols))
        col_sql = ",".join(cols)
        for r in src_det:
            vals = []
            for c0 in cols:
                if c0 == "user_id":
                    vals.append(admin_id)
                else:
                    vals.append(r[c0])
            pgc.execute(f"INSERT INTO detection_results ({col_sql}) VALUES ({ph})", vals)
        pg.commit()
    print(f"✓ 迁移 detection_results {len(src_det)} 条 → 归管理员 {admin_name}")

    # 5. 迁移 cascade_logs（新增 user_id，全部归管理员）
    src_log = src.execute("SELECT * FROM cascade_logs").fetchall()
    log_cols = [d[0] for d in src.execute("SELECT * FROM cascade_logs LIMIT 0").description]
    if src_log:
        # SQLite 源无 user_id 列 → 显式插入管理员 id（列头放最前）
        cols = ["user_id"] + [c0 for c0 in log_cols if c0 != "id"]
        ph = ",".join(["%s"] * len(cols))
        col_sql = ",".join(cols)
        for r in src_log:
            vals = [admin_id] + [r[c0] for c0 in cols if c0 != "user_id"]
            pgc.execute(f"INSERT INTO cascade_logs ({col_sql}) VALUES ({ph})", vals)
        pg.commit()
    print(f"✓ 迁移 cascade_logs {len(src_log)} 条 → 归管理员 {admin_name}")

    # 6. 行数校验
    for table in ("users", "detection_results", "cascade_logs"):
        n_sqlite = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pgc.execute(f"SELECT COUNT(*) FROM {table}")
        n_pg = pgc.fetchone()[0]
        mark = "OK" if n_sqlite == n_pg else "MISMATCH!"
        print(f"   {table}: sqlite={n_sqlite} pg={n_pg} {mark}")

    # 7. 展示管理员
    pgc.execute("SELECT id, username, role FROM users ORDER BY created_at ASC")
    print("   用户清单：", [dict(zip(("id", "username", "role"), row)) for row in pgc.fetchall()])

    pg.commit()
    pg.close()
    src.close()
    print("\n✓ 迁移完成。")


if __name__ == "__main__":
    main()
