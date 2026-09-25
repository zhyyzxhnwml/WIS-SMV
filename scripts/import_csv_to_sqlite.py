"""
scripts/import_csv_to_sqlite.py — CSV船舶数据导入SQLite
======================================================

【功能】
  将 E:\AIS\data\input 目录下的 AIS CSV 文件导入 SQLite 数据库。
  数据库文件生成在 data/ais.db，包含 ships 和 raw_ais 两张表。

【用法】
  python scripts/import_csv_to_sqlite.py                          # 导入全部
  python scripts/import_csv_to_sqlite.py --file 铜锣峡              # 按文件名过滤
  python scripts/import_csv_to_sqlite.py --mmsi 413802097          # 只导某条船

【输出】
  data/ais.db — SQLite 数据库文件

【将来迁移到 PostgreSQL】
  改 DB_TYPE=postgres 并配好环境变量即可切换到 PostgreSQL。
  迁移脚本见 scripts/migrate_sqlite_to_pg.py
"""

import sys
import os
import io
import csv
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Windows UTF-8 修复
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 数据库路径
DB_PATH = Path("data/ais.db")
INPUT_DIR = Path("data/input")


# ── SQLite Schema（与 PostgreSQL 的 database/schema.sql 保持一致） ──

SCHEMA_SQL = """
-- ships 表
CREATE TABLE IF NOT EXISTS ships (
    mmsi TEXT PRIMARY KEY,
    ship_name TEXT,
    ship_type TEXT,
    length REAL,
    width REAL,
    first_seen TEXT,
    last_seen TEXT,
    total_points INTEGER DEFAULT 0,
    avg_speed REAL DEFAULT 0,
    max_speed REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- raw_ais 表（原始AIS数据点）
CREATE TABLE IF NOT EXISTS raw_ais (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi TEXT NOT NULL REFERENCES ships(mmsi),
    timestamp TEXT NOT NULL,
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    speed REAL DEFAULT 0,
    course REAL DEFAULT 0,
    heading REAL DEFAULT 0,
    source_file TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_raw_ais_mmsi ON raw_ais(mmsi);
CREATE INDEX IF NOT EXISTS idx_raw_ais_time ON raw_ais(timestamp);

-- cases 表（三级架构核心表）
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    mmsi TEXT NOT NULL,
    source_file TEXT,
    feature_vector TEXT,
    feature_json TEXT,
    annotations TEXT,
    ml_prediction TEXT,
    ml_confidence REAL,
    llm_result TEXT,
    has_violation INTEGER,
    violation_type TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- violations 表
CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    mmsi TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    risk_level TEXT DEFAULT 'low',
    violation_desc TEXT,
    ai_suggestion TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


# ── 导入函数 ──

def format_time(raw_time: str) -> str:
    """将 YYYYMMDDHHMMSS 转为 ISO 格式 YYYY-MM-DD HH:MM:SS"""
    if not raw_time or len(raw_time) != 14 or not raw_time.isdigit():
        return raw_time
    return f"{raw_time[:4]}-{raw_time[4:6]}-{raw_time[6:8]} {raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"


def import_csv_to_sqlite(
    csv_path: str,
    db_path: str = "data/ais.db",
    mmsi_filter: str = None,
    batch_size: int = 10000,
) -> dict:
    """将单个 CSV 文件导入 SQLite。

    Args:
        csv_path: CSV 文件路径
        db_path: 目标 SQLite 数据库路径
        mmsi_filter: 可选，只导入指定 MMSI
        batch_size: 批量插入每批条数

    Returns:
        导入统计信息
    """
    path = Path(csv_path)
    if not path.exists():
        return {"error": f"文件不存在: {csv_path}"}

    source_file = path.name
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")

    # 建表
    conn.executescript(SCHEMA_SQL)

    # 读取CSV
    ship_data = defaultdict(list)  # mmsi -> [rows]
    total_rows = 0
    valid_rows = 0
    invalid_rows = 0
    skipped_by_filter = 0

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # 标准化字段名
        reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]

        for row in reader:
            total_rows += 1
            mmsi = row.get("mmsi", "").strip()
            if not mmsi:
                invalid_rows += 1
                continue
            if mmsi_filter and mmsi != mmsi_filter:
                skipped_by_filter += 1
                continue

            # 解析必填字段
            try:
                lon = float(row.get("lon", 0))
                lat = float(row.get("lat", 0))
                speed = float(row.get("speed", 0))
                course = float(row.get("course", 0))
                heading = float(row.get("heading", 0))
                raw_time = row.get("time", "")
                timestamp = format_time(raw_time)
            except (ValueError, TypeError):
                invalid_rows += 1
                continue

            # 基本校验
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                invalid_rows += 1
                continue

            ship_data[mmsi].append({
                "mmsi": mmsi,
                "timestamp": timestamp,
                "lon": lon,
                "lat": lat,
                "speed": speed,
                "course": course,
                "heading": heading,
                "source_file": source_file,
            })
            valid_rows += 1

    print(f"  读取: {total_rows} 行 | 有效: {valid_rows} | 无效: {invalid_rows}"
          f"{f' | 跳过(MMSI过滤): {skipped_by_filter}' if skipped_by_filter else ''}")

    if not ship_data:
        conn.close()
        return {"total_rows": total_rows, "valid_rows": valid_rows, "ships_imported": 0}

    try:
        # ── 批量写入 raw_ais ──
        all_points = []
        for mmsi, points in ship_data.items():
            all_points.extend(points)

        conn.execute("BEGIN TRANSACTION")
        for i in range(0, len(all_points), batch_size):
            batch = all_points[i:i + batch_size]
            conn.executemany(
                """
                INSERT INTO raw_ais (mmsi, timestamp, lon, lat, speed, course, heading, source_file)
                VALUES (:mmsi, :timestamp, :lon, :lat, :speed, :course, :heading, :source_file)
                """,
                batch,
            )
        conn.commit()
        print(f"  写入 raw_ais: {len(all_points)} 条记录")

        # ── 写入/更新 ships ──
        ships_imported = 0
        for mmsi, points in ship_data.items():
            speeds = [p["speed"] for p in points]
            times = [p["timestamp"] for p in points]

            conn.execute(
                """
                INSERT INTO ships (mmsi, first_seen, last_seen, total_points, avg_speed, max_speed)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mmsi) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    total_points = total_points + excluded.total_points,
                    avg_speed = (avg_speed * (total_points - excluded.total_points) + excluded.avg_speed * excluded.total_points) / total_points,
                    max_speed = MAX(max_speed, excluded.max_speed)
                """,
                (
                    mmsi,
                    min(times),
                    max(times),
                    len(points),
                    round(sum(speeds) / len(speeds), 2),
                    max(speeds),
                ),
            )
            ships_imported += 1

        conn.commit()
        print(f"  写入/更新 ships: {ships_imported} 艘船")

    except Exception as e:
        conn.rollback()
        conn.close()
        return {"error": str(e)}

    conn.close()

    return {
        "source_file": source_file,
        "total_rows": total_rows,
        "valid_rows": valid_rows,
        "ships_imported": ships_imported,
        "ais_points_written": len(all_points),
    }


def import_all(mmsi_filter: str = None, file_filter: str = None) -> list[dict]:
    """导入 INPUT_DIR 下所有匹配的 CSV 文件。

    Args:
        mmsi_filter: 可选 MMSI 过滤
        file_filter: 可选文件名关键字过滤

    Returns:
        每个文件的导入结果列表
    """
    csv_files = sorted(INPUT_DIR.glob("*.csv"))
    if file_filter:
        csv_files = [f for f in csv_files if file_filter in f.name]

    if not csv_files:
        print(f"没有找到 CSV 文件 (目录: {INPUT_DIR.resolve()})")
        if file_filter:
            print(f"  文件名包含: '{file_filter}'")
        return []

    results = []
    for csv_file in csv_files:
        print(f"\n{'='*55}")
        print(f"  导入: {csv_file.name}")
        print(f"{'='*55}")
        result = import_csv_to_sqlite(
            csv_path=str(csv_file),
            db_path=str(DB_PATH),
            mmsi_filter=mmsi_filter,
        )
        results.append(result)
        if "error" in result:
            print(f"  ❌ 错误: {result['error']}")
        else:
            print(f"  ✅ 完成: {result['ships_imported']} 艘船, {result['ais_points_written']} 条轨迹点")

    return results


def show_summary(db_path: str = "data/ais.db") -> None:
    """打印数据库统计信息。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        ship_count = conn.execute("SELECT COUNT(*) as c FROM ships").fetchone()["c"]
        point_count = conn.execute("SELECT COUNT(*) as c FROM raw_ais").fetchone()["c"]
        case_count = conn.execute("SELECT COUNT(*) as c FROM cases").fetchone()["c"]
        violation_count = conn.execute("SELECT COUNT(*) as c FROM violations").fetchone()["c"]

        print(f"\n{'='*55}")
        print(f"  📊 数据库统计: {db_path}")
        print(f"{'='*55}")
        print(f"    船舶:   {ship_count} 艘")
        print(f"    AIS点:  {point_count:,} 条")
        print(f"    案例:   {case_count} 条")
        print(f"    违规:   {violation_count} 条")

        if ship_count > 0:
            top = conn.execute(
                "SELECT mmsi, total_points, max_speed FROM ships ORDER BY total_points DESC LIMIT 5"
            ).fetchall()
            print(f"\n    数据量前5船舶:")
            for r in top:
                print(f"      {r['mmsi']}: {r['total_points']} 点, 最高速度 {r['max_speed']:.1f} 节")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CSV船舶数据导入SQLite")
    parser.add_argument("--file", "-f", help="按文件名关键字过滤")
    parser.add_argument("--mmsi", "-m", help="只导入指定MMSI的船")
    parser.add_argument("--db", default="data/ais.db", help="SQLite数据库路径 (默认 data/ais.db)")
    parser.add_argument("--summary", "-s", action="store_true", help="仅显示统计信息")
    args = parser.parse_args()

    DB_PATH = Path(args.db)

    if args.summary:
        show_summary(str(DB_PATH))
    else:
        print(f"\n{'='*55}")
        print(f"  🚢 船舶 AIS 数据导入工具")
        print(f"  数据库: {DB_PATH}")
        print(f"  源目录: {INPUT_DIR.resolve()}")
        print(f"{'='*55}")

        results = import_all(mmsi_filter=args.mmsi, file_filter=args.file)
        if results:
            total_ships = sum(r.get("ships_imported", 0) for r in results)
            total_points = sum(r.get("ais_points_written", 0) for r in results)
            print(f"\n{'='*55}")
            print(f"  ✅ 全部完成! 共导入 {total_ships} 艘船, {total_points:,} 条AIS点")
            print(f"{'='*55}")

            # 显示统计
            show_summary(str(DB_PATH))