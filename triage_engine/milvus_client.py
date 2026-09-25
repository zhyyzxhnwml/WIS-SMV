"""
triage_engine/milvus_client.py — Milvus 向量数据库客户端
==========================================================

【这个文件做什么？】
  提供 Milvus 向量数据库的访问接口，支持两种运行模式：
  1. Lite 模式（推荐）：进程内嵌，零配置，数据存本地文件
  2. Server 模式：连接外部 Milvus 服务，适合生产环境

【核心功能】
  - 轨迹序列向量化：将变长轨迹序列转为固定 128 维向量
    - 方法：时间轴均匀采样 32 个点，每点取 [lat, lon, speed, course] 4 维
    - 不足 32 个点则 padding，超出则均匀采样
  - 相似轨迹检索：用 COSINE 距离度量
  - 集合管理：自动创建 schema 和索引

【环境变量】
  MILVUS_HOST (默认 localhost, Server 模式)
  MILVUS_PORT (默认 19530, Server 模式)
  MILVUS_DB_FILE (默认 ./milvus_lite.db, Lite 模式)

【为什么用 Milvus？】
  pgvector 存结构化特征向量（22 维），
  Milvus 存轨迹序列向量（128 维），两者互补。
  前者查"特征相似的案例"，后者查"轨迹形态相似的案例"。
"""

from __future__ import annotations

import json  # JSON 序列化/反序列化模块
import os  # 操作系统接口（环境变量、文件路径）
from datetime import datetime  # 日期时间处理
from typing import Any, Optional  # 类型注解

try:
    # 优先尝试 pymilvus 2.4+（自带 milvus-lite 内嵌模式）
    from pymilvus import MilvusClient as MilvusLiteClient  # Milvus Lite 客户端类
    from pymilvus import Collection, CollectionSchema, DataType, FieldSchema  # Milvus 集合/数据类型定义
    from pymilvus import connections, utility  # Milvus 连接管理和工具函数
    HAS_MILVUS = True  # pymilvus 可用标识（任意模式）
    HAS_LITE = True  # Milvus Lite 内嵌模式可用标识
except ImportError:
    HAS_LITE = False  # Milvus Lite 内嵌模式不可用
    try:
        from pymilvus import (
            Collection, CollectionSchema, DataType, FieldSchema,
            connections, utility,
        )
        HAS_MILVUS = True  # 仅有 Server 模式可用
    except ImportError:
        HAS_MILVUS = False  # pymilvus 完全不可用


COLLECTION_NAME = "trajectory_sequences"  # Milvus 中轨迹序列集合的名称
VECTOR_DIM = 128  # 轨迹序列向量的维度（32 点 × 4 维）


def track_to_vector(track_points: list[dict], n: int = 32) -> list[float]:
    """将轨迹序列转为固定长度向量。

    方法：对时间轴均匀采样 N 个点，每个点取 [lat, lon, speed, course] 4 维，
    组成 4N 维向量。不足 padding 0，超出截断采样。

    Args:
        track_points: 轨迹点列表（需含 lat, lon, speed, course 字段）
        n: 采样点数，默认 32 → 128 维向量

    Returns:
        128 维浮点向量
    """
    if not track_points:  # 空轨迹列表
        return [0.0] * (4 * n)  # 返回全零向量

    # 时间轴均匀插值采样
    if len(track_points) <= n:  # 不足采样点数时
        sampled = list(track_points)  # 复制所有轨迹点
        # 重复最后一个点 padding
        while len(sampled) < n:  # 补足到 n 个点
            sampled.append(track_points[-1])  # 用最后一个点填充
    else:  # 轨迹点数超过采样点数
        indices = [  # 计算均匀采样索引列表
            int(round(i * (len(track_points) - 1) / (n - 1)))  # 线性插值取整
            for i in range(n)  # 生成 n 个均匀分布的索引
        ]
        sampled = [track_points[i] for i in indices]  # 按索引取出采样点

    vector = []  # 初始化结果向量
    for pt in sampled:  # 遍历每个采样点
        vector.append(float(pt.get("lat", 0)))  # 添加纬度（默认 0）
        vector.append(float(pt.get("lon", 0)))  # 添加经度（默认 0）
        vector.append(min(float(pt.get("speed", 0)), 102.3))  # 添加速度（上限 102.3 节）
        vector.append(float(pt.get("course", 0) % 360))  # 添加航向（归一化到 0-360 度）
    return vector


class MilvusClient:
    """Milvus 向量数据库客户端。

    支持两种运行模式（自动检测）：
    1. Lite 模式（推荐）— 进程内嵌，零配置，`pip install pymilvus` 即可
       - MilvusLiteClient(db_file) — 数据存本地文件
       - 无需外部服务，适合开发和验证
    2. Server 模式 — 连接外部 Milvus 服务
       - 需要单独部署 Milvus（Docker/binary）
       - 适合生产环境

    环境变量配置：
        MILVUS_HOST (默认 localhost, 仅 Server 模式)
        MILVUS_PORT (默认 19530, 仅 Server 模式)
        MILVUS_DB_FILE (默认 ./milvus_lite.db, 仅 Lite 模式)
    """

    # Milvus 连接失效的错误特征（如容器重启后旧 gRPC channel 被关闭）
    _CONN_ERROR_MARKERS = (
        "closed channel",
        "channel is closed",
        "connection refused",
        "connection reset",
        "fail to connect",
    )

    def __init__(
        self,
        host: Optional[str] = None,  # Milvus 服务主机地址（Server 模式）
        port: int = 19530,  # Milvus 服务端口号（Server 模式，默认 19530）
        collection_name: str = COLLECTION_NAME,  # 集合名称（默认 trajectory_sequences）
        db_file: Optional[str] = None,  # 兼容参数（本项目不再用 Lite 模式，忽略）
        force_lite: bool = False,  # 兼容参数（本项目只支持 Server 模式，忽略）
    ):
        if not HAS_MILVUS:  # pymilvus 未安装
            raise ImportError(  # 抛出导入错误
                "pymilvus 未安装。运行: pip install 'pymilvus>=2.4'"
            )
        self.collection_name = collection_name  # 存储集合名称
        self._lite_client = None  # 初始化 Lite 客户端为 None

        # 本项目【只支持连接外部 Milvus（Docker）】，不再使用 Lite 模式。
        # 连接地址统一从 config.milvus_uri() 解析（优先级：MILVUS_URI 环境变量 > config 默认）。
        try:
            from config import milvus_uri as _cfg_uri
            cfg_uri = _cfg_uri()
        except Exception:
            cfg_uri = None

        # 优先用传入 host（兼容旧写法），否则用 config；仍拿不到则回退 localhost
        self.uri = None
        if host:
            self.uri = f"http://{host}:{port}"
        elif cfg_uri:
            self.uri = cfg_uri

        # Server 模式
        self.host = host or os.environ.get("MILVUS_HOST", "localhost")  # 从环境变量获取主机地址
        self.port = int(os.environ.get("MILVUS_PORT", str(port)))  # 从环境变量获取端口号
        self._mode = "server"  # 设置模式标识为 server
        self._collection: Optional[Collection] = None  # 初始化集合对象为 None
        self._connect_server()  # 连接到外部 Milvus 服务

    def _connect_server(self) -> None:
        """连接外部 Milvus 服务（Server 模式）。优先用统一配置的 uri（http 地址）。"""
        if self.uri:
            # 支持 http://host:port 直连（pymilvus 2.4+ 的 MilvusClient uri 方式）
            from pymilvus import MilvusClient as _MC
            self._mc = _MC(uri=self.uri)
            self._mode = "server"
            print(f"[Milvus] 已连接 {self.uri}")
            self._connect_orm_default()  # Collection()/utility 依赖 default 连接
            return
        # 兼容旧的方式：host/port 连接
        connections.connect(  # 建立 Milvus 连接
            alias="default",  # 连接别名（默认）
            host=self.host,  # 主机地址
            port=self.port,  # 端口号
        )

    def _connect_orm_default(self) -> None:
        """注册 ORM 的 default 连接（Collection()/utility 依赖它）；已存在则跳过。"""
        try:
            from pymilvus import connections
            if not connections.has_connection("default"):
                connections.connect(
                    alias="default",
                    host=self._uri_host(),
                    port=self._uri_port(),
                )
        except Exception as e:
            print(f"[Milvus] 注册 ORM default 连接失败: {e}")

    def _uri_host(self) -> str:
        """从 uri 解析 host（解析失败回退 self.host）。"""
        if self.uri:
            try:
                from urllib.parse import urlparse
                h = urlparse(self.uri).hostname
                if h:
                    return h
            except Exception:
                pass
        return self.host

    def _uri_port(self) -> int:
        """从 uri 解析 port（解析失败回退 self.port）。"""
        if self.uri:
            try:
                from urllib.parse import urlparse
                p = urlparse(self.uri).port
                if p:
                    return p
            except Exception:
                pass
        return self.port

    def _reconnect_orm(self) -> None:
        """连接失效（如 Milvus 重启）时重建 ORM default 连接。"""
        try:
            from pymilvus import connections
            connections.disconnect("default")
        except Exception:
            pass
        self._connect_orm_default()

    @property
    def _lite(self):
        return self._lite_client  # 兼容属性：本项目不用 Lite，恒为 None

    # ── 集合管理 ──

    def create_collection(self, drop_if_exists: bool = False) -> None:
        """创建轨迹序列集合（本项目只支持 Server 模式）。"""
        return self._create_collection_server(drop_if_exists)

    def _create_collection_lite(self, drop_if_exists: bool) -> None:
        """Lite 模式下创建集合。"""
        if self._lite.has_collection(self.collection_name):  # 集合已存在
            if drop_if_exists:  # 允许删除已有集合
                self._lite.drop_collection(self.collection_name)  # 删除已有集合
            else:  # 不允许删除
                print(f"  Milvus 集合 {self.collection_name} 已存在")
                return
        schema = MilvusLiteClient.create_schema(  # 创建集合 schema
            auto_id=False,  # 不自动生成 ID（手动指定）
            enable_dynamic_field=True,  # 启用动态字段
        )
        schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)  # 主键 ID 字段（字符串，最长 64）
        schema.add_field("mmsi", DataType.VARCHAR, max_length=16)  # MMSI 号字段（字符串，最长 16）
        schema.add_field("segment_id", DataType.VARCHAR, max_length=64)  # 轨迹分段 ID 字段
        schema.add_field("sequence_vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM)  # 轨迹序列向量字段（128 维浮点向量）
        schema.add_field("has_violation", DataType.BOOL)  # 是否违规字段（布尔）
        schema.add_field("violation_type", DataType.VARCHAR, max_length=32)  # 违规类型字段（字符串）
        schema.add_field("point_count", DataType.INT32)  # 轨迹点数（32 位整数）
        schema.add_field("duration_minutes", DataType.FLOAT)  # 轨迹时长（分钟，浮点）
        schema.add_field("metadata_json", DataType.VARCHAR, max_length=4096)  # 元数据 JSON（字符串，最长 4096）
        schema.add_field("created_at", DataType.INT64)  # 创建时间戳（64 位整数）

        index_params = self._lite.prepare_index_params()  # 准备索引参数
        index_params.add_index("sequence_vector", metric_type="COSINE",  # 为向量字段添加索引
                                index_type="IVF_FLAT", params={"nlist": 128})  # 使用 IVF_FLAT 索引，128 聚类中心

        self._lite.create_collection(  # 在 Milvus Lite 中创建集合
            collection_name=self.collection_name,  # 集合名称
            schema=schema,  # 字段 schema
            index_params=index_params,  # 索引参数
        )
        self._lite.load_collection(self.collection_name)  # 加载集合到内存
        print(f"[Milvus Lite] 集合 {self.collection_name} 创建完成")

    def _create_collection_server(self, drop_if_exists: bool) -> None:
        """Server 模式下创建集合。使用 MilvusClient(uri) 统一管理，不再用 ORM connections。"""
        # 用 MilvusClient 判断集合是否存在（与 _connect_server 里的 _mc 保持一致）
        mc = getattr(self, "_mc", None)
        if mc is None:
            # 老式 host/port 连接没有 _mc → 建临时客户端
            from pymilvus import MilvusClient as _MC
            mc = _MC(uri=self.uri) if self.uri else _MC(uri=f"http://{self.host}:{self.port}")
            self._mc = mc
        if mc.has_collection(self.collection_name):  # 集合已存在
            if drop_if_exists:  # 允许删除
                mc.drop_collection(self.collection_name)  # 删除已有集合
            else:  # 不允许删除
                print(f"  Milvus 集合 {self.collection_name} 已存在")
                return
        fields = [  # 定义字段列表
            FieldSchema(name="id", dtype=DataType.VARCHAR,  # 主键 ID 字段
                        max_length=64, is_primary=True),  # 字符串类型，最长 64，设为主键
            FieldSchema(name="mmsi", dtype=DataType.VARCHAR, max_length=16),  # MMSI 号字段
            FieldSchema(name="segment_id", dtype=DataType.VARCHAR,  # 轨迹分段 ID 字段
                        max_length=64),  # 字符串类型，最长 64
            FieldSchema(name="sequence_vector", dtype=DataType.FLOAT_VECTOR,  # 轨迹序列向量字段
                        dim=VECTOR_DIM),  # 128 维浮点向量
            FieldSchema(name="has_violation", dtype=DataType.BOOL),  # 是否违规字段
            FieldSchema(name="violation_type", dtype=DataType.VARCHAR,  # 违规类型字段
                        max_length=32),  # 字符串类型，最长 32
            FieldSchema(name="point_count", dtype=DataType.INT32),  # 轨迹点数
            FieldSchema(name="duration_minutes", dtype=DataType.FLOAT),  # 轨迹时长（分钟）
            FieldSchema(name="metadata_json", dtype=DataType.VARCHAR,  # 元数据 JSON
                        max_length=4096),  # 字符串类型，最长 4096
            FieldSchema(name="created_at", dtype=DataType.INT64),  # 创建时间戳
        ]
        schema = CollectionSchema(  # 创建集合 schema
            fields, description="AIS 轨迹序列向量 — 用于序列模式匹配"  # 字段列表和集合描述
        )
        # 用 MilvusClient 的统一 schema/index 方式创建（与 RegulationDB 一致，兼容 ORM Collection）
        _schema = mc.create_schema(auto_id=False, enable_dynamic_field=True)
        for f in fields:
            _kw = {"is_primary": f.is_primary}
            if f.dtype == DataType.VARCHAR:
                _kw["max_length"] = f.params.get("max_length", 256)
            if f.dtype == DataType.FLOAT_VECTOR:
                _kw["dim"] = f.params.get("dim", 128)
            _schema.add_field(f.name, f.dtype, **{k: v for k, v in _kw.items()})
        _idx = mc.prepare_index_params()
        _idx.add_index("sequence_vector", metric_type="COSINE", index_type="IVF_FLAT",
                       params={"nlist": 128})
        mc.create_collection(collection_name=self.collection_name, schema=_schema,
                             index_params=_idx)
        mc.load_collection(self.collection_name)
        print(f"[Milvus] 集合 {self.collection_name} 创建完成")

    @property
    def collection(self) -> Collection:
        """获取 Server 模式的集合对象。"""
        if self._mode != "server":
            raise RuntimeError("本项目只支持 Milvus Server 模式")
        if self._collection is None:  # 集合对象未初始化
            self._collection = Collection(name=self.collection_name)  # 获取集合
            self._collection.load()  # 加载到内存
        return self._collection  # 返回集合对象

    def _coll(self, name: str, *args, **kwargs):
        """调用 ORM Collection 的成员；连接失效（如 Milvus 重启）时重建 default 连接与
        集合对象并重试一次。支持方法（collection.search(...)）与属性（collection.num_entities）。"""
        def _invoke() -> Any:
            obj = getattr(self.collection, name)
            return obj(*args, **kwargs) if callable(obj) else obj

        try:
            return _invoke()
        except Exception as exc:
            if not any(m in str(exc).lower() for m in self._CONN_ERROR_MARKERS):
                raise
            self._reconnect_orm()  # 重建 ORM default 连接
            self._collection = None  # 重建集合对象
            return _invoke()

    # ── 写入 ──

    def insert_track(
        self,
        id: str,  # 轨迹记录唯一 ID
        mmsi: str,  # 船舶 MMSI 号
        track_points: list[dict],  # 轨迹点列表（含 lat/lon/speed/course）
        segment_id: Optional[str] = None,  # 轨迹分段 ID（可选）
        has_violation: bool = False,  # 是否违规（默认 False）
        violation_type: Optional[str] = None,  # 违规类型（可选）
        metadata: Optional[dict] = None,  # 额外元数据字典（可选）
    ) -> None:
        """插入一条轨迹序列（本项目只支持 Server 模式）。"""
        self._insert_track_server(  # 调用 Server 模式插入方法
            id, mmsi, track_points, segment_id,
            has_violation, violation_type, metadata,
        )

    def _build_track_vector(self, track_points: list[dict], id: str, mmsi: str,
                            segment_id: str, has_violation: bool,
                            violation_type: str,
                            metadata: dict) -> tuple[list[float], float]:
        """构建轨迹向量并计算时长。"""
        vector = track_to_vector(track_points)  # 将轨迹点转为 128 维向量
        duration = 0.0  # 初始化时长为 0
        if len(track_points) >= 2:  # 至少 2 个点才能计算时长
            try:
                t0 = datetime.fromisoformat(  # 解析第一个点的时间戳
                    track_points[0].get("timestamp", "").replace("Z", "+00:00")  # 替换 Z 为 UTC 时区
                )
                t1 = datetime.fromisoformat(  # 解析最后一个点的时间戳
                    track_points[-1].get("timestamp", "").replace("Z", "+00:00")
                )
                duration = (t1 - t0).total_seconds() / 60  # 计算时长（秒转分钟）
            except (ValueError, TypeError):  # 时间戳解析失败
                pass
        return vector, duration  # 返回向量和时长

    def _insert_track_lite(self, id, mmsi, track_points, segment_id,
                           has_violation, violation_type, metadata):
        """Lite 模式下插入轨迹。"""
        vector, duration = self._build_track_vector(  # 构建轨迹向量和时长
            track_points, id, mmsi, segment_id or id,  # segment_id 为空则用 id
            has_violation, violation_type or "", metadata or {},  # 默认空值处理
        )
        self._lite.insert(self.collection_name, [{  # 向 Lite 集合插入数据
            "id": id,  # 记录 ID
            "mmsi": mmsi,  # MMSI 号
            "segment_id": segment_id or id,  # 分段 ID
            "sequence_vector": vector,  # 轨迹序列向量
            "has_violation": has_violation,  # 是否违规
            "violation_type": violation_type or "",  # 违规类型（空字符串默认）
            "point_count": len(track_points),  # 轨迹点数
            "duration_minutes": duration,  # 轨迹时长（分钟）
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),  # 元数据 JSON 序列化
            "created_at": int(datetime.now().timestamp()),  # 当前时间戳（秒）
        }])

    def _insert_track_server(self, id, mmsi, track_points, segment_id,
                             has_violation, violation_type, metadata):
        """Server 模式下插入轨迹。"""
        vector, duration = self._build_track_vector(  # 构建轨迹向量和时长
            track_points, id, mmsi, segment_id or id,
            has_violation, violation_type or "", metadata or {},
        )
        entities = [  # 准备按列组织的数据实体列表
            [id], [mmsi], [segment_id or id], [vector],  # ID、MMSI、分段 ID、向量
            [has_violation], [violation_type or ""],  # 违规标记、违规类型
            [len(track_points)], [duration],  # 点数、时长
            [json.dumps(metadata or {}, ensure_ascii=False)],  # 元数据 JSON
            [int(datetime.now().timestamp())],  # 创建时间戳
        ]
        self._coll("insert", entities)  # 插入到 Server 端集合（连接失效自动重建重试）

    # ── 检索 ──

    def search_similar(
        self,
        track_points: list[dict],  # 查询用的轨迹点列表
        top_k: int = 5,  # 返回最相似的 K 条结果
        metric_type: str = "COSINE",  # 距离度量方式（默认余弦相似度）
    ) -> list[dict[str, Any]]:
        """搜索轨迹模式最相似的 K 条记录（本项目只支持 Server 模式）。"""
        return self._search_server(track_points, top_k, metric_type)

    def _search_lite(self, track_points, top_k) -> list[dict]:
        """Lite 模式下的相似轨迹搜索。"""
        vector = track_to_vector(track_points)  # 将查询轨迹转为向量
        self._lite.load_collection(self.collection_name)  # 加载集合到内存
        results = self._lite.search(  # 执行向量搜索
            collection_name=self.collection_name,  # 集合名称
            data=[vector],  # 查询向量（列表形式）
            limit=top_k,  # 返回数量上限
            output_fields=[  # 需要返回的字段列表
                "id", "mmsi", "segment_id",
                "has_violation", "violation_type",
                "point_count", "duration_minutes",
            ],
        )
        hits = []  # 初始化结果列表
        for result_group in results:  # 遍历每组搜索结果
            for hit in result_group:  # 遍历每条命中结果
                entity = hit.get("entity", {})  # 获取实体的字段数据
                hits.append({  # 添加到结果列表
                    "id": hit.get("id"),  # 记录 ID
                    "mmsi": entity.get("mmsi"),  # MMSI 号
                    "segment_id": entity.get("segment_id"),  # 分段 ID
                    "has_violation": entity.get("has_violation"),  # 是否违规
                    "violation_type": entity.get("violation_type"),  # 违规类型
                    "point_count": entity.get("point_count"),  # 轨迹点数
                    "duration_minutes": entity.get("duration_minutes"),  # 轨迹时长
                    "similarity": round(hit.get("distance", 0), 4),  # 相似度（保留 4 位小数）
                })
        return hits  # 返回搜索结果列表

    def _search_server(self, track_points, top_k, metric_type) -> list[dict]:
        """Server 模式下的相似轨迹搜索。"""
        vector = track_to_vector(track_points)  # 将查询轨迹转为向量
        self._coll("load")  # 加载集合到内存（连接失效自动重建重试）
        search_params = {  # 搜索参数
            "metric_type": metric_type,  # 距离度量方式
            "params": {"nprobe": 10},  # 搜索时探针数量（查询精度）
        }
        results = self._coll(  # 执行向量搜索（连接失效自动重建重试）
            "search",
            data=[vector],  # 查询向量
            anns_field="sequence_vector",  # 向量字段名
            param=search_params,  # 搜索参数
            limit=top_k,  # 返回数量上限
            output_fields=[  # 需要返回的字段列表
                "id", "mmsi", "segment_id",
                "has_violation", "violation_type",
                "point_count", "duration_minutes",
            ],
        )
        hits = []  # 初始化结果列表
        if results:  # 有搜索结果
            for hit in results[0]:  # 遍历第一条查询的结果列表
                hits.append({  # 添加到结果列表
                    "id": hit.id,  # 记录 ID
                    "mmsi": hit.entity.get("mmsi"),  # MMSI 号
                    "segment_id": hit.entity.get("segment_id"),  # 分段 ID
                    "has_violation": hit.entity.get("has_violation"),  # 是否违规
                    "violation_type": hit.entity.get("violation_type"),  # 违规类型
                    "point_count": hit.entity.get("point_count"),  # 轨迹点数
                    "duration_minutes": hit.entity.get("duration_minutes"),  # 轨迹时长
                    "similarity": round(hit.score, 4),  # 相似度（保留 4 位小数）
                })
        return hits  # 返回搜索结果列表

    def count(self) -> int:
        """获取集合中的记录总数。"""
        return self._coll("num_entities")  # Server 模式直接获取实体数量（连接失效自动重建重试）

    def drop(self) -> None:
        """删除集合。"""
        try:
            if getattr(self, "_mc", None) is not None:  # uri 直连客户端（自包含连接）
                self._mc.drop_collection(self.collection_name)
            else:
                utility.drop_collection(self.collection_name)  # 老式 ORM 连接
        except Exception:  # 捕获异常
            pass
        print(f"[Milvus] 集合 {self.collection_name} 已删除")

    def close(self) -> None:
        """关闭连接（Server 模式）。"""
        try:
            connections.disconnect("default")  # 断开默认连接
        except Exception:  # 捕获异常
            pass