"""
ais_accelerator/ingest.py — CSV 数据导入与 SQLite 缓存
========================================================

【这个文件做什么？】
  读取 AIS CSV 文件，解析每行数据，按 MMSI（船舶唯一标识码）分组，
  存入 SQLite 数据库作为暂存。下次读同个文件时直接复用缓存。

【设计要点】
  1. 指纹缓存：用文件大小 + 修改时间生成指纹，同名内容不变则复用
  2. 批次写入：每 10,000 行批量 INSERT，避免逐行写入性能差
  3. 内存安全：通过 SQLite 按 MMSI 逐条读取，不用一次加载所有数据
  4. max_ships 控制：可限制处理的船舶数量，方便测试
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import csv  # CSV文件解析模块
import hashlib  # 哈希计算模块
import json  # JSON序列化模块
import sqlite3  # SQLite数据库模块
import time  # 时间模块
from pathlib import Path  # 路径处理模块
from typing import Iterator, Optional  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .models import AISPoint, IngestStats, ShipProfile  # 数据模型
from .quality import parse_raw_point  # 原始数据点解析函数


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ais_points (
    mmsi TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    speed REAL NOT NULL,
    course REAL NOT NULL,
    status TEXT,
    quality_flags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def source_fingerprint(source: Path) -> str:  # 生成源文件的指纹（用于缓存判断）
    stat = source.stat()  # 获取文件状态信息
    payload = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")  # 文件路径+大小+修改时间
    return hashlib.sha256(payload).hexdigest()[:16]  # 返回SHA256前16位作为指纹


class SQLiteAISStore:  # SQLite AIS数据存储器
    def __init__(self, config: AcceleratorConfig):  # 初始化
        self.config = config  # 加速器配置
        self.config.ensure_directories()  # 确保缓存和输出目录存在
        self.db_path: Optional[Path] = None  # 数据库文件路径
        self.stats: Optional[IngestStats] = None  # 导入统计信息

    def ingest(  # 导入CSV文件到SQLite缓存
        self, source_path: str, max_ships: Optional[int] = None  # CSV源文件路径, 最大船舶数（可选）
    ) -> IngestStats:
        started = time.perf_counter()  # 记录开始时间
        source = Path(source_path).resolve()  # 解析源文件绝对路径
        if not source.exists():  # 如果文件不存在
            raise FileNotFoundError(source)  # 抛出文件未找到异常
        fingerprint = source_fingerprint(source)  # 生成文件指纹
        suffix = f"_max{max_ships}" if max_ships else ""  # 如果限制船舶数，添加后缀
        self.db_path = self.config.cache_dir / f"{source.stem}_{fingerprint}{suffix}.sqlite"  # 数据库路径

        if self.db_path.exists() and self._cache_complete(self.db_path, fingerprint):  # 如果缓存完整且指纹匹配
            stats = self._load_stats(self.db_path)  # 从缓存加载统计信息
            stats.reused_cache = True  # 标记为复用缓存
            stats.elapsed_seconds = time.perf_counter() - started  # 计算耗时
            self.stats = stats  # 保存统计信息
            return stats  # 返回统计信息

        if self.db_path.exists():  # 如果缓存不完整但文件存在
            self.db_path.unlink()  # 删除旧缓存文件
        connection = sqlite3.connect(self.db_path)  # 连接SQLite数据库
        try:
            connection.executescript(SCHEMA_SQL)  # 执行建表SQL
            raw_rows = valid_rows = invalid_rows = 0  # 行计数初始化
            batch: list[tuple] = []  # 批量插入缓冲区
            selected_mmsi: set[str] = set()  # 已选船舶的MMSI集合
            with source.open("r", encoding="utf-8-sig", newline="") as handle:  # 以只读方式打开CSV文件
                reader = csv.DictReader(handle)  # 创建CSV字典读取器
                if not reader.fieldnames:  # 如果没有表头
                    raise ValueError("CSV缺少表头")  # 抛出值错误
                reader.fieldnames = [name.strip() for name in reader.fieldnames]  # 去除表头字段名的空格
                for row in reader:  # 遍历CSV的每一行
                    raw_rows += 1  # 原始行数加1
                    raw_mmsi = str(row.get("mmsi", "")).strip()  # 获取并清理MMSI
                    if (  # 如果限制了船舶数且当前MMSI未选但已达上限
                        max_ships  # 有限制
                        and raw_mmsi not in selected_mmsi  # 当前MMSI不在已选集合中
                        and len(selected_mmsi) >= max_ships  # 已选船舶数达到上限
                    ):
                        continue  # 跳过该行
                    point, _issues = parse_raw_point(row)  # 解析原始数据行
                    if point is None:  # 如果解析失败
                        invalid_rows += 1  # 无效行数加1
                        continue  # 跳过该行
                    selected_mmsi.add(point.mmsi)  # 将船舶MMSI加入已选集合
                    batch.append(  # 添加数据到批量缓冲区
                        (
                            point.mmsi,  # MMSI
                            point.timestamp.isoformat(),  # 时间戳
                            point.lat,  # 纬度
                            point.lon,  # 经度
                            point.speed,  # 航速
                            point.course,  # 航向
                            point.status,  # 船舶状态
                            json.dumps(point.quality_flags, ensure_ascii=False),  # 质量标记JSON
                        )
                    )
                    valid_rows += 1  # 有效行数加1
                    if len(batch) >= self.config.ingest_batch_size:  # 如果缓冲区达到批次大小
                        self._insert_batch(connection, batch)  # 批量插入数据库
                        batch.clear()  # 清空缓冲区
            if batch:  # 处理最后一批剩余数据
                self._insert_batch(connection, batch)  # 批量插入数据库
            connection.execute(  # 创建索引（按MMSI和时间排序）
                "CREATE INDEX idx_ais_ship_time ON ais_points(mmsi, timestamp)"
            )
            stats = IngestStats(  # 创建导入统计信息
                source_path=str(source),  # 源文件路径
                cache_path=str(self.db_path),  # 缓存文件路径
                raw_rows=raw_rows,  # 原始总行数
                valid_rows=valid_rows,  # 有效行数
                invalid_rows=invalid_rows,  # 无效行数
                ship_count=connection.execute(  # 查询不同MMSI的数量
                    "SELECT COUNT(DISTINCT mmsi) FROM ais_points"
                ).fetchone()[0],
                elapsed_seconds=time.perf_counter() - started,  # 耗时
            )
            metadata = {  # 缓存元数据
                "fingerprint": fingerprint,  # 文件指纹
                "complete": "1",  # 缓存完整标记
                "stats": stats.model_dump_json(),  # 统计信息的JSON表示
            }
            connection.executemany(  # 批量插入元数据
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",  # SQL语句
                metadata.items(),  # 元数据键值对
            )
            connection.commit()  # 提交事务
            self.stats = stats  # 保存统计信息
            return stats  # 返回统计信息
        finally:
            connection.close()  # 确保关闭数据库连接

    def iter_ship_tracks(self) -> Iterator[tuple[ShipProfile, list[AISPoint]]]:  # 逐船遍历所有轨迹数据
        if not self.db_path:  # 如果数据库路径未设置
            raise RuntimeError("ingest() must be called before iter_ship_tracks()")  # 抛出运行时错误
        connection = sqlite3.connect(self.db_path)  # 连接SQLite数据库
        try:
            mmsi_rows = connection.execute(  # 查询所有不同的MMSI
                "SELECT DISTINCT mmsi FROM ais_points ORDER BY mmsi"  # 按MMSI排序
            )
            for (mmsi,) in mmsi_rows:  # 遍历每个MMSI
                rows = connection.execute(  # 查询该船的所有AIS点
                    """
                    SELECT timestamp, lat, lon, speed, course, status, quality_flags
                    FROM ais_points WHERE mmsi = ? ORDER BY timestamp
                    """,
                    (mmsi,),  # 参数化查询
                ).fetchall()  # 获取所有结果
                points = [  # 转换为AISPoint对象列表
                    AISPoint(
                        mmsi=mmsi,  # MMSI
                        timestamp=row[0],  # 时间戳
                        lat=row[1],  # 纬度
                        lon=row[2],  # 经度
                        speed=row[3],  # 航速
                        course=row[4],  # 航向
                        status=row[5],  # 船舶状态
                        quality_flags=json.loads(row[6] or "[]"),  # 质量标记
                    )
                    for row in rows  # 遍历数据库行
                ]
                yield ShipProfile(mmsi=mmsi), points  # 生成船舶概要+轨迹点
        finally:
            connection.close()  # 确保关闭数据库连接

    @staticmethod
    def _insert_batch(connection: sqlite3.Connection, batch: list[tuple]) -> None:  # 批量插入AIS数据点
        connection.executemany(  # 执行批量插入
            """
            INSERT INTO ais_points
            (mmsi, timestamp, lat, lon, speed, course, status, quality_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,  # 数据批次
        )

    @staticmethod
    def _cache_complete(db_path: Path, fingerprint: str) -> bool:  # 检查缓存是否完整且指纹匹配
        try:
            connection = sqlite3.connect(db_path)  # 连接数据库
            values = dict(connection.execute("SELECT key, value FROM metadata"))  # 读取元数据
            return values.get("complete") == "1" and values.get("fingerprint") == fingerprint  # 检查完整性和指纹
        except sqlite3.Error:  # SQLite错误
            return False  # 返回False
        finally:
            if "connection" in locals():  # 如果连接已创建
                connection.close()  # 关闭连接

    @staticmethod
    def _load_stats(db_path: Path) -> IngestStats:  # 从缓存加载统计信息
        connection = sqlite3.connect(db_path)  # 连接数据库
        try:
            raw = connection.execute(  # 查询统计信息
                "SELECT value FROM metadata WHERE key = 'stats'"  # 获取stats字段
            ).fetchone()  # 获取第一行
            if not raw:  # 如果没有数据
                raise ValueError("缓存缺少统计信息")  # 抛出值错误
            return IngestStats.model_validate_json(raw[0])  # 解析JSON并返回IngestStats对象
        finally:
            connection.close()  # 确保关闭连接