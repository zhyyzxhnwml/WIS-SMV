"""
llm_layer/feedback.py — 反馈闭环 + 自动增量训练
=================================================

【这个文件做什么？】
  将 LLM 的判定结果回流到 ML 训练集中，形成闭环：
  1. 比较 LLM 判定与 ML 预测的一致性
  2. 将 LLM 结果存入训练集（支持 PostgreSQL 或本地 JSON 文件）
  3. 分歧样本（LLM 不同意 ML）加高权重
  4. 累积足够新数据后触发 ML 模型增量更新

【为什么需要反馈闭环？】
  没有反馈闭环的话，ML 模型永远停在初始训练状态。
  有了闭环，每次 LLM 判定都在为 ML 模型产生新的训练数据，
  系统会越用越准。

【分歧处理】
  当 LLM 和 ML 结论不一致时：
  - 训练权重设为 3.0（是普通样本的 3 倍）
  - 标记为 needs_human_review（需要人工复核）
  - 累积 50 条以上新数据 → 自动触发增量训练
"""

from __future__ import annotations

import json  # JSON编解码模块
import os  # 操作系统接口（用于文件路径操作）
import time  # 时间模块（用于时间戳和冷却计时）
from pathlib import Path  # 路径操作类
from typing import Any, Optional  # 类型提示


# ── 文件型训练集存储（PostgreSQL 不可用时的降级方案） ──

TRAINING_DATA_FILE = "data/feedback_training_data.json"  # 训练数据本地JSON文件路径
RETRAIN_THRESHOLD = 50  # 累积多少条新数据后触发增量训练
RETRAIN_COOLDOWN = 3600  # 两次增量训练最小间隔（秒）


def _load_training_data() -> list[dict]:
    """从本地 JSON 文件加载累积的训练数据。"""
    path = Path(TRAINING_DATA_FILE)  # 训练数据文件路径对象
    if not path.exists():  # 文件不存在
        return []  # 返回空列表
    try:
        with open(path, "r", encoding="utf-8") as f:  # 以只读模式打开JSON文件
            return json.load(f)  # 反序列化JSON为Python列表
    except Exception:
        return []  # 加载失败则返回空列表


def _save_training_data(records: list[dict]) -> None:
    """保存训练数据到本地 JSON 文件。"""
    path = Path(TRAINING_DATA_FILE)  # 训练数据文件路径对象
    path.parent.mkdir(parents=True, exist_ok=True)  # 确保父目录存在
    with open(path, "w", encoding="utf-8") as f:  # 以写入模式打开JSON文件
        json.dump(records, f, ensure_ascii=False, indent=2)  # 序列化并写入JSON文件


def _add_training_record(
    feature_vector: list[float],  # 特征向量（数值列表）
    label: str,  # 训练标签（如 obvious_issue / normal 等）
    weight: float,  # 训练权重（分歧样本为3.0，普通为1.0）
    ml_prediction: str,  # ML模型的预测结果
    llm_confidence: float,  # LLM判定的置信度
    agreement: bool,  # LLM与ML是否一致
) -> None:
    """添加一条训练记录到本地文件。"""
    records = _load_training_data()  # 加载现有训练数据
    records.append({  # 添加新训练记录
        "feature_vector": feature_vector,  # 特征向量
        "label": label,  # 训练标签（字符串）
        "weight": weight,  # 训练权重（浮点数）
        "ml_prediction": ml_prediction,  # ML模型的预测结果
        "llm_confidence": round(llm_confidence, 4),  # LLM判定置信度（保留4位小数）
        "agreement": agreement,  # LLM与ML是否一致（布尔值）
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),  # 记录时间戳（字符串格式）
    })
    _save_training_data(records)  # 保存更新后的训练数据


def _get_last_retrain_time() -> float:
    """读取上次增量训练的时间戳。"""
    marker = Path("data/.last_retrain")  # 上次训练时间标记文件路径
    if not marker.exists():  # 文件不存在
        return 0.0  # 返回0（从未训练过）
    try:
        return float(marker.read_text().strip())  # 读取文件内容并转为浮点数时间戳
    except Exception:
        return 0.0  # 读取失败返回0


def _set_last_retrain_time() -> None:
    """记录本次增量训练的时间戳。"""
    Path("data/.last_retrain").parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在
    Path("data/.last_retrain").write_text(str(time.time()))  # 写入当前时间戳


def _do_incremental_training(model_path: str = "triage_model.txt") -> bool:
    """执行增量训练——从本地文件取数据，更新模型。"""
    records = _load_training_data()  # 加载训练数据
    if len(records) < 10:  # 数据太少
        return False  # 不足10条，跳过训练

    try:
        import numpy as np  # 数值计算库
        from triage_engine.model import TriageModel, LABELS  # 三分类模型类和标签列表
    except ImportError:
        return False  # 导入失败则跳过训练

    # 构造训练数据
    X_list, y_list, w_list = [], [], []  # 特征矩阵、标签向量、权重向量（初始化为空列表）
    for rec in records:  # 遍历每条训练记录
        fv = rec["feature_vector"]  # 提取特征向量
        # 补齐 64 维
        if len(fv) == 22:  # 旧格式数据（22维）
            fv = fv + [0.0] * 42  # 22维补齐到64维（填充0）
        try:
            label_idx = LABELS.index(rec["label"])  # 将标签文本映射为索引
        except ValueError:
            continue  # 标签不在已知列表中则跳过
        X_list.append(fv)  # 添加特征向量到列表
        y_list.append(label_idx)  # 添加标签索引到列表
        w_list.append(rec.get("weight", 1.0))  # 添加权重（默认为1.0）

    if len(X_list) < 10:  # 有效数据不足10条
        return False  # 跳过训练

    X = np.array(X_list, dtype=np.float32)  # 转换为NumPy浮点数矩阵
    y = np.array(y_list, dtype=np.int32)  # 转换为NumPy整数向量
    w = np.array(w_list, dtype=np.float32)  # 转换为NumPy浮点数权重向量

    # 创建模型（加载已有模型或从头训练）
    model = TriageModel(model_path=model_path)  # 创建或加载三分类模型

    if model.is_fitted:  # 如果模型已训练过
        # 增量更新
        model.incremental_update(  # 执行增量训练
            X, y,  # 特征矩阵和标签向量
            sample_weight=w,  # 样本权重向量
            num_boost_round=50,  # 增量训练的迭代轮数
            learning_rate=0.02,  # 增量训练的学习率（较小值防止灾难性遗忘）
        )
        print(f"\n  [反馈] 增量训练完成: {len(X)} 条数据, "
              f"模型现在 {model.model.num_trees() if model.model else 0} 棵树")
    else:  # 模型未训练过
        # 初始训练（首次没有模型时）
        from triage_engine.train import train_initial  # 导入初始训练函数
        # 合并已有的训练数据
        model = train_initial(  # 执行初始训练
            output_path=model_path,  # 模型输出路径
            n_samples=max(500, len(X)),  # 合成样本数（至少500）
        )
        print(f"\n  [反馈] 初始训练完成: {len(X)} 条真实 + 合成数据")

    # 清空已使用的训练数据
    _save_training_data([])  # 保存空列表，清空已使用的训练数据
    _set_last_retrain_time()  # 记录本次训练时间
    return True  # 返回训练成功


def check_and_retrain(model_path: str = "triage_model.txt", force: bool = False) -> bool:
    """检查是否需要增量训练，需要则执行。

    Args:
        model_path: 模型文件路径
        force: 强制训练（忽略冷却时间）

    Returns:
        是否执行了训练
    """
    records = _load_training_data()  # 加载训练数据
    count = len(records)  # 新数据条数

    if count < RETRAIN_THRESHOLD and not force:  # 未达到阈值且非强制
        return False  # 跳过

    last = _get_last_retrain_time()  # 上次训练时间
    elapsed = time.time() - last  # 距上次训练的间隔时间（秒）

    if elapsed < RETRAIN_COOLDOWN and not force:  # 冷却期内且非强制
        print(f"  [反馈] 距上次训练仅 {elapsed:.0f}s (< {RETRAIN_COOLDOWN}s 冷却), 跳过")
        return False  # 跳过

    print(f"\n  [反馈] 触发增量训练: {count} 条新标注数据等待学习")
    return _do_incremental_training(model_path)  # 执行增量训练


# ── 反馈闭环 ──


class FeedbackLoop:
    """LLM → ML 反馈闭环。

    职责：
    1. 比较 LLM 判定与 ML 预测的一致性
    2. 将 LLM 结果存入训练集（数据库优先，文件降级）
    3. 触发 ML 模型的增量更新
    """

    def __init__(
        self,
        pgvector_client: Optional[Any] = None,  # PGVector数据库客户端（可选）
        triage_model: Optional[Any] = None,  # 三分类模型实例（可选）
        model_path: str = "triage_model.txt",  # 模型文件路径
        auto_retrain: bool = True,  # 是否自动触发增量训练
    ):
        self.pgvector = pgvector_client  # PGVector数据库客户端
        self.triage_model = triage_model  # 三分类模型实例
        self.model_path = model_path  # 模型文件路径
        self.auto_retrain = auto_retrain  # 自动训练开关
        self._pending_count = 0  # 待处理的新数据计数（文件模式下使用）

    def process_result(
        self,
        case_id: str,  # 案例 ID
        ml_prediction: str,  # ML 路由预测结果（如 obvious_issue / normal 等）
        ml_confidence: float,  # ML 置信度
        llm_result: dict,  # LLM 判定结果字典
        feature_vector: Optional[list[float]] = None,  # 特征向量（文件模式必需）
    ) -> dict:
        """处理一次 LLM 判定结果。

        流程：
        1. 记录 ML vs LLM 反馈
        2. 如果 LLM 同意 ML → 加入训练集（正常权重）
        3. 如果 LLM 不同意 ML → 加入训练集（高权重）+ 标记待复核
        4. 触发增量更新（如果累积足够不一致样本）

        Args:
            case_id: 案例 ID
            ml_prediction: ML 路由预测
            ml_confidence: ML 置信度
            llm_result: LLM 判定结果
            feature_vector: 特征向量（文件模式必需）

        Returns:
            处理结果字典
        """
        result = {  # 处理结果字典
            "case_id": case_id,  # 案例 ID
            "agreement": None,  # 一致性状态（初始为None）
            "added_to_training": False,  # 是否已添加到训练集
            "triggered_update": False,  # 是否触发了增量更新
            "needs_human_review": False,  # 是否需要人工复核
        }

        llm_violation = llm_result.get("violation_detected", False)  # LLM判定的违规标记
        ml_thinks_issue = ml_prediction in ("obvious_issue", "has_risk", "needs_review")  # ML认为有问题的路由类型
        agreement = (ml_thinks_issue == llm_violation)  # LLM与ML结论是否一致
        result["agreement"] = agreement  # 记录一致性

        # 确定训练集标签
        label = self._llm_to_label(llm_result, ml_prediction)  # 将LLM结果映射为训练标签
        weight = 3.0 if not agreement else 1.0  # 分歧样本权重3倍于普通样本

        # 标记需人工复核（分歧样本）
        if not agreement:  # LLM与ML结论不一致
            result["needs_human_review"] = True  # 标记为需要人工复核

        # ── 存入训练集 ──
        if self.pgvector:  # PostgreSQL 模式
            try:
                self.pgvector.add_to_training_set(  # 添加到PGVector训练集
                    case_id=case_id,  # 案例 ID
                    label=label,  # 训练标签
                    label_source="llm",  # 标签来源（LLM判定）
                    label_confidence=llm_result.get("confidence", 0.5),  # 标签置信度
                    weight=weight,  # 样本权重
                )
                result["added_to_training"] = True  # 标记已添加到训练集

                # 记录反馈
                self.pgvector.record_feedback(  # 记录ML vs LLM反馈
                    case_id=case_id,  # 案例 ID
                    ml_prediction=ml_prediction,  # ML预测结果
                    ml_confidence=ml_confidence,  # ML置信度
                    llm_result=llm_result,  # LLM判定结果
                )

                # 更新 LLM 结果
                self.pgvector.update_llm_result(  # 更新数据库中的LLM判定结果
                    case_id=case_id,  # 案例 ID
                    llm_result=llm_result,  # LLM判定结果
                    has_violation=llm_violation,  # 是否违规
                    violation_type=llm_result.get("violation_type"),  # 违规类型
                )
            except Exception:  # 数据库操作异常
                # PG 不可用，降级到文件
                pass

        # ── 文件模式降级 ──
        if not result["added_to_training"] and feature_vector:  # 未成功添加到数据库且有特征向量
            _add_training_record(  # 添加训练记录到本地文件
                feature_vector=feature_vector,  # 特征向量
                label=label,  # 训练标签
                weight=weight,  # 样本权重
                ml_prediction=ml_prediction,  # ML预测结果
                llm_confidence=llm_result.get("confidence", 0),  # LLM置信度
                agreement=agreement,  # 一致性标志
            )
            result["added_to_training"] = True  # 标记已添加到训练集
            self._pending_count += 1  # 待处理计数加1

        return result  # 返回处理结果

    def maybe_retrain(self, force: bool = False) -> bool:
        """检查并触发增量训练。

        在 PostgreSQL 模式下检查 should_retrain()，
        在文件模式下直接 check_and_retrain()。
        """
        if self.pgvector:  # 数据库模式
            if self.should_retrain() or force:  # 检查是否需要训练
                return self._trigger_retrain_from_db()  # 从数据库触发训练
            return False  # 不需要训练
        else:  # 文件模式
            return check_and_retrain(self.model_path, force=force)  # 文件模式触发训练

    def _llm_to_label(self, llm_result: dict, ml_prediction: str = "") -> str:
        """将 LLM 判定结果映射为训练集标签。"""
        violation = llm_result.get("violation_detected", False)  # 是否检测到违规
        confidence = llm_result.get("confidence", 0)  # LLM判定置信度

        if violation and confidence >= 0.85:  # 高置信度违规
            return "obvious_issue"  # 明显问题
        elif violation and confidence >= 0.6:  # 中置信度违规
            return "has_risk"  # 有风险
        elif not violation and confidence >= 0.85:  # 高置信度无违规
            return "normal"  # 正常
        elif not violation:  # 低置信度无违规
            return "needs_review"  # 需审查
        else:  # 其他情况
            return "has_risk"  # 默认归为有风险

    def should_retrain(self, min_new_samples: int = 100) -> bool:
        """判断是否需要触发增量训练。"""
        if not self.pgvector:  # 无数据库客户端
            return False  # 返回False
        try:
            unused = self.pgvector.get_unused_training_data(limit=1)  # 获取一条未使用的训练数据
            return len(unused) >= min_new_samples  # 未使用数据是否达到阈值
        except Exception:  # 查询异常
            return False  # 返回False

    def _trigger_retrain_from_db(self) -> bool:
        """从 PostgreSQL 触发增量训练。"""
        if not self.pgvector:  # 无数据库客户端
            return False  # 返回False
        try:
            from triage_engine.train import incremental_train  # 导入增量训练函数
            incremental_train(  # 从数据库触发增量训练
                model_path=self.model_path,  # 模型文件路径
                db_client=self.pgvector,  # 数据库客户端
                num_boost_round=50,  # 增量训练的迭代轮数
                learning_rate=0.02,  # 增量训练的学习率
            )
            return True  # 训练成功
        except Exception as e:  # 异常对象
            print(f"  [反馈] 增量训练失败: {e}")
            return False  # 训练失败
