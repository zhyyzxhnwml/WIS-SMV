"""
ais_accelerator/cache.py — 分析结果缓存
=========================================

【这个文件做什么？】
  对 Agent/LLM 的分析结果做缓存，避免相同输入重复调用。
  缓存 key 融合了 agent_input + rule_version + model_version，
  任一变化都会导致缓存失效。

【为什么需要缓存？】
  LLM 调用每次 3-8 秒，如果 100 艘船中有 30 艘完全相同的情况，
  缓存能节省 90-240 秒的处理时间。

【实现】
  底层使用 SQLite，key 为 SHA256 hash，value 为 JSON 序列化的结果。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import hashlib  # 哈希计算模块
import json  # JSON序列化模块
import sqlite3  # SQLite数据库模块
from datetime import datetime, timezone  # 日期时间处理
from pathlib import Path  # 路径处理模块
from typing import Optional  # 可选类型


def build_cache_key(  # 根据输入构建缓存键（SHA256哈希）
    agent_input: dict,  # Agent输入数据
    rule_version: str,  # 规则版本号
    model_version: str,  # 模型版本号
) -> str:
    payload = json.dumps(  # 序列化为JSON字符串
        {
            "agent_input": agent_input,  # Agent输入
            "rule_version": rule_version,  # 规则版本
            "model_version": model_version,  # 模型版本
        },
        ensure_ascii=False,  # 不转义非ASCII字符
        sort_keys=True,  # 按键排序确保一致性
        default=str,  # 不可序列化类型的转换函数
    ).encode("utf-8")  # 编码为UTF-8字节串
    return hashlib.sha256(payload).hexdigest()  # 返回SHA256哈希值


class AnalysisResultCache:  # 分析结果缓存类（基于SQLite）
    def __init__(self, path: Path):  # 初始化缓存，指定数据库文件路径
        self.path = path  # SQLite数据库文件路径
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 确保数据库目录存在
        connection = sqlite3.connect(self.path)  # 连接SQLite数据库
        try:
            connection.execute(  # 创建结果缓存表（如果不存在）
                """
                CREATE TABLE IF NOT EXISTS results (
                    cache_key TEXT PRIMARY KEY,  -- 缓存键（SHA256哈希）
                    result_json TEXT NOT NULL,  -- 缓存结果（JSON格式）
                    created_at TEXT NOT NULL  -- 创建时间（ISO格式）
                )
                """
            )
            connection.commit()  # 提交事务
        finally:
            connection.close()  # 确保关闭连接

    def get(self, cache_key: str) -> Optional[dict]:  # 根据缓存键获取缓存结果
        connection = sqlite3.connect(self.path)  # 连接SQLite数据库
        try:
            row = connection.execute(  # 查询缓存
                "SELECT result_json FROM results WHERE cache_key = ?", (cache_key,)  # 按缓存键查询
            ).fetchone()  # 获取第一行结果
            return json.loads(row[0]) if row else None  # 返回解析后的字典或None
        finally:
            connection.close()  # 确保关闭连接

    def put(self, cache_key: str, result: dict) -> None:  # 将结果存入缓存
        connection = sqlite3.connect(self.path)  # 连接SQLite数据库
        try:
            connection.execute(  # 插入或替换缓存记录
                """
                INSERT OR REPLACE INTO results(cache_key, result_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    cache_key,  # 缓存键
                    json.dumps(result, ensure_ascii=False, default=str),  # JSON序列化结果
                    datetime.now(timezone.utc).isoformat(),  # 当前UTC时间
                ),
            )
            connection.commit()  # 提交事务
        finally:
            connection.close()  # 确保关闭连接

if __name__ == "__main__":  # 测试代码
    
    key = build_cache_key({"input": "test"}, "v1", "model1")  # 构建缓存键
    
    print(key)  # 获取并打印缓存结果