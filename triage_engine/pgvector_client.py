"""
triage_engine/pgvector_client.py — PostgreSQL pgvector 客户端
===============================================================

【这个文件做什么？】
  提供 PostgreSQL + pgvector 扩展的数据库接口，用于：
  1. 案例存储：保存每个加速器处理案例的完整数据
  2. 特征检索：用余弦相似度查找特征最相似的历史案例
  3. 训练集管理：LLM 反馈回流后形成训练数据
  4. ML 反馈记录：对比 ML 预测 vs LLM 判定

【环境变量】
  PG_HOST (默认 localhost)
  PG_PORT (默认 5432)
  PG_DB (默认 ais_accelerator)
  PG_USER (默认 postgres)
  PG_PASSWORD (默认空)

【核心表结构】
  - cases: 案例主表（特征向量 + ML/LLM 结果）
  - training_set: 训练数据（从 LLM 反馈生成）
  - ml_feedback: ML vs LLM 对比记录

【数据库 vs 无数据库模式】
  如果 PostgreSQL 不可用，pgvector_client 初始化时会抛 ImportError，
  上层捕获后会自动降级为无数据库模式（仅 ML 模型 + Milvus Lite）。
"""

from __future__ import annotations

import json  # JSON 序列化/反序列化模块
import os  # 操作系统接口（环境变量、文件路径）
from datetime import datetime, timezone  # 日期时间与时区处理
from typing import Any, Optional  # 类型注解

try:
    import psycopg2  # PostgreSQL 数据库适配器
    import psycopg2.extras  # psycopg2 扩展功能（如 RealDictCursor）
    HAS_PG = True  # PostgreSQL 可用标识
except ImportError:
    HAS_PG = False  # PostgreSQL 不可用


class PGVectorClient:
    """PostgreSQL + pgvector 数据库客户端。

    环境变量配置：
        PG_HOST (默认 localhost)
        PG_PORT (默认 5432)
        PG_DB (默认 ais_accelerator)
        PG_USER (默认 postgres)
        PG_PASSWORD (默认空)
    """

    def __init__(
        self,
        host: Optional[str] = None,  # 数据库主机地址（可选，优先环境变量）
        port: int = 5432,  # 数据库端口号（默认 5432）
        dbname: str = "ais_accelerator",  # 数据库名称
        user: str = "postgres",  # 数据库用户名
        password: str = "",  # 数据库密码
        autocommit: bool = True,  # 是否自动提交事务（默认 True）
    ):
        if not HAS_PG:  # psycopg2 未安装
            raise ImportError(  # 抛出导入错误
                "psycopg2 未安装。运行: pip install psycopg2-binary"
            )
        # 统一从 config 读取连接参数（环境变量可覆盖），保持与 Milvus 一致的中枢配置。
        try:
            from config import pg_config
            _cfg = pg_config()
            self.host = host or _cfg.host
            self.port = int(_cfg.port)
            self.dbname = _cfg.db
            self.user = _cfg.user
            self.password = _cfg.password
        except Exception:
            # config 不可用时回退到环境变量/入参（兼容旧行为）
            self.host = host or os.environ.get("PG_HOST", "localhost")
            self.port = int(os.environ.get("PG_PORT", str(port)))
            self.dbname = os.environ.get("PG_DB", dbname)
            self.user = os.environ.get("PG_USER", user)
            self.password = os.environ.get("PG_PASSWORD", password)
        self.autocommit = autocommit  # 存储自动提交设置
        self._conn: Optional[psycopg2.extensions.connection] = None  # 初始化数据库连接对象

    # ── 连接管理 ──

    @property
    def conn(self):
        """获取数据库连接对象（懒加载）。"""
        if self._conn is None or self._conn.closed:  # 连接未创建或已关闭
            self._conn = psycopg2.connect(  # 创建新的数据库连接
                host=self.host,  # 主机地址
                port=self.port,  # 端口号
                dbname=self.dbname,  # 数据库名
                user=self.user,  # 用户名
                password=self.password,  # 密码
            )
            if self.autocommit:  # 需要自动提交
                self._conn.autocommit = True  # 开启自动提交模式
        return self._conn  # 返回连接对象

    def close(self):
        """关闭数据库连接。"""
        if self._conn and not self._conn.closed:  # 连接存在且未关闭
            self._conn.close()  # 关闭连接
            self._conn = None  # 清空连接引用

    # ── 数据库初始化 ──

    def init_schema(self, schema_path: Optional[str] = None) -> None:
        """执行 schema.sql 建表（幂等：表已存在则跳过，不报错）。"""
        if schema_path is None:  # 未指定 schema 文件路径
            schema_path = os.path.join(  # 构造默认路径
                os.path.dirname(__file__), "schema.sql"  # 同目录下的 schema.sql
            )
        with open(schema_path, "r", encoding="utf-8") as f:  # 以只读方式打开 schema 文件
            sql = f.read()  # 读取 SQL 内容
        # 对 executescript：让每条语句独立失败但整体不中断（已存在的表跳过）
        try:
            with self.conn.cursor() as cur:  # 创建数据库游标
                cur.execute("SAVEPOINT init_schema_start")
                cur.execute(sql)  # 执行 SQL 建表语句
        except Exception:
            # 表已存在等幂等错误：逐个尝试通过的错误忽略，重要的表存在于否则抛。
            pass
        # 确认关键表存在
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.cases'), to_regclass('public.training_set')"
            )
            _has = cur.fetchone()
        print(f"[DB] Schema 检查完成 cases={'OK' if _has and _has[0] else 'MISSING'} "
              f"training_set={'OK' if _has and _has[1] else 'MISSING'}")

    # ── 案例写入 ──

    def insert_case(
        self,
        mmsi: str,  # 船舶 MMSI 号
        feature_vector: list[float],  # 64 维特征向量列表
        feature_json: dict,  # 特征明细字典（特征名→值）
        annotations: list[dict],  # 加速器标记列表
        annotation_counts: dict,  # 标记统计字典（如 {critical: N}）
        source_file: Optional[str] = None,  # 来源文件名（可选）
        segment_id: Optional[str] = None,  # 轨迹分段 ID（可选）
        event_id: Optional[str] = None,  # 事件 ID（可选）
    ) -> str:
        """插入一条加速器处理记录，返回 case UUID。"""
        import uuid  # UUID 生成模块
        case_id = str(uuid.uuid4())  # 生成新的 UUID 作为案例 ID
        with self.conn.cursor() as cur:  # 创建数据库游标
            cur.execute(  # 执行 INSERT SQL
                """
                INSERT INTO cases
                    (id, mmsi, source_file, segment_id, event_id,
                     feature_vector, feature_json, annotations, annotation_counts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (  # SQL 参数绑定
                    case_id, mmsi, source_file, segment_id, event_id,
                    feature_vector,  # 特征向量（pgvector 类型）
                    json.dumps(feature_json, ensure_ascii=False),  # 特征明细 JSON
                    json.dumps(annotations, ensure_ascii=False),  # 标记列表 JSON
                    json.dumps(annotation_counts, ensure_ascii=False),  # 标记统计 JSON
                ),
            )
        return case_id  # 返回新创建的案例 UUID

    def update_ml_result(
        self, case_id: str, prediction: str, confidence: float
    ) -> None:
        """更新案例的 ML 预测结果。"""
        with self.conn.cursor() as cur:  # 创建数据库游标
            cur.execute(  # 执行 UPDATE SQL
                "UPDATE cases SET ml_prediction = %s, ml_confidence = %s WHERE id = %s",
                (prediction, confidence, case_id),  # 预测类别、置信度、案例 ID
            )

    def update_llm_result(
        self,
        case_id: str,  # 案例 ID
        llm_result: dict,  # LLM 输出结果字典
        has_violation: Optional[bool] = None,  # 是否违规（可选覆盖）
        violation_type: Optional[str] = None,  # 违规类型（可选覆盖）
    ) -> None:
        """更新案例的 LLM 判定结果。"""
        llm_agreement = None  # 初始化 LLM 与 ML 一致标志
        if "agrees_with_ml_triage" in llm_result:  # LLM 结果中包含一致判断
            llm_agreement = llm_result["agrees_with_ml_triage"]  # 提取一致标志
        with self.conn.cursor() as cur:  # 创建数据库游标
            cur.execute(  # 执行 UPDATE SQL
                """
                UPDATE cases
                SET llm_result = %s,
                    llm_agreement = %s,
                    llm_feedback = %s,
                    has_violation = COALESCE(%s, has_violation),
                    violation_type = COALESCE(%s, violation_type)
                WHERE id = %s
                """,
                (  # SQL 参数绑定
                    json.dumps(llm_result, ensure_ascii=False),  # LLM 完整结果 JSON
                    llm_agreement,  # LLM 与 ML 一致标志
                    json.dumps(llm_result.get("feedback_for_ml", {}),  # LLM 对 ML 的反馈 JSON
                               ensure_ascii=False),
                    has_violation,  # 是否违规（可选）
                    violation_type,  # 违规类型（可选）
                    case_id,  # 案例 ID
                ),
            )

    # ── 相似案例检索 ──

    def search_similar(
        self,
        feature_vector: list[float],  # 查询用的特征向量
        top_k: int = 5,  # 返回最相似的 K 个案例
        min_confidence: float = 0.0,  # 最低置信度阈值（默认 0，不限）
    ) -> list[dict[str, Any]]:
        """用余弦相似度查找特征最相似的 K 个案例。"""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # 创建字典游标
            cur.execute(  # 执行相似度查询 SQL
                """
                SELECT
                    c.id, c.mmsi, c.ml_prediction, c.ml_confidence,
                    c.has_violation, c.violation_type,
                    c.created_at,
                    1 - (c.feature_vector <=> %s::vector) AS similarity
                FROM cases c
                WHERE c.ml_prediction IS NOT NULL
                  AND c.ml_confidence >= %s
                ORDER BY c.feature_vector <=> %s::vector
                LIMIT %s
                """,
                (feature_vector, min_confidence, feature_vector, top_k),  # 参数：向量、置信度阈值、向量、返回数
            )
            rows = cur.fetchall()  # 获取所有查询结果
            return [  # 转换为字典列表返回
                {
                    "id": r["id"],  # 案例 ID
                    "mmsi": r["mmsi"],  # MMSI 号
                    "ml_prediction": r["ml_prediction"],  # ML 预测类别
                    "ml_confidence": float(r["ml_confidence"]) if r["ml_confidence"] else None,  # ML 置信度
                    "has_violation": r["has_violation"],  # 是否违规
                    "violation_type": r["violation_type"],  # 违规类型
                    "similarity": float(r["similarity"]),  # 余弦相似度
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,  # 创建时间
                }
                for r in rows  # 遍历每行结果
            ]

    # ── 训练集管理 ──

    def add_to_training_set(
        self,
        case_id: str,  # 关联的案例 ID
        label: str,  # 目标变量标签：obvious_issue/has_risk/needs_review/normal
        label_source: str = "llm",  # 标签来源（默认 LLM）
        label_confidence: float = 1.0,  # 标签置信度（默认 1.0）
        weight: float = 1.0,  # 样本权重（默认 1.0）
    ) -> str:
        """将案例加入训练集。"""
        import uuid  # UUID 生成模块
        tid = str(uuid.uuid4())  # 生成训练记录 UUID
        with self.conn.cursor() as cur:  # 创建数据库游标
            # 先取特征向量
            cur.execute(  # 查询案例特征向量
                "SELECT feature_vector FROM cases WHERE id = %s",
                (case_id,),  # 按案例 ID 查询
            )
            row = cur.fetchone()  # 获取第一行结果
            if not row:  # 案例不存在
                raise ValueError(f"Case {case_id} 不存在")  # 抛出异常
            vector = row[0]  # 提取特征向量

            cur.execute(  # 插入训练集记录
                """
                INSERT INTO training_set
                    (id, case_id, feature_vector, label, label_source,
                     label_confidence, sample_weight)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (tid, case_id, vector, label, label_source,  # ID、案例 ID、向量、标签、来源
                 label_confidence, weight),  # 标签置信度、样本权重
            )
        return tid  # 返回训练记录 UUID

    def get_unused_training_data(
        self, limit: int = 1000,  # 最大返回条数（默认 1000）
    ) -> list[dict[str, Any]]:
        """获取未用于训练的标注数据。"""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # 创建字典游标
            cur.execute(  # 查询未使用的训练数据
                """
                SELECT t.id, t.case_id, t.feature_vector, t.label,
                       t.label_confidence, t.sample_weight
                FROM training_set t
                WHERE t.used_in_training = FALSE
                ORDER BY t.sample_weight DESC, t.created_at ASC
                LIMIT %s
                """,
                (limit,),  # 返回数量限制
            )
            rows = cur.fetchall()  # 获取所有结果
            return [  # 转换为字典列表
                {
                    "id": r["id"],  # 训练记录 ID
                    "case_id": r["case_id"],  # 案例 ID
                    "feature_vector": list(r["feature_vector"]),  # 特征向量
                    "label": r["label"],  # 标签
                    "confidence": float(r["label_confidence"]),  # 标签置信度
                    "weight": float(r["sample_weight"]),  # 样本权重
                }
                for r in rows  # 遍历每行结果
            ]

    def mark_training_data_used(self, ids: list[str]) -> None:
        """标记训练数据已使用。"""
        if not ids:  # ID 列表为空
            return  # 直接返回
        with self.conn.cursor() as cur:  # 创建数据库游标
            cur.execute(  # 更新标记
                "UPDATE training_set SET used_in_training = TRUE WHERE id = ANY(%s)",
                (ids,),  # ID 列表参数
            )

    # ── ML 反馈记录 ──

    def record_feedback(
        self,
        case_id: str,  # 案例 ID
        ml_prediction: str,  # ML 预测类别
        ml_confidence: float,  # ML 预测置信度
        llm_result: dict,  # LLM 结果字典
    ) -> str:
        """记录 ML vs LLM 的比较结果。"""
        import uuid  # UUID 生成模块
        fid = str(uuid.uuid4())  # 生成反馈记录 UUID

        llm_violation = llm_result.get("violation_detected", False)  # LLM 是否检测到违规
        llm_conf = llm_result.get("confidence", 0.0)  # LLM 判定置信度

        # 判断一致性与错误类型
        agreement = self._check_agreement(ml_prediction, ml_confidence,  # 检查 ML 与 LLM 是否一致
                                           llm_violation, llm_conf)
        error_type = self._classify_error(  # 分类错误类型
            ml_prediction, ml_confidence, llm_violation, llm_conf
        )

        llm_text = json.dumps(  # 构建 LLM 结果摘要文本
            {
                "violation_detected": llm_violation,  # 违规检测结果
                "violation_type": llm_result.get("violation_type"),  # 违规类型
                "confidence": llm_conf,  # 置信度
                "recommended_action": llm_result.get("recommended_action"),  # 推荐操作
            },
            ensure_ascii=False,
        )

        with self.conn.cursor() as cur:  # 创建数据库游标
            cur.execute(  # 插入反馈记录
                """
                INSERT INTO ml_feedback
                    (id, case_id, ml_prediction, ml_confidence,
                     llm_result_text, llm_confidence,
                     agreement, error_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (fid, case_id, ml_prediction, ml_confidence,  # ID、案例 ID、ML 预测、ML 置信度
                 llm_text, llm_conf, agreement, error_type),  # LLM 摘要、LLM 置信度、一致标志、错误类型
            )
        return fid  # 返回反馈记录 UUID

    def get_pending_review(self, limit: int = 50) -> list[dict]:
        """获取待人工复核的反馈记录。"""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # 创建字典游标
            cur.execute(  # 从待复核视图查询
                "SELECT * FROM v_pending_review LIMIT %s", (limit,)  # 返回数量限制
            )
            return [  # 转换为字典列表
                dict(r) for r in cur.fetchall()  # 将每行转为字典
            ]

    # ── 内部方法 ──

    @staticmethod
    def _check_agreement(
        ml_prediction: str,  # ML 预测类别
        ml_confidence: float,  # ML 预测置信度
        llm_violation: bool,  # LLM 是否判定违规
        llm_confidence: float,  # LLM 判定置信度
    ) -> bool:
        """判断 ML 路由与 LLM 结论是否一致。"""
        if ml_prediction in ("obvious_issue", "has_risk", "needs_review"):  # ML 认为有问题
            ml_thinks_violation = True  # ML 认为违规
        else:  # ML 认为正常
            ml_thinks_violation = False  # ML 认为未违规
        return ml_thinks_violation == llm_violation  # 返回是否一致

    @staticmethod
    def _classify_error(
        ml_prediction: str,  # ML 预测类别
        ml_confidence: float,  # ML 预测置信度
        llm_violation: bool,  # LLM 是否判定违规
        llm_confidence: float,  # LLM 判定置信度
    ) -> Optional[str]:
        """分类 ML 的错误类型。"""
        if ml_prediction == "normal":  # ML 判为正常
            if llm_violation:  # LLM 判为违规
                return "FN"  # 漏报（False Negative）
        elif ml_prediction in ("obvious_issue", "has_risk"):  # ML 判为明显问题或有风险
            if not llm_violation:  # LLM 判为未违规
                return "FP"  # 误报（False Positive）
        if ml_prediction == "needs_review":  # ML 无法判定
            return None  # 不确定，不归类
        return "correct"  # 默认正确