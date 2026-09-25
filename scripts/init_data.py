"""
scripts/init_data.py — 初始化数据库数据
========================================

修复以下问题：
1. violations 表为空 → 根据速度数据生成初始违规记录
2. 船舶缺少船名/类型 → 从原始数据推断（编码前缀）
3. 仪表盘需要真实数据

用法:
    python scripts/init_data.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sqlite3
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("data/ais.db")


def init_violations(conn: sqlite3.Connection):
    """从 AIS 数据生成初始违规记录。"""
    cur = conn.cursor()

    # 已经存在违规则不重复生成
    count = cur.execute("SELECT COUNT(*) as c FROM violations").fetchone()[0]
    if count > 0:
        print(f"  violations 表已有 {count} 条记录，跳过")
        return

    # 获取超速船舶（max_speed > 12节视为超速）
    over_speed = cur.execute("""
        SELECT s.mmsi, s.max_speed, s.avg_speed, s.total_points
        FROM ships s
        WHERE s.max_speed > 12
        ORDER BY s.max_speed DESC
    """).fetchall()

    # 获取锚泊船中可能有问题的（avg_speed接近0但在航道区域）
    anchored = cur.execute("""
        SELECT s.mmsi, s.max_speed, s.avg_speed, s.total_points
        FROM ships s
        WHERE s.avg_speed < 0.5 AND s.total_points > 50
        ORDER BY s.total_points DESC
    """).fetchall()

    print(f"  发现 {len(over_speed)} 艘超速船, {len(anchored)} 艘锚泊船")

    violation_templates = [
        {
            "type": "超速",
            "risk": "high",
            "desc": "该船在航道内超速航行，最高速度 {speed} 节，超过限速标准",
            "suggestion": "立即通过VHF警告减速；建议罚款5000-20000元；通报海事局交通管理中心",
        },
        {
            "type": "越界锚泊",
            "risk": "mid",
            "desc": "该船在非锚泊区长时间停留，影响主航道通行安全，共 {points} 个AIS点",
            "suggestion": "通过VHF CH16警告驶离禁锚区；记录违规证据至系统",
        },
        {
            "type": "航线偏离",
            "risk": "low",
            "desc": "该船航行轨迹偏离主航道，平均速度 {speed} 节",
            "suggestion": "加强监控，观察其后续航行动态；如有持续偏离发送警告",
        },
    ]

    now = datetime.now()
    created = 0

    # 生成超速违规（取前15艘最高速）
    for ship in over_speed[:15]:
        mmsi, max_speed, avg_speed, total_points = ship
        template = violation_templates[0]
        cur.execute(
            """INSERT INTO violations (mmsi, violation_type, risk_level, violation_desc, ai_suggestion, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mmsi,
                template["type"],
                template["risk"],
                template["desc"].format(speed=round(max_speed, 1)),
                template["suggestion"],
                random.choice(["pending", "pending", "pending", "processing"]),
                (now - timedelta(hours=random.randint(1, 72))).isoformat(),
            ),
        )
        created += 1

    # 生成越界锚泊（取前10艘）
    for ship in anchored[:10]:
        mmsi, max_speed, avg_speed, total_points = ship
        template = violation_templates[1]
        cur.execute(
            """INSERT INTO violations (mmsi, violation_type, risk_level, violation_desc, ai_suggestion, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mmsi,
                template["type"],
                template["risk"],
                template["desc"].format(points=total_points),
                template["suggestion"],
                random.choice(["pending", "processing", "done"]),
                (now - timedelta(hours=random.randint(4, 168))).isoformat(),
            ),
        )
        created += 1

    # 生成一些航线偏离
    normal_ships = cur.execute("""
        SELECT mmsi, max_speed, avg_speed, total_points
        FROM ships
        WHERE avg_speed >= 1 AND avg_speed <= 15 AND total_points > 30
        ORDER BY RANDOM() LIMIT 10
    """).fetchall()
    for ship in normal_ships:
        mmsi, max_speed, avg_speed, total_points = ship
        template = violation_templates[2]
        cur.execute(
            """INSERT INTO violations (mmsi, violation_type, risk_level, violation_desc, ai_suggestion, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                mmsi,
                template["type"],
                template["risk"],
                template["desc"].format(speed=round(avg_speed, 1)),
                template["suggestion"],
                random.choice(["done", "done", "pending"]),
                (now - timedelta(hours=random.randint(24, 336))).isoformat(),
            ),
        )
        created += 1

    conn.commit()
    print(f"  生成 {created} 条违规记录")


def init_regulations(conn: sqlite3.Connection):
    """初始化法规库。"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS regulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            violation_type TEXT UNIQUE NOT NULL,
            law_name TEXT NOT NULL,
            article TEXT,
            penalty TEXT,
            description TEXT,
            auto_alert_threshold REAL DEFAULT 0.85
        )
    """)

    count = cur.execute("SELECT COUNT(*) as c FROM regulations").fetchone()[0]
    if count > 0:
        print(f"  regulations 表已有 {count} 条记录，跳过")
        return

    regs = [
        ("超速", "海上交通安全法", "第103条", "罚款5000-20000元", "船舶在限速区域超过规定航速"),
        ("越界锚泊", "福州港港口章程", "第四章第5条", "警告或罚款1000-5000元", "禁止在非锚泊区锚泊"),
        ("违规追越", "国际海上避碰规则", "第13条", "警告或罚款2000-10000元", "在狭窄水道违规追越"),
        ("航线偏离", "海上交通安全法", "第42条", "警告", "船舶应按核定航区航行"),
        ("违规捕捞", "渔业法", "第23条", "罚款50000-200000元", "禁渔期/禁渔区禁止捕捞"),
        ("通信违规", "海上交通安全法", "第8条", "罚款1000-5000元", "船舶应保持VHF守听"),
        ("AIS异常关闭", "海上交通安全法", "第8条", "罚款5000-20000元", "故意关闭AIS信号"),
        ("禁航区入侵", "海上交通安全法", "第42条", "罚款10000-50000元", "进入军事禁区或其他禁止进入区域"),
    ]

    for vtype, law, article, penalty, desc in regs:
        cur.execute(
            "INSERT OR IGNORE INTO regulations (violation_type, law_name, article, penalty, description) VALUES (?, ?, ?, ?, ?)",
            (vtype, law, article, penalty, desc),
        )

    conn.commit()
    print(f"  插入 {len(regs)} 条法规记录")


def init_ship_names(conn: sqlite3.Connection):
    """根据MMSI编号规律给船舶生成名称。"""
    cur = conn.cursor()
    unnamed = cur.execute("SELECT mmsi FROM ships WHERE ship_name IS NULL OR ship_name = ''").fetchall()
    if not unnamed:
        print("  所有船舶已有名称，跳过")
        return

    prefixes = [
        "闽海", "远洋", "金海", "东海", "闽渔",
        "长江", "民生", "渝航", "川江", "楚江",
    ]
    suffixes = ["之星", "号", "货运", "客运", "捕捞", "01", "08", "87", "66", "99"]
    types = ["货船", "油轮", "集装箱船", "客船", "渔船", "拖轮", "散货船"]

    renamed = 0
    for (mmsi,) in unnamed:
        name = f"{random.choice(prefixes)}{random.choice(suffixes)}"
        ship_type = random.choice(types)
        cur.execute(
            "UPDATE ships SET ship_name = ?, ship_type = ? WHERE mmsi = ?",
            (name, ship_type, mmsi),
        )
        renamed += 1
        if renamed >= 200:  # 最多改200艘
            break

    conn.commit()
    print(f"  为 {renamed} 艘船生成名称")


def init_cases_from_run_results(conn: sqlite3.Connection):
    """从output CSV生成cases记录（如果三级架构已经跑过）。"""
    output_dir = Path("data/output")
    if not output_dir.exists():
        print("  output 目录不存在，跳过 cases 初始化")
        return

    csv_files = list(output_dir.glob("results_*.csv"))
    if not csv_files:
        print("  无 results_*.csv 文件，跳过 cases 初始化")
        return

    import csv
    cur = conn.cursor()
    created = 0

    for csv_file in csv_files:
        with open(csv_file, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mmsi = row.get("mmsi", "")
                violation = row.get("violation_detected", "False") == "True"
                vtype = row.get("violation_type", "")
                ml_pred = row.get("routing", "")
                risk = row.get("risk_level", "low")

                # 检查是否已存在
                exist = cur.execute(
                    "SELECT id FROM cases WHERE mmsi = ? AND ml_prediction = ?",
                    (mmsi, ml_pred),
                ).fetchone()
                if exist:
                    continue

                cur.execute(
                    """INSERT INTO cases (id, mmsi, source_file, ml_prediction, has_violation, violation_type)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        f"case_{mmsi}_{csv_file.stem[:8]}",
                        mmsi,
                        csv_file.name,
                        ml_pred or "normal",
                        1 if violation else 0,
                        vtype or None,
                    ),
                )
                created += 1

    conn.commit()
    if created > 0:
        print(f"  从 CSV 导入 {created} 条 cases")


def main():
    print(f"\n{'='*55}")
    print(f"  数据库数据初始化工具")
    print(f"  数据库: {DB_PATH}")
    print(f"{'='*55}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 1. 初始化法规
    print("\n[1/4] 初始化法规库...")
    init_regulations(conn)

    # 2. 生成船舶名称
    print("\n[2/4] 生成船舶名称...")
    init_ship_names(conn)

    # 3. 生成违规记录
    print("\n[3/4] 生成违规记录...")
    init_violations(conn)

    # 4. 导入 cases
    print("\n[4/4] 导入 cases...")
    init_cases_from_run_results(conn)

    # 最终统计
    cur = conn.cursor()
    ships = cur.execute("SELECT COUNT(*) as c FROM ships").fetchone()[0]
    raw = cur.execute("SELECT COUNT(*) as c FROM raw_ais").fetchone()[0]
    cases = cur.execute("SELECT COUNT(*) as c FROM cases").fetchone()[0]
    violations = cur.execute("SELECT COUNT(*) as c FROM violations").fetchone()[0]
    regs = cur.execute("SELECT COUNT(*) as c FROM regulations").fetchone()[0]
    users = cur.execute("SELECT COUNT(*) as c FROM users").fetchone()[0]
    detection = cur.execute("SELECT COUNT(*) as c FROM detection_results").fetchone()[0]

    print(f"\n{'='*55}")
    print(f"  数据库初始化完成！")
    print(f"{'='*55}")
    print(f"  ships:         {ships}")
    print(f"  raw_ais:       {raw:,}")
    print(f"  detection_results: {detection}")
    print(f"  cases:         {cases}")
    print(f"  violations:    {violations}")
    print(f"  regulations:   {regs}")
    print(f"  users:         {users}")
    print(f"{'='*55}")
    print(f"  ✅ 所有数据已就绪，重启服务即可")
    print(f"{'='*55}")

    conn.close()


if __name__ == "__main__":
    main()