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
from triage_engine.router import RISK_GATE_THRESHOLD  # LLM 门控阈值（与 L2 同源，勿另行硬编码）
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
    "violation_type",  # 违规类型（LLM 原始判性，可能是自由文本）
    # 规范三级分类：二级规范码 / 三级细类 / 定档实测量值。
    # 这三列此前只写库、不写 CSV——而 CSV 是导出件、也是跑实验要读的产物，
    # 于是分析时拿到的是 violation_type 那个有十余种脏拼法的原始字段
    # （实测出现过 AIS01 / AIS012 / AIS1001 / ais异常关闭 等），
    # 用它做任何按类型统计都是在统计 LLM 的拼写习惯。
    "violation_code",  # 二级规范码（CANONICAL 词表）
    "violation_subtype",  # 三级细类
    "violation_metric",  # 定档实测量值
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
        # 船型码列名统一成 vessel_type。不同数据源叫法不同：MarineCadastre 是
        # VesselType（小写后成 vesseltype），本仓 7 列格式是 vessel_type。
        # 不统一的后果很隐蔽 —— 船型在这一步被丢掉，下游禁渔判定静默退回
        # 速度代理判据，结果看着正常其实判错（实测：NOAA 官方禁渔区上
        # 两条真实商业渔船违规全部漏报）。
        if "vesseltype" in reader.fieldnames and "vessel_type" not in reader.fieldnames:  # 别名归一
            reader.fieldnames = ["vessel_type" if name == "vesseltype" else name  # 重命名船型列
                                 for name in reader.fieldnames]
        # 判断需要的列：加速器要 mmsi, time, lat, lon, speed, course（+可选船型）
        fields = [f for f in ["mmsi", "time", "lon", "lat", "course", "heading", "speed",
                              "vessel_type"]  # 需要的字段列表
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
                "violation_type": llm.get("violation_type", "") or "",  # 违规类型（LLM 原始判性）
                "violation_code": llm.get("violation_code", "") or "",  # 二级规范码
                "violation_subtype": llm.get("violation_subtype", "") or "",  # 三级细类
                "violation_metric": llm.get("violation_metric", ""),  # 定档实测量值
                "violation_label": llm.get("violation_label", "") or "",  # 违规标签
                "confidence": f"{llm.get('confidence', 0):.2%}" if llm  # 置信度百分比
                             else f"{routing.get('ml_confidence', 0):.2%}",  # 无LLM时使用ML置信度
                # 无 LLM 时按路由推断。此处曾写 "middle"——那是**事件优先级**
                # (EventPriority) 的词表；risk_level 的词表是 {high, mid, low}。
                # 两者长得像、列却不同（models.py 的 priority 字段 / risk_engine.py
                # 的归一），混写会让仪表盘的风险饼图把同一档拆成两个扇区。
                "risk_level": llm.get("risk_level", "low") if llm  # 风险等级
                              else ("low" if routing.get("routing") == "normal" else "mid"),  # 无LLM时基于路由推断
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

    # 全船队派生结构：航道走廊（陆地规避）+ 流向/流速场 + 多船时空索引 + 违规追越。
    # 这四样都要求「看得到全体船」，而下游是逐船调用的，所以只能在这里一次算好再下发。
    # 合并成一次遍历是有意的：iter_ship_tracks() 每趟要重读 SQLite 并重建 93.7 万个
    # AISPoint（实测约 10 秒），分开建就是四趟白跑。
    try:
        _t_ctx = time.perf_counter()  # 计时起点
        fleet = orchestrator.accelerator.pipeline.build_fleet_context()  # 一次遍历算齐
        water = fleet.water  # 陆地判定器
        if water is not None:
            print(f"    ⛰ 陆地规避: 航道走廊 {len(water.corridor)} 格")  # 打印陆地规避信息
        if fleet.field is not None:
            _fs = fleet.field.stats()  # 场统计
            print(f"    🧭 流向流速场: {_fs.get('cells', 0)} 格 / "
                  f"{_fs.get('cells_with_modes', 0)} 格有主导航向")  # 打印流向场信息
        _ovt_ships = len(fleet.overtakings)  # 追越事件涉及的船数
        _ovt_events = sum(len(v) for v in fleet.overtakings.values())  # 追越事件数
        _gwy_events = sum(len(v) for v in fleet.give_ways.values())  # 让路失败事件数
        _tow_events = sum(len(v) for v in fleet.towings.values())  # 拖带/并靠事件数
        print(f"    ⚓ 多船遭遇: 索引 {len(fleet.index._bucket_ships) if fleet.index else 0} 桶 | "
              f"违规追越 {_ovt_events} 起 / {_ovt_ships} 船 | "
              f"让路失败 {_gwy_events} 起 | 拖带并靠 {_tow_events} 起 | {fleet.pairwise_seconds:.1f}s")
        if fleet.pairwise_seconds > orchestrator.accelerator.pipeline.config.interaction_max_seconds:
            print(f"    ⚠ 多船检测超出预算 "
                  f"{orchestrator.accelerator.pipeline.config.interaction_max_seconds:.0f}s")  # 超预算提示
        print(f"    ⏱ 派生结构总耗时 {time.perf_counter()-_t_ctx:.1f}s")  # 打印总耗时
    except Exception as _ctx_err:
        water = None  # 派生结构失败不影响主流程
        fleet = None  # 无派生结构：碰撞与追越不检测，其余照常
        print(f"    ⚠ 全船队派生结构初始化失败（已忽略）: {_ctx_err}")  # 打印警告

    # 窗口特征的「同刻点表」：L2 的 64 维契约里 reserved_0..22 载 23 维窗口运动学特征，
    # 其中最近会遇距离要看到当天全体船。全批建一次后各船共用 —— 逐船重建等于把
    # 全船队分桶 N 遍。建不出来（无派生结构）就整体不填，让 has_w=0，
    # **不能**拿"没有邻居信息"的 23 维冒充有邻居的，那是训练/推理错配。
    win_tindex = None  # 建不出来时为 None
    win_feat = None  # 模块句柄：导入失败则本批整批不填（而不是逐船抛异常）
    try:
        # 别名绑定成局部名，所以下面**只能**用 win_feat：写 window_feat 会去查全局，
        # 而全局里没有这个名字（真踩过：NameError 被 except 吞掉 → 静默不填特征）。
        from triage_engine import window_feat as win_feat  # 生产侧窗口特征（与训练同源）
        if fleet is not None and fleet.light_tracks:
            _t_win = time.perf_counter()  # 计时起点
            win_tindex = win_feat.build_tindex(fleet.light_tracks)  # 全批一次
            _npt = sum(len(v) for v in fleet.light_tracks.values())  # 参与分桶的点数
            print(f"    🪟 窗口特征同刻点表: {len(win_tindex)} 刻 / {_npt} 点 | "
                  f"{time.perf_counter()-_t_win:.1f}s")
    except Exception as _win_err:
        win_tindex = None  # 失败则整批不填窗口特征（has_w=0），不影响其余流程
        print(f"    ⚠ 窗口特征点表构建失败（本批不填 reserved）: {_win_err}")

    results = []  # 处理结果列表

    # ── 插件层：每批只重置一次预算 ──
    # 预算是「整批 process_file 的插件总用时上限」（默认 120s），不是每船的。
    # 必须在船循环**之外**重置：放进循环里等于每船都恢复满额，预算就失效了，
    # 500 船 × 每船 3s 会把流水线拖到几十分钟。
    try:
        from plugin_runtime import preload_registry
        _enabled = preload_registry()
        if _enabled:
            print(f"🧩 插件层：本批启用 {len(_enabled)} 个插件 — "
                  + "、".join(p["label"] for p in _enabled))
        else:
            print("🧩 插件层：无已启用插件")
    except Exception as _pl_err:
        print(f"⚠ 插件层初始化失败（本批不跑插件）: {_pl_err}")

    # 逐条船遍历
    for idx, (ship, raw_points) in enumerate(store.iter_ship_tracks(), 1):  # idx: 船舶序号; ship: 船舶元数据; raw_points: 原始轨迹点
        print_sep(f"⚓ [{idx}/{total_ships}] MMSI={ship.mmsi}")  # 打印船舶分隔线和序号

        # ━━ 第一层：AIS 加速器 ━━
        print("  [1/3] AIS 加速器 — 质量清洗 → 分段 → 事件检测")  # 提示当前层级
        annotated = orchestrator.accelerator.annotate_track(  # 单条船舶的加速器标注结果
            ship, raw_points,  # 船舶与原始点
            water=water,  # 陆地判定器
            field=fleet.field if fleet else None,  # 流向/流速场（数据驱动超速与逆向航行）
            nearby=fleet.nearby_for(raw_points, ship.mmsi, orchestrator.accelerator.pipeline.config) if fleet else None,  # 邻近船（碰撞风险）
            extra_events=fleet.events_for(ship.mmsi) if fleet else None,  # 本船的多船事件（追越/让路/拖带）
        )

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
        win = None  # 本船的 23 维窗口特征（算不出来则 None → has_w=0）
        if win_feat is not None and win_tindex:  # 无同刻点表则整批不填，见上
            # 用**清洗后**的轨迹（`annotated.track`），与训练落盘 `winprod_*.json`
            # 走的同一条路（`dump_winfeat.py` 也是取 `AnnotatedTrack.track`）；
            # 换成原始点会让最近会遇距离看到已被清洗掉的点，属于训练/推理错配。
            win = win_feat.features_max(annotated.track, win_tindex)
            fv = win_feat.fill_reserved(fv, win)  # 写 reserved_0..22 + has_w
        # 门控判据（可用环境变量 GATE_MODE 一键回退，不需要改代码）：
        #   evidence（默认）：L1 无候选事件才送 LLM —— 注入集上三个场景全部
        #                     同时赢成本与质量（100%@27% / 67.4%@32.5% / 79.4%@44.2%，
        #                     现役判据是 64.3%@100%），详见 triage_engine/router.py 第 2b 步。
        #   risk            ：退回原来的风险分阈值判据。
        # 适用前提：真实数据上 L1 报掉 91% 的船，所以"无候选事件"≈"安静的船"、
        #           而不是"规则盲区"——该判据只能在规则覆盖良好的域单独使用；
        #           规则覆盖差的域必须与风险分门控组合，阈值按域标定（尚未标定）。
        #           收益是成本（调用率 6.2% vs 16.6~17.3%）。
        _gate = os.environ.get("GATE_MODE", "evidence").strip().lower()
        decision = router.route(  # ML路由分类决策
            feature_vector=fv,  # 特征向量
            mmsi=ship.mmsi,  # 船舶MMSI号
            n_events=len(annotated.events) if _gate == "evidence" else None,
        )

        # ML 结果输出
        probs = " | ".join(f"{k}: {v:.1%}" for k, v in sorted(decision.all_probabilities.items())) if decision.all_probabilities else ""  # 各分类概率字符串
        similar_info = f" | Milvus 相似: {len(decision.milvus_hits)} 条" if decision.milvus_hits else ""  # Milvus相似检索信息
        print(f"    ├ 分类: {decision.ml_category} | 置信度: {decision.ml_confidence:.1%}")  # ML分类结果和置信度
        print(f"    ├ 路由: {decision.routing}{similar_info}")  # 路由决策及相似轨迹信息
        print(f"    ├ 概率: {probs}")  # 各分类概率分布
        print(f"    ├ 窗口特征: {'23 维已填 (has_w=1)' if win else '未算出 (has_w=0)'}")  # reserved_0..22 是否带了值
        print(f"    └ 耗时: {decision.triage_latency_ms:.0f}ms")  # ML路由耗时（毫秒）

        # 构建 entry
        entry = annotated.to_agent_input()  # 转换为Agent输入格式
        entry["mmsi"] = ship.mmsi  # 船舶MMSI号
        entry["routing"] = decision.to_dict()  # 路由决策转为字典

        # ── 把判定所依据的东西交给 LLM（原先一个都没给）──
        # 23 维窗口特征具名化：模型吃的仍是上面那个定长 `fv`（64 维契约不动），
        # 这里只是**另存一份看得懂的**给 prompt 用。它含 min_sep_nm —— 与最近
        # 他船的实测会遇距离，而违规就住在两船的关系里。原先这项只存在于局部
        # 变量 `win` 里，出了这个函数就没了，prompt 侧无从取用。
        # （L2 的风险分/四类概率走 `RoutingDecision.to_dict()`，见 router.py。）
        entry["window_features"] = win_feat.named(win) if win_feat is not None else None

        # 如果跳 LLM，直接收集
        if args.no_llm:  # 如果指定了--no-llm参数（跳过LLM层）
            entry["pipeline_metrics"] = orchestrator.accelerator.metrics.summary()  # 加速器指标摘要
            results.append(entry)  # 直接保存结果（无LLM分析）
            print(f"    ▶ 结果: 路由={decision.routing} (置信度 {decision.ml_confidence:.0%})")  # 打印简化结果
            continue  # 跳过LLM层，处理下一艘船

        # ━━ 第三层：判定（LLM 或门控跳过后由 L1 证据给出） ━━
        _gate_in = bool(getattr(decision, "needs_llm", True))  # 门控是否放行
        print(f"  [3/3] 判定层 — 模板: {decision.llm_prompt_template} | "
              f"门控: {'放行 LLM' if _gate_in else '跳过 LLM'}")  # 提示当前层级与门控结论

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

        # ── 门控：要不要花 LLM 由 L2 的风险分决定 ──
        # `decision.needs_llm` 早就算好了（triage_engine/router.py 第 2b 步，
        # risk_score >= RISK_GATE_THRESHOLD），但**全仓原先没有任何地方读它** ——
        # 每船必调 LLM，实测报警率 92.6%、最终判定 lift 0.88（比乱抓还差），
        # 而这层门本来就是把 LLM 预算压在 L2 风险分最高的那一小撮船上。
        # 不放行的船判定交给 L1 实测证据（_verdict_without_llm），不花 LLM 预算。
        if _gate_in:
            llm_result = orchestrator._llm_with_memory(
                track=annotated,
                entry=entry,
                routing_decision=decision,
            )
        else:
            llm_result = orchestrator._verdict_without_llm(
                track=annotated,
                entry=entry,
                routing_decision=decision,
            )
            print(f"    ↷ 跳过 LLM（风险分 {getattr(decision, 'risk_score', 0) or 0:.4f} < "
                  f"{RISK_GATE_THRESHOLD}）→ 判定由 L1 实测证据给出")

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
            # 规范码优先取 orchestrator.classify() 的物化结果，而不是从 violation_type
            # 重新推导：classify() 以 L1 实测事件的 candidate_type 定码，而 violation_type
            # 是 LLM 的原始判性——L1 提升的场景下它被**刻意留空**（见 orchestrator.py
            # 「violation_type 留空：它代表"LLM 的原始判性"」），从空串推导必然得到
            # unknown，于是 L1 实锤的追越/逆向/碰撞全部落到 base_risk=30（全表最低档）。
            # 实测：classify("", L1追越事件) 正确给出 overtaking_violation(base_risk=65)，
            # 而 normalize_violation_code("") 给出 unknown(30)。检测越准判得越轻。
            _vcode = (llm_result.get("violation_code") or "").strip()
            if not _vcode or _vcode == "unknown":
                _vcode = (normalize_violation_code(_vtype_raw)
                          if llm_result.get("violation_detected") else "unknown")
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
            # L2 风险门控立的判定：规范码可能是 unknown（模型不产出违规类型，只给风险分），
            # unknown 的 base_risk=30 → 引擎会给出"违规但低风险"的自相矛盾行。
            # 与 L1 提升把 risk_level 抬到 mid 同一口径，这里用 extra_bonus 补到 mid（≥40）。
            _bonus = 15 if (llm_result.get("detection_source") == "l2_risk_gate"
                            and llm_result.get("violation_detected")) else 0
            _risk = assess_risk(
                violation_code=_vcode,
                evidence_count=_max_ev if llm_result.get("violation_detected") else 0,
                duration_minutes=_dur if llm_result.get("violation_detected") else 0.0,
                has_regulation=_has_reg,
                quality_level=_ql,
                llm_risk=_llm_risk,
                extra_bonus=_bonus,
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

        # ━━ 插件判定（dsh 风格可执行插件，附加式） ━━
        # 落点刻意选在这里：L1 事件、L2 路由、L3 LLM、risk_engine 的确定性覆盖
        # 全部已尘埃落定。插件拿到只读的 facts.base，代码层面没有写
        # violation_detected / violation_code 的入口；唯一能改的 risk_level
        # 需 can_raise_risk 授权且只升不降，并单独审计（见 registry._maybe_raise_risk）。
        # 异常一律吞掉：插件层出问题绝不许影响主判定。
        try:
            from plugin_runtime import run_adjudicators
            # 规则提供者挂在 pipeline 上（facade 自身没有 rules 属性 —— 写成
            # orchestrator.accelerator.rules 会静默拿到 None，于是 facts.zones 恒为空，
            # 所有依赖区域几何的插件在线上永远不命中。这个坑踩过一次：日志只会显示
            # 「本批启用 1 个插件」，然后一条产物都没有，看起来像插件没生效）。
            _rule_provider = getattr(getattr(orchestrator.accelerator, "pipeline", None),
                                     "rules", None)
            _pf = run_adjudicators(
                entry, llm_result, rules=_rule_provider,
                user_id=CURRENT_USER_ID, source_file=source_name, mmsi=ship.mmsi,
            )
            if _pf:
                entry["plugin_findings"] = _pf
                from plugin_runtime.registry import findings_summary
                print(f"    🧩 插件附加判定: {findings_summary(_pf)}")
        except Exception as _plugin_err:
            print(f"    ⚠ 插件判定失败（已忽略，不影响主判定）: {_plugin_err}")

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
                    -- 下面五列与 app.py 的 DB_TABLES_SQL 保持一致。此前这份 DDL 少了
                    -- violation_label / violation_event，而下面的 INSERT 又写了它们 ——
                    -- 表由本文件创建时（全新库 + 直接跑 CLI）INSERT 会因缺列直接失败。
                    violation_label TEXT DEFAULT '',
                    violation_event TEXT DEFAULT '',
                    violation_code TEXT DEFAULT '',
                    violation_subtype TEXT DEFAULT '',
                    violation_metric DOUBLE PRECISION DEFAULT 0,
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
                     violation_label, violation_event,
                     violation_code, violation_subtype, violation_metric,
                     llm_latency_seconds, detailed_result)
                VALUES (?, ?, ?,
                        ?,?,?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,
                        ?,?,
                        ?,?,?,
                        ?,?)
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
                # 三级分类（orchestrator 的 classify() 产出）：二级规范码 / 三级细类 /
                # 定档依据的实测量值。缺失时落空，接口层会退回按 violation_type 归一。
                (llm_result.get("violation_code", "") or "")[:64],
                (llm_result.get("violation_subtype", "") or "")[:64],
                llm_result.get("violation_metric", 0) or 0,
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
        # 只回流**真正被 LLM 判过**的：feedback 收的是弱标签，把 L1 立的判定当
        # LLM 结论喂进去，等于给训练集贴了错误来源的标签（数据必须是真实的）。
        if feedback and not llm_result.get("llm_skipped"):  # 反馈闭环可能为 None（LLM 不可用时）
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
    vio_types = {}  # 违规类型（规范码）→ 船数，用于看清类型是否真的分化
    for r in results:  # 遍历所有处理结果
        rt = r.get("routing", {}).get("routing", "?") if r.get("routing") else "?"  # 获取路由分类
        routes[rt] = routes.get(rt, 0) + 1  # 按路由类型计数
        if r.get("llm_result", {}).get("violation_detected"):  # 如果LLM检测到违规
            vios += 1  # 违规计数加1
            # 取与落库同一列（orchestrator 的 classify() 产出），口径和 detection_results 一致
            _vc = r.get("llm_result", {}).get("violation_code") or "unknown"
            vio_types[_vc] = vio_types.get(_vc, 0) + 1

    print(f"\n{'='*60}")  # 打印汇总分隔线
    print(f"  ✅ 文件处理完成: {len(results)} 条船舶")  # 打印完成提示
    print(f"{'='*60}")  # 打印底部分隔线
    print(f"  路由分布:")  # 打印路由分布标题
    for route, count in sorted(routes.items(), key=lambda x: -x[1]):  # 按数量降序排列
        bar = "█" * max(1, int(count / len(results) * 30))  # 用方块字符绘制柱状图
        print(f"    {route:<20} {bar} {count} ({count/len(results)*100:.0f}%)")  # 每类路由的数量和占比

    if not args.no_llm:  # 如果启用了LLM层
        print(f"  违规判定: {vios}/{len(results)} 艘违规 ({vios/max(len(results),1)*100:.1f}%)")  # 违规率统计
        if vio_types:  # 违规类型分布（与 detection_results.violation_code 同口径）
            _labels = {}
            try:
                from ais_accelerator.violation_catalog import CANONICAL
                _labels = {k: v.get("label", k) for k, v in CANONICAL.items()}
            except Exception:
                pass
            print(f"  违规类型分布:")  # 类型分化一眼可见，不必再去翻数据库
            for _code, _n in sorted(vio_types.items(), key=lambda x: -x[1]):
                print(f"    {_labels.get(_code, _code):<14} {_n} 艘  [{_code}]")
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