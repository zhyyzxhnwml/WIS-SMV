"""
run.py — 全链路一键运行入口（系统主程序）
=========================================

【这个文件做什么？】
  三级级联系统的完整运行入口，也是全系统唯一的数据处理管道。
  无论是 CLI 直接运行、start.py --cli、还是 Web 系统上传数据，
  最终都是运行这个文件的 main() 函数。

  自动完成：CSV 读取 → 格式转换 → AIS 加速器 → ML Triage → LLM 判定 → 结果保存

  【重要】CascadeAgent 不再自己造管道，而是直接调用本文件的 main()。
  本文件就是唯一的管道，所有入口都走这里。

【执行流程】
  1. 初始化各层组件（加速器/ML模型/向量库/LLM）
  2. 逐文件读取输入 CSV
  3. 格式转换（原始时间格式 → ISO 8601）
  4. 逐船执行三级级联分析
  5. 实时打印每艘船的分析过程和结果
  6. 保存全部结果到输出 CSV

用法:
    # 处理 data/input/ 下所有 CSV
    python run.py

    # 指定输入输出目录
    python run.py --input data/input --output data/output

    # 处理单个文件
    python run.py -i ship-violation-agent/data/input/210724铜锣峡210731.csv

    # 只处理前 10 艘船
    python run.py -n 10

    # 跳过 LLM (仅加速器+ML路由，快)
    python run.py --no-llm
"""
import sys, os, io, csv, argparse, json, time  # 系统模块、文件操作、编码、CSV解析、命令行参数、JSON、时间模块
from datetime import datetime  # 日期时间处理
from pathlib import Path  # 跨平台路径处理

# Windows 终端 UTF-8 修复（仅在直接运行时执行，避免 import 时干扰调用方的 stdout）
if sys.platform == "win32" and __name__ == "__main__":  # 判断当前操作系统是否为Windows且为主入口
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 设置标准输出为UTF-8编码
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # 设置标准错误输出为UTF-8编码
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(__file__))  # 将项目根目录加入Python模块搜索路径

# 当前数据归属用户：cascade_agent.scan_csv() 会先设为上传者 id；CLI 直跑保持 'system'
CURRENT_USER_ID = "system"

from triage_engine.model import TriageModel  # ML分类模型
from triage_engine.router import TriageRouter  # ML路由分类器
from triage_engine.milvus_client import MilvusClient  # Milvus向量数据库客户端
from llm_layer.handler import LLMHandler  # LLM违规分析处理模块
from llm_layer.feedback import FeedbackLoop, check_and_retrain  # 反馈闭环（自动累积+增量训练）
from memory import SessionMemory, ShipMemory  # 记忆系统（PatternMemory 不再用于检测管道）
from orchestrator import EndToEndOrchestrator  # 三级级联系统主编排器

# ── 参数 ──

parser = argparse.ArgumentParser(description="全链路一键运行")  # 命令行参数解析器
parser.add_argument("--input", "-i", default="data/input",  # 输入CSV文件或目录路径
                    help="输入 CSV 文件或目录 (默认 data/input)")
parser.add_argument("--output", "-o", default="data/output",  # 输出目录路径
                    help="输出目录 (默认 data/output)")
parser.add_argument("--max-ships", "-n", type=int, default=0,  # 最大处理船舶数
                    help="最多处理船舶数, 0=全部 (默认 0)")
parser.add_argument("--no-llm", action="store_true",  # 跳过LLM层开关
                    help="跳过 LLM 层（只用加速器+ML路由）")
parser.add_argument("--workers", "-w", type=int, default=4,  # 并行分析线程数
                    help="并行分析线程数 (默认 4)")
parser.add_argument("--retrain", action="store_true",  # 强制增量训练开关
                    help="强制增量训练（从已累积的反馈数据训练模型后退出）")

# 只在直接运行时解析命令行参数，被 import 时不解析（避免和调用方的 argparse 冲突）
if __name__ == "__main__":
    args = parser.parse_args()
else:
    # 被 import 时用默认值，后续由 CascadeAgent 设置
    args = parser.parse_args([])

INPUT = args.input  # 输入路径
OUTPUT = args.output  # 输出路径
MAX_SHIPS = args.max_ships if args.max_ships > 0 else None  # 最大处理船舶数（0表示全部）


# ── CSV 输出列 ──

OUTPUT_FIELDS = [  # CSV输出字段名列表
    "mmsi",  # 船舶MMSI号
    "source_file",  # 源文件名
    "analysis_time",  # 分析时间
    "violation_detected",  # 是否检测到违规
    "violation_type",  # 违规类型
    "violation_label",  # 违规标签
    "confidence",  # 置信度
    "risk_level",  # 风险等级
    "routing",  # ML路由结果
    "recommended_action",  # 建议操作
    "final_assessment",  # 最终评估
    "errors",  # 错误信息
]


# ── 初始化（只做一次，惰性：首次 import 时执行） ──

_initialized = False  # 是否已初始化
model = None  # ML模型实例
milvus = None  # Milvus向量数据库客户端
router = None  # ML路由模块
llm = None  # LLM处理模块（None=不可用，LLMHandler实例=可用）
feedback = None  # 反馈闭环实例
pgclient = None  # PostgreSQL/pgvector 客户端（None=未连接）
session_memory = None  # 会话记忆
ship_memory = None  # 船舶记忆
orchestrator = None  # 三级级联系统主编排器
regulation_db = None  # 法规向量数据库


def _ensure_initialized():
    """确保所有组件已初始化（惰性初始化，首次调用时执行）。"""
    global _initialized, model, milvus, router, llm, feedback, pgclient
    global session_memory, ship_memory, orchestrator, regulation_db

    if _initialized:
        return

    _MODEL = "triage_model.txt"  # ML模型文件路径
    if not os.path.exists(_MODEL):  # 如果模型文件不存在
        print(">> 训练初始 ML 模型...")  # 提示开始训练初始模型
        from triage_engine.train import train_initial  # 导入训练模块
        train_initial(output_path=_MODEL, n_samples=500)  # 用500个样本训练初始模型

    model = TriageModel(model_path=_MODEL)  # 创建ML模型实例

    # 特征向量填充: 模型训练用 64 维 (22 实际 + 42 预留 0), 但实际只有 22 维
    _predict_single = model.predict_single  # 保存原始的单样本预测方法引用
    def _pad_predict(fv):  # fv: 特征向量（22维或64维）
        if len(fv) == 22:  # 如果输入的只有22维（实际特征）
            fv = fv + [0.0] * 42  # 填充42个0到64维（匹配模型输入要求）
        return _predict_single(fv)  # 调用原始预测方法
    model.predict_single = _pad_predict  # 替换为带填充的预测方法

    try:
        from config import milvus_uri as _cfg_uri
        _m_uri = _cfg_uri()
    except Exception:
        _m_uri = None
    # 本项目只支持连接外部 Milvus（Docker）。连不上则置 None 并告警，不搞本地 Lite。
    try:
        milvus = MilvusClient()  # 从 config 解析连接地址
        milvus.create_collection(drop_if_exists=False)  # 创建轨迹集合（如已存在则跳过）
        if _m_uri:
            print(f"  ✓ Milvus 已连接: {_m_uri}")
    except Exception as _milvus_err:
        print(f"  ⚠ Milvus 初始化失败: {_milvus_err}")
        print(f"  本项目只支持连接 Docker Milvus，请确认已启动 docker compose（F:\\milvus）")
        milvus = None

    router = TriageRouter(model=model, milvus_client=milvus)  # ML路由模块

    llm = LLMHandler(model_name="qwen2.5:7b-instruct-q4_K_M")  # LLM处理模块（使用Qwen2.5 7B模型）

    # 若 Ollama 仍在启动早期（HTTP 500/拒连）导致初始化失败，
    # 这里轮询等待至多 ~25s，覆盖 Windows 上 Ollama 的秒级启动窗口。
    if not llm.model_available:
        for _ in range(5):  # 每 5s 探测一次，最多 5 次 = 25s
            time.sleep(5)
            llm._init_llm()  # 用新的连通性探测重新初始化
            if llm.model_available:
                break

    if llm.model_available:
        print("  ✓ LLM 模型已就绪")
    else:
        print("  ⚠ LLM 模型暂不可用，级联检测将自动重试（加速器+ML路由正常工作）")

    # 反馈闭环（自动累积训练数据 + 自动增量训练）
    feedback = FeedbackLoop(model_path=_MODEL, auto_retrain=True)  # 反馈闭环实例（自动重训练）

    # ── 智能体记忆系统初始化 ──
    print(">> 初始化智能体记忆系统...")
    session_memory = SessionMemory()  # 会话记忆（进程内，跑完丢弃）
    ship_memory = ShipMemory()  # 船舶记忆（SQLite 持久化）
    # PatternMemory 不再用于检测管道——记忆系统只用于小慧智能体（个性化对话）
    # 不干预检测管道的 LLM 判定，避免"删了记忆数据还复用旧结论"的问题

    # ── 法规向量数据库初始化 ──
    print(">> 初始化法规向量数据库...")
    regulation_db = None
    try:
        from ais_accelerator.regulation_db import RegulationDB
        regulation_db = RegulationDB()
        reg_count = regulation_db.initialize()
        print(f"  法规数据库: {reg_count} 条法规已就绪")
    except Exception as _reg_err:
        print(f"  ⚠ 法规数据库初始化失败: {_reg_err}")
        print(f"  级联检测将继续，但 LLM 判定将无法引用具体法规条款")

    orchestrator = EndToEndOrchestrator(  # 三级级联系统主编排器
        triage_router=router,  # ML路由模块
        llm_handler=llm,  # LLM处理模块（可能为 None）
        milvus_client=milvus,  # Milvus向量数据库客户端
        feedback_loop=feedback,  # 反馈闭环模块
        session_memory=session_memory,  # 会话记忆
        ship_memory=ship_memory,  # 船舶记忆
        regulation_db=regulation_db,  # 法规向量数据库
    )

    # ── PostgreSQL / pgvector 客户端（存结构化案例/训练集/反馈） ──
    try:
        from triage_engine.pgvector_client import PGVectorClient
        _pg = PGVectorClient()  # 从 config 读连接参数
        _pg.init_schema()  # 确保表存在（幂等）
        pgclient = _pg
        print(f"  ✓ PostgreSQL/pgvector 已连接（{_pg.host}:{_pg.port}/{_pg.dbname}）")
        orchestrator.pgvector = _pg  # 挂到编排器，供案例写入/相似检索
    except Exception as _pg_err:
        pgclient = None
        print(f"  ⚠ PostgreSQL/pgvector 未连接: {_pg_err}")

    os.makedirs(OUTPUT, exist_ok=True)  # 创建输出目录（如已存在则跳过）
    _initialized = True


# CLI 直接运行时立即初始化
if __name__ == "__main__":
    _ensure_initialized()


# ── CSV 格式转换（原 CSV 列名大写、时间格式 YYYYMMDDHHMMSS → 加速器格式） ──

def convert_csv(raw_path: str, cache_dir: str) -> str:  # raw_path: 原始CSV路径; cache_dir: 缓存目录
    """把原始 AIS CSV 转为加速器能读的格式"""
    cache_dir = Path(cache_dir)  # 缓存目录Path对象
    cache_dir.mkdir(parents=True, exist_ok=True)  # 创建缓存目录（递归创建）
    # 用文件名 hash 做缓存名
    raw = Path(raw_path)  # 原始文件Path对象
    converted_path = cache_dir / f"_{raw.stem}_converted.csv"  # 转换后的缓存文件路径
    if converted_path.exists():  # 如果缓存文件已存在
        return str(converted_path)  # 直接返回缓存路径（跳过转换）
    print(f"  [转换] {raw.name} → 标准化格式...")
    with open(raw, "r", encoding="utf-8-sig") as fin,open(converted_path, "w", encoding="utf-8", newline="") as fout:  # 以写入方式打开目标CSV# 以只读方式打开原始CSV
        reader = csv.DictReader(fin)  # CSV字典读取器
        reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]  # 字段名统一小写+去空白
        # 判断需要的列：加速器要 mmsi, time, lat, lon, speed, course
        fields = [f for f in ["mmsi", "time", "lon", "lat", "course", "heading", "speed"]  # 需要的字段列表
                  if f in reader.fieldnames]  # 只保留实际存在的字段
        writer = csv.DictWriter(fout, fieldnames=fields)  # CSV字典写入器
        writer.writeheader()  # 写入表头
        for row in reader:  # 遍历原始CSV的每一行
            # 时间格式: YYYYMMDDHHMMSS → YYYY-MM-DD HH:MM:SS
            raw_time = row.get("time", "")  # 原始时间字符串
            if raw_time and len(raw_time) == 14 and raw_time.isdigit():  # 如果符合14位数字格式
                row["time"] = f"{raw_time[:4]}-{raw_time[4:6]}-{raw_time[6:8]} " f"{raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"# 转换为ISO 8601格式
            # 如果只有 lat/lon 没有 mmsi 列但列名不同，尝试兼容
            writer.writerow({k: row[k] for k in fields})  # 写入转换后的行（仅保留需要的字段）
    print(f"  [转换] 完成: {converted_path}")
    return str(converted_path)  # 返回转换后文件的路径


# ── 结果保存为 CSV ──

def _get_source_stem(pm) -> str:  # pm: pipeline_metrics，可以是dict或Pydantic对象
    """从 pipeline_metrics 提取源文件名，兼容 dict 和 Pydantic 对象"""
    if pm is None:  # 如果指标为空
        return ""
    if isinstance(pm, dict):  # 如果是字典类型
        sp = pm.get("source_path", "")  # 从字典获取source_path
    else:  # 如果是Pydantic对象
        sp = getattr(pm, "source_path", "")  # 通过属性获取source_path
    return Path(sp).stem if sp else ""  # 返回文件名（不含扩展名）


def _get_mmsi(r: dict) -> str:  # r: 单条处理结果字典
    """兼容 run_full (r.mmsi) 和 run_with_ml (r.ship.mmsi)"""
    m = r.get("mmsi") or r.get("ship", {}).get("mmsi") or ""  # 从不同位置获取MMSI号
    return str(m)  # 转为字符串返回


def save_results(results: list[dict], output_path: str):  # results: 处理结果列表; output_path: 输出CSV路径
    """保存全链路结果到 CSV"""
    opath = Path(output_path)  # 输出文件Path对象
    opath.parent.mkdir(parents=True, exist_ok=True)  # 确保父目录存在
    with open(opath, "w", encoding="utf-8-sig", newline="") as f:  # 以UTF-8 BOM格式写入CSV
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)  # CSV写入器，按预定义的字段顺序
        writer.writeheader()  # 写入CSV表头
        for r in results:  # 遍历每条处理结果
            llm = r.get("llm_result", {})  # LLM分析结果
            routing = r.get("routing", {})  # ML路由结果
            row = {  # CSV行数据
                "mmsi": _get_mmsi(r),  # 船舶MMSI号
                "source_file": _get_source_stem(r.get("pipeline_metrics")),  # 源文件名
                "analysis_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # 当前分析时间
                "violation_detected": str(llm.get("violation_detected", "")),  # 是否检测到违规
                "violation_type": llm.get("violation_type", "") or "",  # 违规类型
                "violation_label": llm.get("violation_label", "") or "",  # 违规标签
                "confidence": f"{llm.get('confidence', 0):.2%}" if llm  # 置信度百分比
                             else f"{routing.get('ml_confidence', 0):.2%}",  # 无LLM时使用ML置信度
                "risk_level": llm.get("risk_level", "low") if llm  # 风险等级
                              else ("low" if routing.get("routing") == "normal" else "middle"),  # 无LLM时基于路由推断
                "routing": routing.get("routing", "") if routing else "",  # ML路由结果
                "recommended_action": llm.get("recommended_action", ""),  # 建议操作
                "final_assessment": (llm.get("final_assessment", "") or "")[:200],  # 最终评估（截取前200字符）
                "errors": (llm.get("error", "") or ""),  # 错误信息
            }
            writer.writerow(row)  # 写入一行
    print(f"  [保存] {opath} ({len(results)} 条)")  # 打印保存结果


# ── 三层级联处理（逐条船实时输出） ──

def print_sep(title: str = ""):  # title: 分隔线标题（可选）
    print(f"\n{'─'*50}")  # 打印50个横线作为分隔线
    if title:  # 如果有标题
        print(f"  {title}")  # 打印居中标题
        print(f"{'─'*50}")  # 打印底部横线


def process_file(csv_path: str):  # csv_path: CSV文件路径
    """处理单个 CSV 文件，每条船逐层过：加速器 → ML → LLM"""
    _ensure_initialized()  # 确保组件已初始化
    print(f"\n{'='*60}")  # 打印等号分隔线
    print(f"  ⚓ 文件: {Path(csv_path).name}")  # 显示当前处理文件名
    print(f"{'='*60}")  # 打印底部分隔线

    converted = convert_csv(csv_path, "data/input")  # 转换为标准化格式后的文件路径
    source_name = Path(csv_path).stem  # 源文件名（不含扩展名）

    # 批量载入数据到 SQLite 缓存
    print("\n  ⬆ [加速器] 载入数据...")  # 提示开始载入数据
    store = orchestrator.accelerator.pipeline.store  # SQLite数据存储
    ingest_stats = store.ingest(converted, max_ships=MAX_SHIPS)  # 批量载入数据，返回载入统计
    total_ships = ingest_stats.ship_count  # 总船舶数
    total_raw_rows = ingest_stats.raw_rows  # 原始行数
    total_valid_rows = ingest_stats.valid_rows  # 有效行数
    print(f"    原始行: {total_raw_rows} | 有效: {total_valid_rows} | 船舶: {total_ships}")  # 打印数据载入统计

    # 陆地规避：用本批全部船的原始轨迹点构建航道走廊（WaterChecker），
    # 传给 annotate_track 让清洗层剔除"落在陆地掩码内且远离航道"的点。
    try:
        from ais_accelerator.water import WaterChecker  # 陆地/水域判定器
        _lon_lats = [(p.lon, p.lat) for _s, _pts in store.iter_ship_tracks() for p in _pts]  # 收集全部轨迹点经纬度
        water = WaterChecker(lon_lats=_lon_lats) if _lon_lats else None  # 构建判定器（无数据则None）
        _on_land = sum(1 for lon, lat in _lon_lats if water is not None and water.is_on_land(lon, lat))  # 统计陆地点数
        if water is not None:
            print(f"    ⛰ 陆地规避: 航道走廊 {len(water.corridor)} 格 | 原始点中陆地点 {_on_land} 个")  # 打印陆地规避信息
    except Exception as _water_err:
        water = None  # 陆地判定失败不影响主流程
        print(f"    ⚠ 陆地规避初始化失败（已忽略）: {_water_err}")  # 打印警告

    results = []  # 处理结果列表
    # 逐条船遍历
    for idx, (ship, raw_points) in enumerate(store.iter_ship_tracks(), 1):  # idx: 船舶序号; ship: 船舶元数据; raw_points: 原始轨迹点
        print_sep(f"⚓ [{idx}/{total_ships}] MMSI={ship.mmsi}")  # 打印船舶分隔线和序号

        # ━━ 第一层：AIS 加速器 ━━
        print("  [1/3] AIS 加速器 — 质量清洗 → 分段 → 事件检测")  # 提示当前层级
        annotated = orchestrator.accelerator.annotate_track(ship, raw_points, water=water)  # 单条船舶的加速器标注结果

        # 加速器输出摘要
        anno_types = {}  # 各类标注计数
        for a in annotated.annotations:  # 遍历所有标注
            anno_types[a.type] = anno_types.get(a.type, 0) + 1  # 按标注类型计数
        anno_str = ", ".join(f"{k}={v}" for k, v in sorted(anno_types.items())) if anno_types else "无标记"  # 格式化标注统计字符串
        fm = annotated.feature_metadata  # 特征元数据（包含速度等统计信息）
        print(f"    ├ 轨迹点: {len(annotated.track)} | 分段: {len(annotated.segments)}")  # 轨迹点和分段数
        print(f"    ├ 事件: {len(annotated.events)} | 标记: {len(annotated.annotations)} | {anno_str}")  # 事件和标注数
        print(f"    ├ 速度: 平均 {fm.get('avg_speed', '?'):.1f}节 / 最高 {fm.get('max_speed', '?'):.1f}节")  # 速度统计
        print(f"    └ 质量: {annotated.quality_report.level if annotated.quality_report else '?'} "  # 数据质量等级
              f"(重复点: {annotated.quality_report.duplicate_points if annotated.quality_report else 0})")  # 重复点数量

        # ━━ 第二层：ML Triage ━━
        print("  [2/3] ML Triage 路由")  # 提示当前层级
        fv = annotated.feature_vector  # 轨迹特征向量（22维）
        if len(fv) == 22:  # 如果只有22维实际特征
            fv = fv + [0.0] * 42  # 填充42个0到64维
        decision = router.route(  # ML路由分类决策
            feature_vector=fv,  # 特征向量
            mmsi=ship.mmsi,  # 船舶MMSI号
        )

        # ML 结果输出
        probs = " | ".join(f"{k}: {v:.1%}" for k, v in sorted(decision.all_probabilities.items())) if decision.all_probabilities else ""  # 各分类概率字符串
        similar_info = f" | Milvus 相似: {len(decision.milvus_hits)} 条" if decision.milvus_hits else ""  # Milvus相似检索信息
        print(f"    ├ 分类: {decision.ml_category} | 置信度: {decision.ml_confidence:.1%}")  # ML分类结果和置信度
        print(f"    ├ 路由: {decision.routing}{similar_info}")  # 路由决策及相似轨迹信息
        print(f"    ├ 概率: {probs}")  # 各分类概率分布
        print(f"    └ 耗时: {decision.triage_latency_ms:.0f}ms")  # ML路由耗时（毫秒）

        # 构建 entry
        entry = annotated.to_agent_input()  # 转换为Agent输入格式
        entry["mmsi"] = ship.mmsi  # 船舶MMSI号
        entry["routing"] = decision.to_dict()  # 路由决策转为字典

        # 如果跳 LLM，直接收集
        if args.no_llm:  # 如果指定了--no-llm参数（跳过LLM层）
            entry["pipeline_metrics"] = orchestrator.accelerator.metrics.summary()  # 加速器指标摘要
            results.append(entry)  # 直接保存结果（无LLM分析）
            print(f"    ▶ 结果: 路由={decision.routing} (置信度 {decision.ml_confidence:.0%})")  # 打印简化结果
            continue  # 跳过LLM层，处理下一艘船

        # ━━ 第三层：LLM 判定（使用 orchestrator 的带记忆分析） ━━
        print(f"  [3/3] LLM 判定 — 模板: {decision.llm_prompt_template}")  # 提示当前层级及使用的提示模板

        # 注意：不再用 PatternMemory 跳过 LLM
        # 记忆系统只用于小慧智能体（个性化对话），不干预检测管道的判定
        llm_result = None

        # 存 Milvus
        if annotated.track:  # 如果有轨迹点数据
            try:
                milvus.insert_track(  # 将轨迹插入Milvus向量数据库
                    id=f"{ship.mmsi}_{annotated.track[0].timestamp.isoformat()[:10]}",  # 以"MMSI_日期"作为唯一ID
                    mmsi=ship.mmsi,  # 船舶MMSI号
                    track_points=[p.model_dump(mode="json") for p in annotated.track],  # 轨迹点JSON列表
                    metadata={"routing": decision.to_dict()},  # 关联的路由决策元数据
                )
            except Exception:  # 插入失败时静默跳过
                pass

        # 调用 LLM（通过 orchestrator）
        llm_result = orchestrator._llm_with_memory(
            track=annotated,
            entry=entry,
            routing_decision=decision,
        )

        # LLM 不可用时返回安全默认值，不再抛异常中断
        if llm_result and llm_result.get("error") == "llm_unavailable":
            print(f"    ⚠ LLM 不可用，跳过判定（仅保留加速器+ML结果）")
            entry["llm_result"] = llm_result
            entry["pipeline_metrics"] = orchestrator.accelerator.metrics.summary()
            results.append(entry)
            continue

        entry["llm_result"] = llm_result  # LLM分析结果存入entry
        entry["pipeline_metrics"] = orchestrator.accelerator.metrics.summary()  # 加速器指标摘要

        # ━━ 风险等级：用确定性引擎覆盖 LLM 的主观判定 ━━
        try:
            from ais_accelerator.risk_engine import assess_risk, normalize_risk_level
            from ais_accelerator.violation_catalog import REG_ANCHORS, normalize_violation_code
            _vtype_raw = llm_result.get("violation_type", "")
            _vcode = normalize_violation_code(_vtype_raw) if llm_result.get("violation_detected") else "unknown"
            # 证据点数量：从加速器事件取最大 evidence_points
            _ev_counts = [len(ev.get("evidence_points", [])) for ev in (entry.get("events") or [])]
            _max_ev = max(_ev_counts) if _ev_counts else 0
            # 持续时长（分钟）：从 feature_metadata 或事件取
            _dur = 0.0
            if annotated.events:
                from datetime import datetime as _dt
                for _ev in annotated.events:
                    try:
                        _s = _dt.fromisoformat(str(_ev.start_time)) if isinstance(_ev.start_time, str) else _ev.start_time
                        _e = _dt.fromisoformat(str(_ev.end_time)) if isinstance(_ev.end_time, str) else _ev.end_time
                        _d = (_e - _s).total_seconds() / 60.0
                        _dur = max(_dur, _d)
                    except Exception:
                        pass
            _has_reg = bool(_vcode in REG_ANCHORS and REG_ANCHORS[_vcode])
            _ql = (annotated.quality_report.level if annotated.quality_report else "good") or "good"
            _llm_risk = llm_result.get("risk_level")
            _risk = assess_risk(
                violation_code=_vcode,
                evidence_count=_max_ev if llm_result.get("violation_detected") else 0,
                duration_minutes=_dur if llm_result.get("violation_detected") else 0.0,
                has_regulation=_has_reg,
                quality_level=_ql,
                llm_risk=_llm_risk,
            )
            llm_result["risk_level"] = _risk["level"]
            llm_result["risk_score"] = _risk["score"]
            llm_result["risk_reasons"] = _risk["reasons"]
            # 违规时同步修正 violation_type 为规范码对应的中文标签，保持落库一致性
            if llm_result.get("violation_detected") and _vcode != "unknown":
                from ais_accelerator.violation_catalog import describe
                llm_result["violation_label"] = describe(_vcode)["label"]
        except Exception as _risk_err:
            # 引擎失败时回退到 LLM 原值，并归一
            from ais_accelerator.risk_engine import normalize_risk_level
            llm_result["risk_level"] = normalize_risk_level(llm_result.get("risk_level"))
            print(f"    ⚠ risk_engine 失败，回退LLM原值: {_risk_err}")

        results.append(entry)  # 将完整结果加入结果列表

        # ── 原始轨迹抽样（raw_track）存入 entry（供前端原始数据可视化） ──
        try:
            if hasattr(annotated, "raw_track") and annotated.raw_track:
                entry["raw_track"] = annotated.raw_track
        except Exception:
            pass

        # ── 写入 detection_results 表（供 Web 前端查询，Docker PostgreSQL） ──
        try:
            import pg_db
            _db_conn = pg_db.connect()
            _db_conn.execute("""
                CREATE TABLE IF NOT EXISTS detection_results (
                    id SERIAL PRIMARY KEY,
                    mmsi TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source_file TEXT,
                    analysis_time TEXT DEFAULT (to_char(now(),'YYYY-MM-DD HH24:MI:SS')),
                    track_points INTEGER DEFAULT 0,
                    segments INTEGER DEFAULT 0,
                    annotations TEXT DEFAULT '[]',
                    avg_speed DOUBLE PRECISION DEFAULT 0,
                    max_speed DOUBLE PRECISION DEFAULT 0,
                    quality_level TEXT DEFAULT '',
                    ml_category TEXT DEFAULT '',
                    ml_confidence DOUBLE PRECISION DEFAULT 0,
                    routing TEXT DEFAULT '',
                    ml_probabilities TEXT DEFAULT '{}',
                    violation_detected INTEGER DEFAULT 0,
                    violation_type TEXT DEFAULT '',
                    risk_level TEXT DEFAULT 'low',
                    confidence DOUBLE PRECISION DEFAULT 0,
                    recommended_action TEXT DEFAULT '',
                    final_assessment TEXT DEFAULT '',
                    regulation_ref TEXT DEFAULT '',
                    llm_latency_seconds DOUBLE PRECISION DEFAULT 0,
                    detailed_result TEXT DEFAULT '{}'
                )
            """)
            # 先删除该船在该用户下的旧记录，避免重复（同船不同用户互不覆盖）
            _db_conn.execute(
                "DELETE FROM detection_results WHERE mmsi = ? AND user_id = ?",
                (ship.mmsi, CURRENT_USER_ID),
            )
            _fm = annotated.feature_metadata or {}
            _ann_json = json.dumps(
                [a.model_dump(mode="json") for a in annotated.annotations],
                ensure_ascii=False,
            ) if hasattr(annotated, 'annotations') else "[]"
            _db_conn.execute("""
                INSERT INTO detection_results
                    (mmsi, user_id, source_file,
                     track_points, segments, annotations, avg_speed, max_speed, quality_level,
                     ml_category, ml_confidence, routing, ml_probabilities,
                     violation_detected, violation_type, risk_level, confidence,
                     recommended_action, final_assessment, regulation_ref,
                     violation_label, violation_event, llm_latency_seconds, detailed_result)
                VALUES (?, ?, ?,
                        ?,?,?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,
                        ?,?,?,?)
            """, (
                ship.mmsi,
                CURRENT_USER_ID,
                source_name,
                len(annotated.track) if hasattr(annotated, 'track') else 0,
                len(annotated.segments) if hasattr(annotated, 'segments') else 0,
                _ann_json,
                _fm.get("avg_speed", 0), _fm.get("max_speed", 0),
                annotated.quality_report.level if hasattr(annotated, 'quality_report') and annotated.quality_report else "",
                decision.ml_category, decision.ml_confidence, decision.routing,
                json.dumps(decision.all_probabilities if hasattr(decision, 'all_probabilities') else decision.all_probs, ensure_ascii=False),
                1 if llm_result.get("violation_detected") else 0,
                llm_result.get("violation_type", ""),
                llm_result.get("risk_level", "low"),
                llm_result.get("confidence", 0),
                llm_result.get("recommended_action", ""),
                (llm_result.get("final_assessment", "") or "")[:500],
                (llm_result.get("regulation_ref", "") or ""),
                (llm_result.get("violation_label", "") or "")[:100],
                (llm_result.get("violation_event", "") or "")[:500],
                llm_result.get("latency_seconds", 0),
                json.dumps(entry, ensure_ascii=False, default=str),
            ))
            _db_conn.commit()

            # 写入 ship_daily_stats（按轨迹点日期聚合，船舶单位活跃统计）
            try:
                from collections import defaultdict
                from datetime import datetime as _dt
                _daily = defaultdict(lambda: {"points": 0, "speed_sum": 0.0, "max_speed": 0.0})
                for _pt in (annotated.track if hasattr(annotated, 'track') else []):
                    _ts = getattr(_pt, "timestamp", None)
                    if _ts is None:
                        continue
                    try:
                        _day = _dt.fromisoformat(str(_ts)).strftime("%Y-%m-%d") if isinstance(_ts, str) else _ts.strftime("%Y-%m-%d")
                    except Exception:
                        continue
                    _d = _daily[_day]
                    _d["points"] += 1
                    _d["speed_sum"] += float(getattr(_pt, "speed", 0) or 0)
                    _d["max_speed"] = max(_d["max_speed"], float(getattr(_pt, "speed", 0) or 0))
                _vio_flag = 1 if llm_result.get("violation_detected") else 0
                for _day, _d in _daily.items():
                    _avg = _d["speed_sum"] / _d["points"] if _d["points"] else 0
                    _moving = _avg >= 0.5
                    _db_conn.execute("""
                        INSERT INTO ship_daily_stats (mmsi, user_id, day, points, avg_speed, max_speed, moving, violated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (mmsi, user_id, day) DO UPDATE SET
                            points = EXCLUDED.points,
                            avg_speed = EXCLUDED.avg_speed,
                            max_speed = EXCLUDED.max_speed,
                            moving = EXCLUDED.moving,
                            violated = EXCLUDED.violated
                    """, (ship.mmsi, CURRENT_USER_ID, _day, _d["points"], _avg, _d["max_speed"], _moving, _vio_flag))
                _db_conn.commit()
            except Exception as _sds_err:
                print(f"    ⚠ ship_daily_stats 写入失败: {_sds_err}")

            _db_conn.close()
        except Exception as _db_err:
            print(f"    ⚠ 写入数据库失败: {_db_err}")

        # 反馈闭环：将 LLM 判定结果回流为训练数据
        if feedback:  # 反馈闭环可能为 None（LLM 不可用时）
            feedback.process_result(  # 将本次结果回馈到训练数据
                case_id=f"{ship.mmsi}_{datetime.now().strftime('%Y%m%d%H%M%S')}",  # 唯一案例ID（MMSI_时间戳）
                ml_prediction=decision.ml_category,  # ML预测分类
                ml_confidence=decision.ml_confidence,  # ML预测置信度
                llm_result=llm_result,  # LLM判定结果（作为弱标签）
                feature_vector=fv,  # 特征向量（用于增量训练）
            )

        # ── 写入 PostgreSQL/pgvector 案例库（若可用） ──
        if pgclient:
            try:
                _case_id = pgclient.insert_case(  # 写入案例主表
                    mmsi=ship.mmsi,
                    feature_vector=fv,  # 28 维特征
                    feature_json=dict(_fm) if _fm else {},
                    annotations=[a.model_dump(mode="json") for a in annotated.annotations]
                        if hasattr(annotated, "annotations") else [],
                    annotation_counts=anno_types,
                    source_file=source_name,
                )
                pgclient.update_ml_result(  # 回填 ML 结果
                    case_id=_case_id,
                    prediction=decision.ml_category,
                    confidence=decision.ml_confidence,
                )
                pgclient.update_llm_result(  # 回填 LLM 结果
                    case_id=_case_id,
                    llm_result=llm_result,
                    has_violation=llm_result.get("violation_detected", False),
                    violation_type=llm_result.get("violation_type"),
                )
            except Exception as _pg_err:
                print(f"    ⚠ 写入 pgvector 失败（已忽略）: {_pg_err}")

        # LLM 结果展示
        vio = llm_result.get("violation_detected", False)  # 是否检测到违规
        vtype = llm_result.get("violation_type", "")  # 违规类型
        conf = llm_result.get("confidence", 0)  # 置信度
        risk = llm_result.get("risk_level", "low")  # 风险等级
        action = llm_result.get("recommended_action", "monitor")  # 建议操作
        assessment = (llm_result.get("final_assessment", "") or "")[:150]  # 最终评估（截取前150字符）
        latency = llm_result.get("latency_seconds", 0)  # LLM调用耗时（秒）

        icon = "[X]" if vio else "[V]"  # 违规标记符号
        print(f"    ├ 结果: {'违规!' if vio else '正常':<8} | 类型: {vtype or '-'}")  # 违规判断结果
        print(f"    ├ 置信度: {conf:.1%} | 风险: {risk} | 建议: {action}")  # 置信度、风险等级和建议操作
        print(f"    ├ 耗时: {latency:.1f}秒")  # LLM调用耗时
        if assessment:  # 如果有评估说明
            print(f"    └ 说明: {assessment}")  # 打印最终评估摘要

        agrees = llm_result.get("agrees_with_triage")  # LLM是否同意ML三分类结果
        if agrees is False:  # LLM与ML分歧
            reason = llm_result.get("disagreement_reason", "")  # 分歧原因
            print(f"    ⚠ LLM 与 ML 分歧: {reason[:100]}")  # 打印分歧原因
        elif agrees is True and decision.routing != "normal":  # LLM确认ML的嫌疑判断
            print(f"    ✓ LLM 确认 ML 判断")  # 打印一致确认信息

    # 保存
    if results:  # 如果有处理结果
        output_path = os.path.join(OUTPUT, f"results_{source_name}.csv")  # 结果CSV输出路径
        save_results(results, output_path)  # 保存结果到CSV文件

    # 汇总
    routes = {}  # 各类路由计数
    vios = 0  # 违规船舶计数
    for r in results:  # 遍历所有处理结果
        rt = r.get("routing", {}).get("routing", "?") if r.get("routing") else "?"  # 获取路由分类
        routes[rt] = routes.get(rt, 0) + 1  # 按路由类型计数
        if r.get("llm_result", {}).get("violation_detected"):  # 如果LLM检测到违规
            vios += 1  # 违规计数加1

    print(f"\n{'='*60}")  # 打印汇总分隔线
    print(f"  ✅ 文件处理完成: {len(results)} 条船舶")  # 打印完成提示
    print(f"{'='*60}")  # 打印底部分隔线
    print(f"  路由分布:")  # 打印路由分布标题
    for route, count in sorted(routes.items(), key=lambda x: -x[1]):  # 按数量降序排列
        bar = "█" * max(1, int(count / len(results) * 30))  # 用方块字符绘制柱状图
        print(f"    {route:<20} {bar} {count} ({count/len(results)*100:.0f}%)")  # 每类路由的数量和占比

    if not args.no_llm:  # 如果启用了LLM层
        print(f"  违规判定: {vios}/{len(results)} 艘违规 ({vios/max(len(results),1)*100:.1f}%)")  # 违规率统计
    print(f"  输出文件: {Path(OUTPUT).resolve() / f'results_{source_name}.csv'}")  # 输出文件路径

    # 文件处理完后检查是否需要增量训练
    if not args.no_llm and feedback:  # 如果启用了LLM层且有反馈闭环
        trained = check_and_retrain("triage_model.txt")  # 检查并执行增量训练
        if trained:  # 如果训练成功
            # 重新加载模型
            new_model = TriageModel(model_path="triage_model.txt")  # 重新加载训练后的模型
            # 恢复 64 维填充适配
            _ps = new_model.predict_single
            def _pp(fv):
                if len(fv) == 22:
                    fv = fv + [0.0] * 42
                return _ps(fv)
            new_model.predict_single = _pp
            router.update_model(new_model)  # 用新模型对象更新路由模块
            print(f"  [反馈] 模型已更新并重新加载")  # 提示模型已更新

    return results  # 返回处理结果列表


# ── 入口 ──

def main():
    """主函数：供 start.py --cli 调用，或被 CascadeAgent 调用。"""
    _ensure_initialized()  # 确保组件已初始化
    # 强制增量训练模式
    if args.retrain:  # 如果指定了--retrain参数
        print(f"\n{'='*60}")  # 打印分隔线
        print(f"  强制增量训练模式")  # 打印模式标题
        print(f"{'='*60}")  # 打印底部分隔线
        trained = check_and_retrain("triage_model.txt", force=True)  # 强制触发增量训练
        if trained:  # 训练成功
            print(f"\n  模型已更新: triage_model.txt")  # 提示模型已更新
        else:  # 训练失败
            print(f"\n  无足够训练数据或训练失败")  # 提示训练失败原因
        return  # 不再 sys.exit()，让调用方正常继续

    try:  # 主逻辑异常保护
        input_path = Path(INPUT)  # 输入路径的Path对象

        if input_path.is_file():  # 如果输入是单个文件
            process_file(str(input_path))  # 直接处理该文件
        elif input_path.is_dir():  # 如果输入是目录
            csv_files = sorted(input_path.glob("*.csv"))  # 获取目录下所有CSV文件（排序）
            if not csv_files:  # 如果没有找到CSV文件
                print(f"输入目录中没有 CSV 文件: {input_path.resolve()}")  # 提示无CSV文件
                return  # 不再 sys.exit()
            print(f"发现 {len(csv_files)} 个 CSV 文件")  # 打印找到的文件数
            all_results = []  # 所有文件的结果汇总
            for f in csv_files:  # 遍历每个CSV文件
                all_results.extend(process_file(str(f)))  # 处理并合并结果
            print(f"\n{'='*60}")  # 打印汇总分隔线
            print(f"全部完成! 共处理 {len(all_results)} 条船舶")  # 打印总处理数
            print(f"结果目录: {Path(OUTPUT).resolve()}")  # 打印输出目录路径
        else:  # 路径不存在
            print(f"输入路径不存在: {input_path}")  # 提示路径不存在
            return  # 不再 sys.exit()
    except Exception as e:  # 捕获其他异常
        print(f"运行异常: {e}")
    # 注意：不在这里关闭 milvus，因为作为模块被 import 时 milvus 需要保持可用
    # milvus.close() 只在 CLI 直接运行时由进程退出自动清理

if __name__ == "__main__":
    try:
        main()
    finally:
        # CLI 直接运行时，进程退出前关闭 milvus
        try:
            milvus.close()
        except Exception:
            pass