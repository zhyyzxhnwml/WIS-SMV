"""
scripts/upgrade_roles_orgs.py — 角色升级 + 组织表初始化（一次性，幂等）
=========================================================================

【做什么】
  1. users 表补充 name（显示昵称）列（幂等：ADD COLUMN IF NOT EXISTS）。
  2. 创建 organizations / org_members 表（幂等：CREATE TABLE IF NOT EXISTS）。
  3. 将用户 wis 升级为 role='超级管理员'（三级角色：超级管理员/管理员/用户）。

【用法】在项目根目录运行：
  python scripts/upgrade_roles_orgs.py
"""
import sys
import io
from pathlib import Path

# Windows 终端 UTF-8 修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pg_db  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    admin_id TEXT NOT NULL,
    created_at TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS'))
);
CREATE INDEX IF NOT EXISTS idx_org_admin ON organizations(admin_id);

CREATE TABLE IF NOT EXISTS org_members (
    org_id TEXT,
    user_id TEXT,
    PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_orgmember_user ON org_members(user_id);

ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT DEFAULT '';
"""


def main():
    conn = pg_db.connect()
    try:
        conn.executescript(DDL)
        conn.execute(
            "UPDATE users SET role = '超级管理员' WHERE username = 'wis'"
        )
        conn.commit()

        total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        supers = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role = '超级管理员'"
        ).fetchone()["c"]
        orgs = conn.execute("SELECT COUNT(*) AS c FROM organizations").fetchone()["c"]
        members = conn.execute("SELECT COUNT(*) AS c FROM org_members").fetchone()["c"]
        print(f"OK  users_total={total} super_admin={supers} orgs={orgs} org_members={members}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
