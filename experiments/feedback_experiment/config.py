"""
experiments/feedback_experiment/config.py — 实验配置
====================================================
"""

from pathlib import Path

# ── 实验根目录（此文件所在目录） ──
EXPERIMENT_DIR = Path(__file__).parent.resolve()  # 实验模块根目录的绝对路径

# ── 阈值扫描范围（RETRAIN_THRESHOLD 候选值） ──
#
# 当前生产环境默认是 50（feedback.py 第 36 行）。
# 扫描 5~50 步长 5，覆盖"几乎每次分歧都触发"到"累积很多才触发"的全范围。
THRESHOLDS = list(range(5, 55, 5))  # 待扫描的触发阈值列表：[5, 10, 15, 20, 25, 30, 35, 40, 45, 50]

# ── 数据拆分 ──
TRAIN_RATIO = 0.70   # 训练池占比：70% 用于模拟训练池
TEST_RATIO = 0.30    # 测试集占比：30% 用于测试评估

# ── 文件路径 ──
DATA_DIR = EXPERIMENT_DIR / "data"  # 数据存放目录
RESULTS_DIR = EXPERIMENT_DIR / "results"  # 实验结果输出目录
CACHE_FILE = DATA_DIR / "collected_ships.json"  # 收集到的船舶数据缓存文件路径

# ── 数据源 CSV（自动检测可用文件） ──
CANDIDATE_CSVS = [  # 候选 CSV 数据源文件路径列表
    "data/input/___210724铜锣峡210731_converted_converted_converted.csv",
    "data/input/__210724铜锣峡210731_converted_converted.csv",
]

# ── 模型路径 ──
BASE_MODEL_PATH = "triage_model.txt"  # 基础 ML 模型文件路径（正常训练）
BAD_MODEL_PATH = "triage_model_bad.txt"   # 差模型文件路径（仅 30 条合成数据训练）

# ── 增量训练超参（与 feedback.py / triage_engine.train 保持一致） ──
INCREMENTAL_NUM_BOOST_ROUNDS = 50  # 增量训练的 boosting 轮数
INCREMENTAL_LEARNING_RATE = 0.02  # 增量训练的学习率

# ── 收敛判定 ──
CONVERGENCE_THRESHOLD = 0.005   # 收敛阈值：连续 3 次提升 < 0.5% 视作收敛
CONVERGENCE_WINDOW = 3  # 收敛判定窗口大小：连续无改善的轮次数

# ── 合成训练参数（仅在基础模型不存在时使用） ──
DEFAULT_SYNTHETIC_SAMPLES = 500  # 默认合成训练样本数