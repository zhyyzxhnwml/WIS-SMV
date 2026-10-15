"""
llm_layer/handler.py — LLM 调用处理器
=======================================

【这个文件做什么？】
  封装 LLM 的调用流程：
  1. 根据路由结果选择 Prompt 模板
  2. 渲染数据（轨迹/标记/事件/相似案例）填充模板
  3. 调用 LLM（Ollama 本地模型）
  4. 解析 JSON 结果
  5. 提取 feedback_for_ml

【数据渲染】
  - 截断轨迹点防止超长上下文（默认 ≤200 个点）
  - 生成事件摘要和规则命中摘要
  - 附上 pgvector/Milvus 相似案例供参考
  - 各字段均有字符数上限，防止爆上下文窗口

【错误处理】
  - LLM 不可用 → 抛出 RuntimeError，由调用方决定如何处理
  - JSON 解析失败 → 返回带 error 标记的字典
  - 调用异常 → 返回安全默认值（不报违规）
"""

from __future__ import annotations

import json  # JSON编解码模块
import threading  # 配置重建锁（防止并发重建竞态）
import time  # 时间模块（用于性能计时）
import urllib.request  # 轻量 HTTP 探测（不依赖 requests 库）
from typing import Any, Optional  # 类型提示

import llm_config  # 全局模型/参数配置（兼容层，开发者模式，调用时读取）
import llm_providers  # 「级联判定」用途绑定的实际提供商（阶段 1）
from .prompts import PROMPT_TEMPLATES  # 四级处理深度的Prompt模板字典

try:
    from langchain_ollama import ChatOllama  # Ollama LLM调用接口
    HAS_OLLAMA = True  # 是否成功导入Ollama库的标记
except ImportError:  # 导入失败
    HAS_OLLAMA = False  # Ollama库不可用标记

# [诊断用] 让初始化是否成功、失败在哪一步，直接打到标准输出（run.py 的 stdout 会透传到前端日志）
_LLM_DIAG = True

# 违规类型词表（惰性构建 + 缓存）。从目录生成而不是在 prompt 里手抄：
# 手抄一份就等于留了第二个真相源，目录一改 prompt 就悄悄过期——
# 而我们正在修的就是"LLM 自创违规码"这类词汇表漂移。
_VIOLATION_VOCAB_CACHE: Optional[str] = None

# ── 实测证据渲染（本模块唯一真正决定"LLM 判不判得准"的东西）────────────
# 【为什么要有这一段】旧版的 `_summarize_events` 只输出 `[priority] 类型 @ 区域`，
# **把 `ev["features"]` 整个丢掉了** —— 而那正是 `violation_catalog.
# deterministic_violation()` 用来定违规的量值（DCPA、实测间距、速差、超速比例）。
# 同理 `_summarize_rules` 只给 label，不给量值；`feature_metadata` 只用了时长，
# 平均/最高航速丢掉；23 维窗口特征（含 min_sep_nm）**一个字都没进过 prompt**。
# 结果是：我们问 LLM"这条船违不违规"，却把它据以回答的**数字全部删掉了**，
# 然后再拿"LLM 判定 lift 0.88"当它不行的证据。那不是模型的能力上限，
# 是我们自己管线造出来的信息瓶颈。
#
# 渲染顺序 = token 预算的分配顺序。排前面的是**判性依据**（与 PROMOTABLE 的
# 门槛字段同源），排后面的是上下文；超预算时先丢上下文，不丢判据。
_EV_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("dcpa_nm", "最近会遇距离DCPA", "海里"),
    ("min_separation_nm", "实测最小间距", "海里"),
    ("give_way_close_quarters_nm", "让路紧迫局面距离", "海里"),
    ("tcpa_minutes", "到最近会遇时间TCPA", "分"),
    ("cri", "碰撞危险度CRI", ""),
    ("max_speed_diff_knots", "追越最大速差", "节"),
    ("speed_diff_knots", "速差", "节"),
    ("speed_over_ratio", "超速比例", ""),
    ("max_speed", "最高速", "节"),
    ("average_speed", "平均速", "节"),
    ("speed_limit", "区域限速", "节"),
    ("reference_speed", "参照主流速", "节"),
    ("gap_minutes", "信号缺失时长", "分"),
    ("duration_minutes", "事件持续", "分"),
    ("anchoring_duration_minutes", "锚泊时长", "分"),
    ("course_change_degrees", "航向变化", "度"),
    ("course_rate_degrees_per_minute", "转向率", "度/分"),
    ("distance_nm", "两船距离", "海里"),
    ("relative_speed", "相对速度", "节"),
    ("relative_bearing", "相对方位", "度"),
    ("target_mmsi", "目标船MMSI", ""),
    ("elapsed_minutes", "历时", "分"),
    ("minimum_speed", "最低速", "节"),
    ("prior_median_speed", "此前后中位速", "节"),
    ("low_speed_threshold", "低速判定线", "节"),
    ("anchoring_speed_threshold", "锚泊速度线", "节"),
    ("point_count", "证据点数", "个"),
    ("merged_event_count", "合并事件数", "个"),
    ("judge", "检测器判定", ""),
    ("risk_level", "检测器风险档", ""),
    ("in_corridor", "在航道走廊内", ""),
    ("in_zone", "在区域内", ""),
)
_EV_MAX_FIELDS = 14   # 单事件最多渲染几个量值（按上表顺序取前 N 个）
_EV_MAX_EVENTS = 5    # 最多渲染几个事件（按严重度排序后）
_PRIO_RANK = {"high": 0, "middle": 1, "low": 2}

# 窗口特征渲染：23 维全给太长，只给**有信息量**的（"最长的一天最危险的那个窗"）。
# 全零/全 NaN 的维度（例如当天无他船时的 min_sep 退化为 NOBODY_NM）不占 token。
_WIN_LABELS = {
    "sog_mean": "窗内平均速(节)", "sog_std": "速标准差", "sog_min": "最低速(节)",
    "sog_max": "最高速(节)", "dsog_mean": "平均加速度(节/步)", "dsog_max": "最大加速度",
    "cog_circstd": "航向圆标准差(度)", "turn_mean": "平均转向(度)",
    "turn_max": "最大转向(度)", "turnrate_max": "最大转向率(度/分)",
    "turn_net": "净转向(度)", "path_len": "路径长(海里)", "net_disp": "净位移(海里)",
    "straightness": "直线度", "n_points": "点数", "interval": "采样间隔(秒)",
    "disp_rate_mean": "平均位移率", "disp_rate_std": "位移率标准差",
    "cog_sin": "航向正弦", "cog_cos": "航向余弦",
    "min_sep_nm": "最近会遇距离(海里)", "sep_p5_nm": "会遇距离5分位(海里)",
    "frac_within_1nm": "1海里内占比",
}


def _num(v: Any) -> str:
    """数值 → 紧凑可读串（bool 走中文，避免 prompt 里出现 True/False 混排）。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return str(v)
        if f == int(f) and abs(f) < 1e9:  # 24.0 → "24"（点数/条数这类整数量值）
            return str(int(f))
        if abs(f) >= 100:
            return f"{f:.0f}"
        if abs(f) >= 10:
            return f"{f:.1f}"
        return f"{f:.3f}".rstrip("0").rstrip(".")
    return str(v)


def violation_vocab() -> str:
    """渲染违规类型词表，供 prompt 的 {violation_vocab} 占位符填充。"""
    global _VIOLATION_VOCAB_CACHE
    if _VIOLATION_VOCAB_CACHE is not None:
        return _VIOLATION_VOCAB_CACHE
    try:
        from ais_accelerator.violation_catalog import CANONICAL, CATEGORY_MAP
        lines = []
        for code, info in CANONICAL.items():
            if code == "unknown":
                continue  # 兜底码放最后单独说明，不混在可选列表里
            cat = CATEGORY_MAP.get(info.get("category", ""), info.get("category", ""))
            lines.append(f"- {code}：{info.get('label', '')}（{cat}）")
        lines.append("- unknown：其他违规（无法归入以上任何一类时才用）")
        _VIOLATION_VOCAB_CACHE = "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — 词表拿不到不该让判定整个失败
        # 不静默降级成空串：那等于把"LLM 自创违规码"的老问题原样放回来，
        # 而且现场看不出任何痕迹。宁可吵一声。
        print(f"    ⚠ 违规类型词表构建失败，prompt 将不含可选值约束: {e}")
        _VIOLATION_VOCAB_CACHE = ""
    return _VIOLATION_VOCAB_CACHE


def _quality_level_of(qr: dict) -> str:
    """从质量报告的 issues 推导综合质量等级（good/fair/poor）。

    注意：QualityReport.level 是 @property，model_dump(mode='json') 不会序列化它
    （run.py 写 quality_level 列时直接访问 .level），此处必须从 issues 自己推导。
    """
    if not qr:
        return "good"
    sevs = {i.get("severity") for i in qr.get("issues", [])}
    if "critical" in sevs:
        return "poor"
    if "warning" in sevs:
        return "fair"
    return "good"


class LLMHandler:
    """LLM 分层处理器。

    职责：根据 ML Triage 的路由结果，选择合适的 Prompt 模板，
    渲染数据，调用 LLM，解析 JSON 结果，并提取 feedback。

    重要：本系统必须依赖 LLM 模型运行，不支持无模型工作。
    模型未启动时，analyze() 会抛出 RuntimeError。
    """

    def __init__(
        self,
        model_name: str = "qwen2.5:7b-instruct-q4_K_M",  # LLM模型名称（默认Qwen2.5量化版）
        base_url: str = "http://localhost:11434",  # Ollama服务地址（默认本地端口）
        temperature: float = 0.0,  # LLM采样温度（0=确定性输出）
    ):
        self.model_name = model_name  # LLM模型名称
        self.base_url = base_url  # Ollama服务地址
        self.temperature = temperature  # 采样温度
        self.llm = None  # LLM客户端实例（初始为None，延迟初始化）
        self._model_available = False  # 模型是否可用
        self._cfg_snapshot = None  # 配置快照（比对是否变化，变化则重建客户端）
        self._llm_lock = threading.Lock()  # 重建锁（防止并发重建竞态）
        # 判定链路的提供商类型：ollama 走 langchain ChatOllama；其余走 llm_providers 出口
        self._kind = "ollama"
        self._api_key = ""
        self._provider_name = ""
        self._init_llm()  # 尝试初始化 LLM
        self._cfg_snapshot = self._config_signature()  # 记录初始化时的配置快照

    def _ollama_up(self, timeout: float = 8.0) -> bool:
        """真实探测 Ollama 服务是否已能响应（HTTP 层，非 gRPC）。

        注意：Ollama 启动早期 HTTP 端点会返回 500/拒连（服务尚未就绪），
        这符合“已启动但此刻尚不稳定”的真实场景；能连上并读到模型列表才算可用。
        timeout 由 3s 提到 8s：某些机器上 Ollama 服务虽已启动、但模型首次载入时，
        /api/tags 响应会被并发地或启动期阻塞超过 3s，导致可用被误判为不可用。
        """
        if _LLM_DIAG:
            print(f"    [LLM-diag] _ollama_up({timeout}s) 探测 {self.base_url}/api/tags ...", flush=True)
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                ok = resp.status == 200 and b"models" in body
                if _LLM_DIAG:
                    print(f"    [LLM-diag] _ollama_up → status={resp.status} 读到模型列表={ok} (len={len(body)})", flush=True)
                return ok
        except Exception as e:  # 联网/解析异常
            if _LLM_DIAG:
                print(f"    [LLM-diag] _ollama_up → FAILED {type(e).__name__}: {e}", flush=True)
            return False

    def _init_llm(self):
        """尝试初始化判定用的 LLM 客户端（可重复调用，用于自动恢复 / 配置变更重建）。

        Ollama：先做一次 HTTP 连通性探测，能响应了才构造客户端；只有连通探测成功
        才标记模型可用，避免“构造成功=可用”的假阳性。本次探测失败（Ollama 仍在启动
        早期）仅返回而不把状态钉死成 False，以便外层（analyze/_call_llm）下次调用时
        能再次尝试恢复。

        兼容网关/云端（阶段 1）：没有 /api/tags 这种免费探针，探活要花一次真实调用
        （要花钱），所以不预探 —— 直接标记可用，把错误留给真实调用暴露。失败会经
        _call_llm 的异常分支变成 risk_unverified，不会被伪装成「低风险正常」。

        开发者模式：模型名/地址/参数从「级联判定」用途绑定读取（调用时读取），
        配置变更后由 _ensure_llm() 触发重建，无需重启。
        """
        if _LLM_DIAG:
            print(f"    [LLM-diag] _init_llm() HAS_OLLAMA={HAS_OLLAMA}", flush=True)
        # 从「级联判定」绑定的提供商读取（读不到回退实例属性）
        try:
            r = llm_providers.resolve("judge")
            p = r.get("params") or {}
            self.model_name = r.get("model") or self.model_name
            self.base_url = r.get("base_url") or self.base_url
            self.temperature = float(p.get("temperature", self.temperature))
            self._num_predict = p.get("num_predict")
            self._top_p = p.get("top_p")
            self._top_k = p.get("top_k")
            self._kind = r.get("kind") or "ollama"
            self._api_key = r.get("api_key") or ""
            self._provider_name = r.get("provider_name") or ""
        except Exception:
            self._num_predict = None
            self._top_p = None
            self._top_k = None
            self._kind = "ollama"
            self._api_key = ""

        if self._kind != "ollama":
            if not self._api_key:
                self.llm = None
                self._model_available = False
                if _LLM_DIAG:
                    print(f"    [LLM-diag] 判定提供商 {self._kind} 缺 API 密钥 → 不可用", flush=True)
                return
            self.llm = None  # 不走 langchain，_call_llm 里改走 llm_providers.complete
            self._model_available = True
            if _LLM_DIAG:
                print(f"    [LLM-diag] _init_llm → kind={self._kind} provider={self._provider_name} "
                      f"model={self.model_name}, model_available=True", flush=True)
            return

        if not HAS_OLLAMA:
            return
        try:
            # 先探测服务是否真的能响应；Ollama 启动早期会 500/拒连 → 不判可用
            if not self._ollama_up():
                self.llm = None
                # 不再强制置 False —— 为下次调用保留复活机会？不能：探测失败就保持“不可用”。
                # 见 _call_llm：不可用时会 `sleep` 后再次 _init_llm，若这里不置 False，
                # 下次循环的 `not self._model_available` 判 False 会误走 invoke。
                # 因此这里必须把状态置为不可用，但重试由 _call_llm 驱动。
                self._model_available = False
                return
            llm_kwargs = dict(  # 初始化Ollama聊天客户端
                model=self.model_name,  # 模型名称
                base_url=self.base_url,  # 服务地址
                temperature=self.temperature,  # 采样温度
            )
            if self._num_predict:  # 非空才传，避免覆盖 LangChain 默认
                llm_kwargs["num_predict"] = self._num_predict
            if self._top_p:  # 非空才传
                llm_kwargs["top_p"] = self._top_p
            if self._top_k:  # 非空才传
                llm_kwargs["top_k"] = self._top_k
            self.llm = ChatOllama(**llm_kwargs)
            self._model_available = True
            if _LLM_DIAG:
                print(f"    [LLM-diag] _init_llm → OK, model={self.model_name}, temp={self.temperature}, model_available=True", flush=True)
        except Exception as e:  # 初始化异常
            self.llm = None
            self._model_available = False
            if _LLM_DIAG:
                print(f"    [LLM-diag] _init_llm → EXC {type(e).__name__}: {e}", flush=True)

    def _ready(self) -> bool:
        """判定链路是否就绪。Ollama 还要求客户端已构造；其它类型只看可用标记。"""
        if self._kind != "ollama":
            return bool(self._model_available)
        return bool(self._model_available and self.llm is not None)

    def _config_signature(self) -> tuple:
        """「级联判定」绑定的签名（影响客户端的字段；用于变更检测）。"""
        r = llm_providers.resolve("judge")
        p = r.get("params") or {}
        return (
            r.get("kind"),
            r.get("base_url"),
            r.get("model"),
            p.get("temperature"),
            p.get("num_predict"),
            p.get("top_p"),
            p.get("top_k"),
            bool(r.get("api_key")),  # 只记「有没有密钥」，密钥本身不进签名
        )

    def _ensure_llm(self):
        """配置变更检测：签名与上次初始化不一致则加锁重建客户端（即时生效）。"""
        sig = self._config_signature()
        if self._cfg_snapshot == sig:
            return
        with self._llm_lock:
            # 双重检查：锁内再比对一次，避免拿到锁后发现已被其他线程重建
            if self._cfg_snapshot == sig:
                return
            self._cfg_snapshot = sig
            self._init_llm()

    @property
    def model_available(self) -> bool:
        """LLM 模型是否可用。"""
        return self._ready()

    def analyze(
        self,
        annotated_track: dict,  # 加速器输出的带标记轨迹字典
        routing: dict,  # 路由决策字典（RoutingDecision.to_dict()）
        max_track_points: int = 200,  # 轨迹点截断数（防止超长上下文）
    ) -> dict:
        """对一条带标记的轨迹做 LLM 判定。

        Args:
            annotated_track: 加速器输出的 AnnotatedTrack.to_agent_input()
            routing: 路由决策字典（RoutingDecision.to_dict()）
            max_track_points: 轨迹点截断数（防止超长上下文）

        Returns:
            dict: LLM 判定结果 + feedback
        """
        prompt_template = routing.get("llm_prompt_template", "quick_verify")  # 从路由中获取Prompt模板名称
        prompt_text = PROMPT_TEMPLATES.get(prompt_template, PROMPT_TEMPLATES["quick_verify"])  # 获取对应的Prompt模板文本

        # ── 渲染数据 ──
        rendered = self._render_prompt(  # 将数据渲染到Prompt模板中
            prompt_text,  # Prompt模板文本
            annotated_track,  # 带标记的轨迹数据
            routing,  # 路由决策信息
            max_track_points,  # 轨迹点截断数
        )

        # ── 调用 LLM ──
        started = time.perf_counter()  # 记录调用开始时间（高精度计时）
        result = self._call_llm(rendered)  # 调用LLM并获取结果字典
        latency = time.perf_counter() - started  # 计算LLM调用耗时（秒）

        # ── 标准化输出 ──
        result["routing"] = routing  # 将路由信息附加到结果中
        result["latency_seconds"] = round(latency, 3)  # 记录调用延迟（保留3位小数）
        result["model"] = self.model_name  # 记录使用的模型名称
        result["prompt_template"] = prompt_template  # 记录使用的Prompt模板名称

        # ── 数据质量标注（只加标注，不硬改判定；供前端徽章/审计）──
        qr = (annotated_track or {}).get("quality_report") or {}  # 质量报告（可能为空）
        dql = _quality_level_of(qr)  # 推导质量等级（good/fair/poor）
        cleaned = int(qr.get("invalid_points", 0) or 0)  # 清洗剔除点数
        result["data_quality_level"] = dql  # 数据质量等级
        result["data_quality_suspect"] = bool(dql != "good" or cleaned > 0)  # 有任何质量问题或剔除过点 → 数据存疑

        # ── 确保 feedback 存在 ──
        if "feedback_for_ml" not in result:  # 反馈字段不存在
            result["feedback_for_ml"] = {}  # 确保反馈字段存在（防止后续处理出错）

        return result  # 返回LLM判定结果

    def _summarize_quality(self, qr: dict) -> str:
        """生成数据质量摘要（注入 LLM prompt，让判定考虑数据可信度）。"""
        if not qr:
            return "无质量评估数据"
        level = _quality_level_of(qr)
        parts = [f"数据质量等级: {level}"]
        bits = []
        if qr.get("duplicate_points"):
            bits.append(f"重复点{qr['duplicate_points']}个")
        if qr.get("invalid_points"):
            bits.append(f"剔除异常点{qr['invalid_points']}个")
        if qr.get("retained_points") is not None:
            bits.append(f"保留{qr['retained_points']}点")
        if bits:
            parts.append("，".join(bits))
        codes = sorted({i.get("code") for i in qr.get("issues", [])})
        if codes:
            parts.append("问题: " + ",".join(codes))
        return "；".join(parts)

    def _render_prompt(
        self,
        template: str,  # Prompt模板文本
        track: dict,  # 轨迹数据字典
        routing: dict,  # 路由决策字典
        max_points: int,  # 最大轨迹点数（截断限制）
    ) -> str:
        """用数据填充 Prompt 模板。"""
        track_points = track.get("track", [])  # 从轨迹字典中提取轨迹点列表
        annotations = track.get("annotations", [])  # 从轨迹字典中提取标记列表
        events = track.get("events", [])  # 从轨迹字典中提取事件列表
        quality_report = track.get("quality_report", {})  # 从轨迹字典中提取数据质量报告
        feature_meta = track.get("feature_metadata", {})  # 从轨迹字典中提取特征元数据
        quality_summary = self._summarize_quality(quality_report)  # 数据质量摘要（注入模板，供判定参考数据可信度）

        # 事件摘要
        event_summary = self._summarize_events(events)  # 生成事件摘要字符串
        rule_hits = self._summarize_rules(annotations)  # 生成规则命中摘要字符串

        # 轨迹数据（截断防止超长）
        if len(track_points) > max_points:  # 轨迹点超过限制
            step = len(track_points) // max_points  # 计算抽样步长（等间隔采样）
            track_points = track_points[::step][:max_points]  # 等间隔采样并截断到最大点数
        track_data = json.dumps(track_points, ensure_ascii=False, default=str)  # 将轨迹点序列化为JSON字符串

        # 标记明细
        annotations_text = json.dumps(annotations, ensure_ascii=False,  # 将标记数据序列化为JSON字符串
                                       indent=2, default=str)

        # 相似案例
        similar = routing.get("pgvector_similar", []) + routing.get("milvus_similar", [])  # 合并PGVector和Milvus的相似案例列表
        similar_count = len(similar)  # 相似案例数量
        similar_text = json.dumps(similar, ensure_ascii=False, indent=2, default=str)[:2000]  # 序列化相似案例（截断前2000字符）

        # 平均置信度
        confidences = [a.get("confidence", 0) for a in annotations]  # 提取所有标记的置信度列表
        avg_conf = sum(confidences) / len(confidences) if confidences else 0  # 计算平均置信度

        # 时间范围
        # 旧版把 `start_time` 绑在 `total_duration_minutes` 上（那是**时长**，不是时刻），
        # 而 `end_time=start_time` —— 注释自己都写了"实际应取最后一个点的时间"。
        # 于是 prompt 里写的是"从 340 到 340 的完整轨迹"。时刻是可核对的量，
        # 而这里是唯一提供它的地方（轨迹 JSON 里的 timestamp 被 8000 字符截断，
        # 长轨迹的首尾根本进不了 prompt）。取首末点，退化时才回落到时长。
        times = [str(p.get("timestamp")) for p in track_points if p.get("timestamp")]
        if times:
            start_time, end_time = times[0][:19].replace("T", " "), times[-1][:19].replace("T", " ")
        else:
            _dur = feature_meta.get("total_duration_minutes")
            start_time = end_time = f"未知（时长 {_num(_dur)} 分钟）" if _dur else "?"
        point_count = len(track.get("track", []))  # 轨迹点总数

        has_annotations = "是" if annotations else "否"  # 是否有标记的文本表示

        # 法规上下文（从 track 中提取，由 orchestrator 注入）
        regulation_context = track.get("_regulation_context", "")  # 法规上下文文本

        # ── 补回判性所需的证据（见模块头 _EV_FIELDS 的说明）──
        # 这些量值原先全部不在 prompt 里：事件量值被 _summarize_events 丢掉、
        # 23 维窗口特征从未注入、航速统计只用了时长。三条各自独立缺失，
        # 合起来就是"让 LLM 蒙着眼睛判违规"。
        measured = [
            f"【L1 实测事件】\n{event_summary}",
            f"【L1 硬证据核对】\n{self._hard_evidence_line(events)}",
            f"【L1 规则命中】\n{rule_hits}",
            f"【本船当天最危险窗口的窗口特征】\n{self._summarize_window(track.get('window_features'))}",
            f"【本船航速/时长统计】\n{self._summarize_stats(feature_meta)}",
        ]
        measured_evidence = "\n".join(measured)
        triage_context = self._summarize_triage(routing)

        return template.format(
            event_summary=event_summary,  # 事件摘要内容
            annotation_confidence=f"{avg_conf:.2%}",  # 平均置信度（百分比格式）
            rule_hits=rule_hits,  # 规则命中摘要
            measured_evidence=measured_evidence,  # 实测证据块（事件量值+窗口特征+速度）
            triage_context=triage_context,  # L2 风险分与门控依据
            regulation_context=regulation_context,  # 法规上下文
            track_data=track_data[:8000],  # 轨迹JSON数据（截断到8000字符）
            annotations=annotations_text[:3000],  # 标记明细（截断到3000字符）
            similar_cases=similar_text,  # 相似案例文本
            similar_count=similar_count,  # 相似案例数量
            start_time=start_time,  # 开始时间（首点时刻）
            end_time=end_time,  # 结束时间（末点时刻）
            point_count=point_count,  # 轨迹点总数
            has_annotations=has_annotations,  # 是否有标记
            quality_summary=quality_summary,  # 数据质量摘要
            # 违规类型可选值。只有三个模板带这个占位符；quick_verify 的模板里
            # 没有它，多传一个未使用的 kwarg 对 str.format 是安全的（会被忽略）。
            violation_vocab=violation_vocab(),
        )

    def _summarize_events(self, events: list[dict]) -> str:
        """事件摘要：**类型 + 区域 + L1 实测到的量值**。

        旧版只给 `[priority] 类型 @ 区域`，把 `features` 丢了 —— 于是 LLM 永远
        看不到"最近会遇 0.09 海里"这类判性依据。这里补回来，并按严重度排序后
        截断（`deterministic_violation` 取的是最严重的那类，前 3 条按原始顺序取
        很可能正好把决定性事件排在窗口外）。
        """
        if not events:  # 无事件
            return "未检测到候选事件"  # 返回默认信息
        ranked = sorted(  # 严重度高在前；同级保持原始顺序（sorted 稳定）
            enumerate(events),
            key=lambda kv: _PRIO_RANK.get(str(kv[1].get("priority") or "middle"), 1),
        )
        summaries = []
        for _, ev in ranked[:_EV_MAX_EVENTS]:
            etype = ev.get("candidate_type", ev.get("type", "unknown"))  # 事件类型
            # `ev.get("zone_id", 默认)` 在**键存在但值为 None** 时返回 None，
            # 于是 prompt 里印出 "@ None"（实测）。逐级 or 兜到底。
            zone = ev.get("zone_name") or ev.get("zone_id") or "未知区域"
            priority = ev.get("priority", "middle")  # 事件优先级
            feats = ev.get("features") or {}
            vals = self._render_measurements(feats)
            span = self._event_span(ev)
            line = f"[{priority}] {etype} @ {zone}{span}"
            if vals:
                line += f" —— 实测量值: {vals}"
            summaries.append(line)
        if len(events) > _EV_MAX_EVENTS:  # 事件数超出渲染上限
            summaries.append(f"... 还有 {len(events) - _EV_MAX_EVENTS} 个事件")
        return "\n".join(summaries)  # 一行一个事件（量值多，分号连会糊成一团）

    @staticmethod
    def _event_span(ev: dict) -> str:
        """事件的起止时间（有就给，没有就空）。"""
        s, e = ev.get("start_time") or ev.get("start"), ev.get("end_time") or ev.get("end")
        s, e = (str(s)[11:16] if s else ""), (str(e)[11:16] if e else "")
        return f" {s}~{e}" if s and e else ""

    @staticmethod
    def _render_measurements(feats: dict) -> str:
        """事件 features → `中文名=值单位` 列表（按 _EV_FIELDS 的判据优先序）。"""
        if not isinstance(feats, dict) or not feats:
            return ""
        seen = set()
        out = []
        for key, label, unit in _EV_FIELDS:
            if key not in feats or feats[key] is None:
                continue
            seen.add(key)
            out.append(f"{label}={_num(feats[key])}{unit}")
            if len(out) >= _EV_MAX_FIELDS:
                break
        if len(out) < _EV_MAX_FIELDS:  # 表外字段兜底（目录新加了量值也不至于丢）
            for key, v in feats.items():
                if key in seen or v is None or isinstance(v, (dict, list)):
                    continue
                out.append(f"{key}={_num(v)}")
                if len(out) >= _EV_MAX_FIELDS:
                    break
        return "，".join(out)

    def _summarize_rules(self, annotations: list[dict]) -> str:
        """规则命中摘要：**label + 码 + 该规则实际测到的量值**。

        旧版只印 label，于是"命中规则: 违规追越"到 LLM 眼里等于没有依据 ——
        追越的门槛是速差 ≥5 节，而那个速差就在 `detail` 里，原样丢掉了。
        """
        rules = [a for a in annotations if a.get("type") == "rule_hit"]  # 提取所有规则命中类型的标记
        if not rules:  # 无命中规则
            return "无命中规则"  # 返回默认信息
        lines = []
        for r in rules[:5]:
            name = r.get("label") or r.get("violation_code") or "?"
            code = r.get("violation_code") or ""
            det = r.get("detail") or {}
            # `detail` 里既有**实测量值**也有元数据（rule_id/zone_id/zone_name/
            # violation_code/description …）。元数据对判性无用却占 token ——
            # 实测把整份 detail 灌进去，prompt 里出现的是
            # "rule_id=ais_gap，violation_code=ais_gap，zone_id=None，…"，
            # 而真正该看的量值一个都没有。这里只留数值型与简短文本。
            _META = {"rule_id", "zone_id", "zone_name", "violation_code",
                     "source", "validity_status", "speed_limit",
                     "event_id", "segment_id"}
            bits, texts = [], []
            for k, v in det.items():
                if k in _META or v is None or isinstance(v, (dict, list)):
                    continue
                if isinstance(v, (bool, int, float)):
                    bits.append(f"{k}={_num(v)}")
                elif isinstance(v, str) and len(v) <= 60:
                    texts.append(f"{k}={v}")
            bits = (bits + texts)[:6]
            line = f"{name}" + (f"（{code}）" if code and code != name else "")
            if bits:
                line += "：实测量值 " + "，".join(bits)
            desc = det.get("description")
            if isinstance(desc, str) and desc and not bits:
                line += f"：{desc[:60]}"
            conf = r.get("confidence")
            if isinstance(conf, (int, float)):
                line += f"（置信度 {_num(conf)}）"
            lines.append(line)
        if len(rules) > 5:
            lines.append(f"... 另有 {len(rules) - 5} 条规则命中")
        return "\n".join(lines)

    @staticmethod
    def _summarize_window(win: Any) -> str:
        """23 维窗口特征（该船当天**最危险那个 20 分钟窗**的 max 聚合）。

        生产入参是 `entry["window_features"]`（run.py 用 window_feat.named() 具名化）。
        旧版这一整块**从未进过 prompt**，而它恰恰是 L2 模型最依赖的输入
        （`min_sep_nm` = 与最近他船的实测会遇距离，违规就住在两船的关系里）。
        """
        if not win:
            return "未算出（该船当天点数不足以凑满 20 分钟窗）"
        items = win.items() if isinstance(win, dict) else enumerate(win)
        out = []
        for k, v in items:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f != f:  # NaN = 当天无他船可比 → 不占 token
                continue
            out.append(f"{_WIN_LABELS.get(str(k), str(k))}={_num(f)}")
        return "，".join(out) if out else "无可用的窗口特征"

    @staticmethod
    def _summarize_stats(feature_meta: dict) -> str:
        """航速/时长统计。旧版只用 `total_duration_minutes`，平均与最高航速全丢。"""
        if not isinstance(feature_meta, dict) or not feature_meta:
            return "无"
        prefer = (
            ("avg_speed", "平均航速(节)"), ("max_speed", "最高航速(节)"),
            ("sog_std", "航速标准差(节)"), ("speed_variance", "航速方差"),
            ("total_duration_minutes", "总时长(分)"), ("total_distance_nm", "总航程(海里)"),
            ("point_count", "轨迹点数"), ("original_point_count", "原始轨迹点数"),
            ("course_changes", "航向变化次数"),
        )
        out, seen = [], set()
        for k, label in prefer:
            if k in feature_meta and feature_meta[k] is not None:
                seen.add(k)
                out.append(f"{label}={_num(feature_meta[k])}")
        for k, v in feature_meta.items():
            if k in seen or v is None or isinstance(v, (dict, list)):
                continue
            # 标注计数/置信度是**系统自己的输出**，不是本船的量测；纪律段已明说
            # "标记置信度是模型的输出，不是证据"。灌进去只会诱导模型顺着系统走。
            if "annotation" in k or "confidence" in k:
                continue
            out.append(f"{k}={_num(v)}")
            if len(out) >= 12:
                break
        return "，".join(out) if out else "无"

    @staticmethod
    def _hard_evidence_line(events: list[dict]) -> str:
        """把 L1 的**硬证据**结论单列一行：命中了哪个码、依据哪个量值、门槛多少。

        这不是替 LLM 下结论，而是把"系统为什么认为有问题"讲清楚 —— 原先 LLM
        只被告知"加速器检测到 XX 事件"，既不知道量值，也不知道门槛，无法核实。
        """
        try:
            from ais_accelerator.violation_catalog import PROMOTABLE, deterministic_violation
            hit = deterministic_violation(events)
        except Exception:  # noqa: BLE001 — 渲染不出证据不该让判定整体失败
            return ""
        if not hit:
            code = ""
            for ev in events or []:
                code = str(ev.get("candidate_type") or ev.get("type") or "")
                if code:
                    break
            return (f"未达硬证据门槛：系统检测到候选事件（{code or '无'}），"
                    f"但没有任一候选过 L1 的确定性门槛，故无硬证据。")
        gate = PROMOTABLE.get(hit.get("code") or "")
        why = ""
        if gate and hit.get("feature"):
            why = f"，依据 {hit['feature']}={_num(hit['value'])}（门槛 {_num(gate[1])}）"
        elif hit.get("code") in PROMOTABLE:
            why = "，该类型成立即硬证据（区域/行为事实，无数值门槛）"
        return (f"**已命中硬证据**：{hit.get('code')}{why}"
                f"，同类硬证据共 {hit.get('count')} 条。")

    @staticmethod
    def _summarize_triage(routing: dict) -> str:
        """L2 级联门控的判定依据：风险分、四类概率、门控结论。

        旧版没进 prompt，于是 LLM 不知道"自己为什么被叫来"，也不知道系统的
        先验有多强 —— 它只能从轨迹形状猜，而那是最弱的一条线索。
        """
        if not isinstance(routing, dict) or not routing:
            return "无"
        bits = []
        cat = routing.get("ml_category")
        if cat:
            bits.append(f"L2 分类={cat}")
        if routing.get("ml_confidence") is not None:
            bits.append(f"分类置信度={_num(routing.get('ml_confidence'))}")
        if routing.get("risk_score") is not None:
            bits.append(f"风险分={_num(routing.get('risk_score'))}")
        probs = routing.get("all_probs") or routing.get("all_probabilities")
        if isinstance(probs, dict) and probs:
            bits.append("四类概率 " + "，".join(
                f"{k}={_num(v)}" for k, v in sorted(probs.items())))
        if routing.get("needs_llm") is not None:
            bits.append(f"门控={'放行 LLM' if routing.get('needs_llm') else '已跳过'}")
        return "，".join(bits) if bits else "无"

    def _call_llm(self, prompt: str) -> dict:
        """调用 LLM 并解析 JSON 结果。

        LLM 不可用时自动尝试重连，仍失败则返回安全默认值（不抛异常）。
        """
        self._ensure_llm()  # 配置变更检测：不一致则重建客户端（开发者模式即时生效）
        # 尝试自动恢复：Ollama 可能仍在启动早期（HTTP 500/拒连），
        # 用带重试的初始化来覆盖启动窗口，避免启动期一次失败被永久钉死为不可用。
        max_retry = 5  # 最多重试次数
        for attempt in range(max_retry):
            if not self._ready():
                self._init_llm()  # 尝试重新连接（Ollama 内含 HTTP 探测）
            if self._ready():
                break  # 已就绪，跳出
            if attempt < max_retry - 1:  # 非最后一次才等待（Ollama 秒级启动）
                time.sleep(2)  # 起步稳定期稍短，后续增长
        if not self._ready():
            return {  # LLM 不可用，返回安全默认值（mid+unverified，避免伪装成"低风险正常"）
                "violation_detected": False,
                "violation_type": None,
                "violation_label": "LLM不可用",
                "confidence": 0.0,
                "risk_level": "mid",
                "risk_unverified": True,
                "agrees_with_triage": None,
                "disagreement_reason": "LLM模型不可用，无法完成分析",
                "final_assessment": "LLM模型不可用，跳过判定",
                "recommended_action": "manual_review",
                "feedback_for_ml": {},
                "error": "llm_unavailable",
            }

        try:
            if self._kind != "ollama":
                # 兼容网关/云端：走统一出口（_kind 已在 _init_llm 里连同密钥一起备好）
                text, err = llm_providers.complete(prompt, purpose="judge", timeout=120)
                if err:
                    raise RuntimeError(err)
                return self._parse_json(text)  # 解析并返回JSON结果
            from langchain_core.messages import HumanMessage  # 人类消息类
            response = self.llm.invoke([  # 调用LLM获取响应
                HumanMessage(content=prompt)  # 以人类消息形式传入Prompt
            ])
            text = response.content  # 提取LLM返回的文本内容
            return self._parse_json(text)  # 解析并返回JSON结果
        except Exception as e:  # 异常对象
            return {  # 返回安全默认值（mid+unverified，避免伪装成"低风险正常"）
                "violation_detected": False,  # 未检测到违规
                "violation_type": None,  # 无违规类型
                "violation_label": "分析异常",  # 标记为分析异常
                "confidence": 0.0,  # 置信度为0
                "risk_level": "mid",  # 风险等级为中（故障兜底，不可信）
                "risk_unverified": True,
                "agrees_with_triage": None,  # 无法判断一致性
                "disagreement_reason": f"LLM调用异常: {str(e)}",  # 异常原因描述
                "final_assessment": "因模型调用异常，无法完成分析",  # 最终评估说明
                "recommended_action": "manual_review",  # 建议人工复核
                "feedback_for_ml": {},  # 空反馈字典
                "error": str(e),  # 原始异常信息
            }

    @staticmethod
    def _fail_closed(reason: str, raw: str = "") -> dict:
        """拿不到可信 LLM 判定时的统一口径：不谎报违规，但标记不可信并转人工。

        为什么必须 fail-closed：下游（run.py 的风险引擎、前端风险面板）都拿
        violation_detected 当"LLM 判定无违规"用。若失败时返回 {} 或只带
        violation_detected=False，那么"LLM 想说有违规、只是格式没对上"会被静默
        降级成"没有违规"，违规就此放行——而且现场没有任何痕迹。
        risk_unverified 前端会渲染成「未核实」（frontend/backend/app.py 已消费），
        所以这些行能真正走到人工，而不是消失在"正常"里。
        """
        return {
            "violation_detected": False,  # 不谎报违规——下面两行说明它不可信
            "violation_type": None,
            "violation_label": "判定不可用",
            "confidence": 0.0,
            "risk_level": "mid",       # 不可信，不落在 low
            "risk_unverified": True,   # 前端「未核实」标记
            "agrees_with_triage": None,
            "disagreement_reason": reason,
            "final_assessment": reason,
            "recommended_action": "manual_review",
            "feedback_for_ml": {},
            "error": raw[:200] if raw else "",
        }

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从 LLM 回复中提取 JSON。空回复与解析失败一律 fail-closed。"""
        if not text:  # 空文本：LLM 没吐任何内容，同样不可信
            return LLMHandler._fail_closed("LLM 返回空回复，判定不可信")
        text = text.strip()  # 去除首尾空白
        # 去掉 markdown 包裹
        if text.startswith("```"):  # Markdown代码块标记
            start = text.find("{")  # JSON对象起始位置
            end = text.rfind("}")  # JSON对象结束位置
            if start != -1 and end != -1:  # 找到JSON花括号
                text = text[start:end+1]  # 提取纯JSON部分
        try:
            return json.loads(text)  # 解析JSON字符串为字典
        except json.JSONDecodeError:  # JSON解析失败
            return LLMHandler._fail_closed(
                "LLM 回复不是合法 JSON，判定不可信", f"JSON解析失败: {text}")

