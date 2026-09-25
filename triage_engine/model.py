"""
triage_engine/model.py — LightGBM Triage 模型  sklearn 版
===============================================

【这个文件做什么？】
  封装 LightGBM 多分类模型，提供：
  - 训练（初始/增量更新）
  - 预测（单条/批量，含置信度）
  - 模型持久化（保存/加载 + 特征名元数据）
  - 评估（准确率/精确率/召回率/F1）
  - 特征重要性分析

【四分类标签】
  - normal (0): 正常 — 置信度够高时直接路由为"正常"
  - obvious_issue (1): 明显违规 — 置信度够高时直接路由为"明显问题"
  - has_risk (2): 存在风险 — 需仔细审查
  - needs_review (3): 无法判定 — 需全面审查

【设计要点】
  - 初始训练使用合成数据 + 真实数据（如果有）
  - 增量更新：从当前模型继续训练，适合在线学习场景
  - 特征维度 64 维：22 维实际特征 + 42 维预留扩展
"""

from __future__ import annotations

import json  # JSON 序列化/反序列化模块
import os  # 操作系统接口（文件路径、文件存在检查）
import tempfile  # 临时文件模块
from pathlib import Path  # 跨平台路径操作
from typing import Any, Optional  # 类型注解

import numpy as np  # 数值计算库

try:
    import lightgbm as lgb  # LightGBM 梯度提升框架
    HAS_LGB = True  # LightGBM 可用标识
except ImportError:
    HAS_LGB = False  # LightGBM 不可用


# 四分类标签
LABELS = ["normal", "obvious_issue", "has_risk", "needs_review"]  # 四分类标签名称列表


class TriageModel:
    """AIS 三层级联系统的 ML Triage 模型。

    职责：
    - 接收加速器输出的 64 维特征向量
    - 输出来自 {normal, obvious_issue, has_risk, needs_review} 的四分类概率
    - 支持初始训练和增量更新
    - 自带特征重要性分析
    """

    def __init__(
        self,
        model_path: Optional[str] = None,  # 模型文件路径（可选，加载已有模型）
        params: Optional[dict] = None,  # LightGBM 训练参数（可选，覆盖默认参数）
    ):
        if not HAS_LGB:  # LightGBM 未安装
            raise ImportError(
                "lightgbm 未安装。运行: pip install 'lightgbm>=4.0' numpy"
            )
        self.model_path = model_path  # 模型文件路径
        self.model: Optional[lgb.Booster] = None  # LightGBM Booster 模型对象
        self.feature_names: list[str] = []  # 特征名称列表
        self._params = params or self._default_params()  # 训练参数（默认或自定义）
        self._is_fitted = False  # 模型是否已训练

        if model_path and os.path.exists(model_path):  # 提供了模型路径且文件存在
            self.load(model_path)  # 加载已有模型

    # ── 默认参数 ──

    @staticmethod
    def _default_params() -> dict:
        """返回 LightGBM 默认训练参数。"""
        return {
            "objective": "multiclass",  # 多分类目标函数
            "num_class": len(LABELS),  # 分类数量（4 类）
            "metric": "multi_logloss",  # 评估指标：多分类对数损失
            "boosting_type": "gbdt",  # 提升类型：梯度提升决策树
            "num_leaves": 31,  # 每棵树的叶子节点数
            "max_depth": -1,  # 树的最大深度（-1 表示不限制）
            "learning_rate": 0.05,  # 学习率
            "feature_fraction": 0.8,  # 每轮迭代随机采样特征比例
            "bagging_fraction": 0.8,  # 每轮迭代随机采样数据比例
            "bagging_freq": 5,  # 每隔多少轮执行一次 bagging
            "lambda_l1": 0.1,  # L1 正则化系数
            "lambda_l2": 0.1,  # L2 正则化系数
            "min_data_in_leaf": 20,  # 每个叶子节点的最少样本数
            "verbose": -1,  # 日志输出级别（-1 表示不输出）
            "num_threads": 2,  # 并行线程数
        }

    # ── 初始训练 ──

    def train(
        self,
        X: np.ndarray,  # 特征矩阵，shape (n_samples, n_features)
        y: np.ndarray,  # 标签数组，0=normal, 1=obvious_issue, 2=has_risk, 3=needs_review
        feature_names: Optional[list[str]] = None,  # 特征名列表（可选）
        valid_sets: Optional[tuple[np.ndarray, np.ndarray]] = None,  # 验证集 (X_val, y_val)
        num_boost_round: int = 200,  # 最大迭代轮数（默认 200）
        early_stopping_rounds: int = 20,  # 早停轮数（验证集指标不改善时停止，默认 20）
        sample_weight: Optional[np.ndarray] = None,  # 样本权重数组（可选）
    ) -> dict:
        """初始训练模型。

        Args:
            X: 特征矩阵，shape (n_samples, n_features)
            y: 标签数组，0=normal, 1=obvious_issue, 2=has_risk, 3=needs_review
            feature_names: 特征名列表
            valid_sets: (X_val, y_val) 验证集
            num_boost_round: 最大迭代轮数
            early_stopping_rounds: 早停轮数
            sample_weight: 样本权重

        Returns:
            训练过程指标
        """
        if feature_names is None:  # 未提供特征名
            feature_names = [f"f{i}" for i in range(X.shape[1])]  # 自动生成特征名 f0, f1, ...
        self.feature_names = feature_names  # 存储特征名

        train_data = lgb.Dataset(  # 创建 LightGBM 训练数据集
            X, label=y,  # 特征矩阵和标签
            feature_name=feature_names,  # 特征名称
            weight=sample_weight,  # 样本权重
        )

        valid_data = None  # 初始化验证集为 None
        if valid_sets:  # 提供了验证集
            valid_data = lgb.Dataset(  # 创建验证数据集
                valid_sets[0], label=valid_sets[1],  # 验证特征和标签
                feature_name=feature_names,  # 特征名称
            )

        callbacks = []  # 初始化回调列表
        if early_stopping_rounds and valid_data:  # 启用早停且需要验证集
            callbacks.append(lgb.early_stopping(early_stopping_rounds))  # 添加早停回调
            callbacks.append(lgb.log_evaluation(period=0))  # 添加日志输出回调（静默）

        self.model = lgb.train(  # 训练 LightGBM 模型
            self._params,  # 训练参数
            train_data,  # 训练数据
            num_boost_round=num_boost_round,  # 最大迭代轮数
            valid_sets=[valid_data] if valid_data else None,  # 验证集列表
            callbacks=callbacks if callbacks else None,  # 回调函数
        )
        self._is_fitted = True  # 标记模型已训练

        # 特征重要性
        importance = self.model.feature_importance(importance_type="gain")  # 计算基于增益的特征重要性
        result = {  # 构建训练结果字典
            "num_boost_round": self.model.num_trees(),  # 实际训练的树的数量
            "feature_importance": dict(zip(feature_names, map(int, importance))),  # 特征重要性字典
            "best_iteration": getattr(self.model, "best_iteration", num_boost_round),  # 最佳迭代轮数
        }

        if valid_data:  # 有验证集
            result["best_score"] = self.model.best_score  # 最佳分数
        return result  # 返回训练结果

    # ── 增量更新 ──

    def incremental_update(
        self,
        X_new: np.ndarray,  # 新数据特征矩阵
        y_new: np.ndarray,  # 新数据标签
        sample_weight: Optional[np.ndarray] = None,  # 新数据样本权重（可选）
        num_boost_round: int = 50,  # 额外训练轮数（默认 50）
        learning_rate: Optional[float] = None,  # 可选，覆盖原学习率（新数据通常用小学习率）
    ) -> dict:
        """增量更新 — 从当前模型继续训练。

        Args:
            X_new: 新数据特征
            y_new: 新数据标签
            sample_weight: 新数据权重
            num_boost_round: 额外训练轮数
            learning_rate: 可选，覆盖原学习率

        Returns:
            更新后指标
        """
        if not self._is_fitted or self.model is None:  # 模型尚未拟合
            return self.train(X_new, y_new, num_boost_round=num_boost_round)  # 转为初始训练

        # 增量训练前先做维度归一：加速器当前实际产出 28 维，
        # 而模型是按 64 维（22 实际 + 42 预留）训练的。若继续训练数据维度
        # 与现有模型不一致，LightGBM 的 shape check 会报
        # "The number of features in data (28) is not the same as it was in training data (64)"。
        # 这里统一补齐到模型期望的维度（缺的补 0，多余截断），保证增量训练可进行。
        target = self.model.num_feature()  # 模型期望的特征数
        cur = X_new.shape[1]  # 新数据实际特征数
        if cur != target:
            if cur < target:
                pad = np.zeros((X_new.shape[0], target - cur), dtype=X_new.dtype)
                X_new = np.concatenate([X_new, pad], axis=1)
            else:
                X_new = X_new[:, :target]

        params = dict(self._params)  # 复制训练参数
        if learning_rate is not None:  # 提供了新学习率
            params["learning_rate"] = learning_rate  # 覆盖学习率

        train_data = lgb.Dataset(  # 创建新数据的数据集
            X_new, label=y_new,  # 特征和标签
            feature_name=self.feature_names,  # 使用已有的特征名
            weight=sample_weight,  # 样本权重
            reference=None,  # 不参考原有数据
        )

        self.model = lgb.train(  # 在现有模型基础上继续训练
            params,  # 训练参数
            train_data,  # 新数据
            num_boost_round=num_boost_round,  # 额外训练轮数
            init_model=self.model,  # 初始模型（当前模型）
            valid_sets=None,  # 增量更新不设验证集
            callbacks=[lgb.log_evaluation(period=0)],  # 静默日志
        )

        # 保存
        if self.model_path:  # 有模型保存路径
            self.save(self.model_path)  # 保存更新后的模型

        return {  # 返回更新结果
            "num_boost_round": self.model.num_trees(),  # 更新后总树数
            "update_rounds": num_boost_round,  # 新增的树数
        }

    # ── 预测 ──

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """预测分类概率。

        Args:
            X: 特征矩阵，shape (n_samples, n_features)

        Returns:
            概率矩阵，shape (n_samples, n_classes)
            列顺序: normal, obvious_issue, has_risk, needs_review
        """
        if not self._is_fitted or self.model is None:  # 模型未训练
            raise RuntimeError("模型尚未训练，请先调用 train() 或 load()")

        # 特征维度兼容：模型按 64 维训练（22 实际 + 42 预留扩展），
        # 但加速器当前只产出 28 维（FEATURE_NAMES）。缺的维度补 0，
        # 使形状与训练时一致，避免 LightGBM 的 shape check 报错中断。
        # 通用化：无论当前模型是几维，统一归一（补 0 / 截断）到模型期望维数。
        if X.ndim == 2 and self.model.num_feature() != X.shape[1]:
            target = self.model.num_feature()  # 模型期望的特征数
            cur = X.shape[1]  # 当前输入特征数
            if cur < target:  # 补零到目标维度
                pad = np.zeros((X.shape[0], target - cur), dtype=X.dtype)
                X = np.concatenate([X, pad], axis=1)
            # 输入多于模型特征数时（理论不应发生），截断到目标维度
            elif cur > target:
                X = X[:, :target]
        return self.model.predict(X)  # 返回预测概率矩阵

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """预测类别 + 置信度。

        Returns:
            (categories, confidences)
            categories: 每个样本的预测类别索引
            confidences: 每个样本的最高概率值
        """
        proba = self.predict_proba(X)  # 获取概率矩阵
        categories = proba.argmax(axis=1)  # 取每行最大概率的类别索引
        confidences = proba.max(axis=1)  # 取每行的最大概率值（置信度）
        return categories, confidences  # 返回类别索引和置信度

    def predict_single(
        self, feature_vector: list[float]  # 单条 64 维特征向量
    ) -> tuple[str, float, dict[str, float]]:
        """对单条特征向量预测（最常用的接口）。

        Returns:
            (category_name, confidence, all_probs)
            category_name: normal / obvious_issue / has_risk / needs_review
            confidence: 最高概率 0-1
            all_probs: {标签名: 概率}
        """
        X = np.array([feature_vector], dtype=np.float32)  # 转为 NumPy 数组（二维）
        proba = self.predict_proba(X)[0]  # 取第一条（也是唯一一条）的概率
        idx = int(proba.argmax())  # 获取最高概率的类别索引
        all_probs = {LABELS[i]: round(float(proba[i]), 4) for i in range(len(LABELS))}  # 构建标签→概率字典
        return LABELS[idx], round(float(proba[idx]), 4), all_probs  # 返回标签名、置信度、所有概率

    # ── 模型持久化 ──

    def save(self, path: str) -> None:
        """保存模型到文件。"""
        if self.model is None:  # 模型不存在
            raise RuntimeError("无模型可保存")
        self.model_path = path  # 更新模型路径
        Path(path).parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在
        self.model.save_model(path)  # 保存 LightGBM 模型文件
        # 同时保存特征名
        meta_path = path + ".meta.json"  # 元数据文件路径（原路径 + .meta.json）
        with open(meta_path, "w") as f:  # 写入元数据
            json.dump({  # 序列化元数据
                "feature_names": self.feature_names,  # 特征名列表
                "labels": LABELS,  # 标签名列表
            }, f)

    def load(self, path: str) -> None:
        """从文件加载模型。"""
        self.model_path = path  # 记录模型路径
        self.model = lgb.Booster(model_file=path)  # 加载 LightGBM 模型
        self._is_fitted = True  # 标记模型已训练

        # 加载特征名
        meta_path = path + ".meta.json"  # 元数据文件路径
        if os.path.exists(meta_path):  # 元数据文件存在
            with open(meta_path) as f:  # 读取元数据
                meta = json.load(f)  # 解析 JSON
                self.feature_names = meta.get("feature_names", [])  # 提取特征名

        # 如果 meta 不存在，从模型获取
        if not self.feature_names:  # 特征名为空
            try:
                self.feature_names = self.model.feature_name()  # 从模型对象中获取特征名
            except Exception:  # 获取失败
                self.feature_names = [f"f{i}" for i in range(self.model.num_feature())]  # 自动生成特征名

    # ── 评估 ──

    def evaluate(self, X_test: np.ndarray, y_test: np.ndarray) -> dict:
        """评估模型在测试集上的表现。"""
        from sklearn.metrics import (  # 导入 sklearn 评估指标
            accuracy_score, precision_recall_fscore_support,  # 准确率、精确率/召回率/F1
            confusion_matrix,  # 混淆矩阵
        )

        categories, confidences = self.predict(X_test)  # 在测试集上做预测

        acc = accuracy_score(y_test, categories)  # 计算准确率
        precision, recall, f1, _ = precision_recall_fscore_support(  # 计算加权精确率、召回率、F1
            y_test, categories, average="weighted", zero_division=0,  # 加权平均，处理除零
        )
        cm = confusion_matrix(y_test, categories).tolist()  # 计算混淆矩阵并转为列表

        # 每类准确率
        per_class = {}  # 初始化每类指标字典
        for i, label in enumerate(LABELS):  # 遍历每个类别
            mask = y_test == i  # 筛选属于该类的样本
            if mask.sum() > 0:  # 该类有样本
                per_class[label] = {  # 记录该类指标
                    "accuracy": round(float((categories[mask] == i).mean()), 4),  # 该类准确率
                    "count": int(mask.sum()),  # 该类样本数
                }

        return {  # 返回评估结果字典
            "accuracy": round(float(acc), 4),  # 整体准确率
            "precision": round(float(precision), 4),  # 精确率
            "recall": round(float(recall), 4),  # 召回率
            "f1_score": round(float(f1), 4),  # F1 分数
            "confusion_matrix": cm,  # 混淆矩阵
            "per_class": per_class,  # 每类指标
            "avg_confidence": round(float(confidences.mean()), 4),  # 平均置信度
            "samples": len(y_test),  # 测试样本总数
        }

    @property
    def is_fitted(self) -> bool:
        """检查模型是否已训练。"""
        return self._is_fitted  # 返回模型训练状态

    def feature_importance(self, top_k: int = 20) -> list[dict]:
        """返回 top-K 重要特征。"""
        if not self.model:  # 模型不存在
            return []  # 返回空列表
        importance = self.model.feature_importance(importance_type="gain")  # 获取基于增益的特征重要性
        names = self.feature_names or [f"f{i}" for i in range(len(importance))]  # 特征名列表
        pairs = sorted(  # 按重要性降序排序
            zip(names, importance),  # 将特征名和重要性配对
            key=lambda x: x[1],  # 按重要性值排序
            reverse=True,  # 降序
        )
        total = sum(importance) or 1  # 重要性总和（避免除零）
        return [  # 返回前 top_k 个重要特征
            {"name": name, "gain": int(gain), "pct": round(gain / total, 4)}  # 特征名、增益值、百分比
            for name, gain in pairs[:top_k]  # 取前 top_k 个
        ]