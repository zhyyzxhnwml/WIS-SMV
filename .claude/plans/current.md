# 三级级联系统 -> 整体智能体：架构转型方案

## 一、当前架构回顾

### 现状：三级"管道级联"
```
L1. ais_accelerator  ── 数据清洗/特征提取/事件检测（规则驱动）
L2. TriageRouter     ── ML分类/置信度路由/向量检索
L3. LLMHandler       ── 分级Prompt/LLM判定/反馈提取
```

问题：
- **各层之间是"顺序管道"（pipeline）**：L1→L2→L3，数据单向流动，无回环
- **每一层被动接受上层输出**：没有"主动思考"能力，只是数据加工节点
- **全局状态缺失**：ShipMemory/PatternMemory/SessionMemory 是附带的补丁，不是架构内生的
- **反馈闭环薄弱**：feedback_loop 在管道末端，只做增量训练触发

### 你已经有的"智能体胚子"
- `memory/` 模块（SessionMemory、ShipMemory、PatternMemory）→ **长期记忆**
- `llm_layer/prompts.py` → **推理能力**
- `triage_engine/` → **工具调用（向量检索）**
- `orchestrator.py` → **主循环框架**
- Shell + EventDetect 模块 → **感知能力**

---

## 二、核心思路：从"管道"变"智能体循环"

### 架构类比

| 概念 | 当前（管道） | 目标（智能体） |
|------|-------------|---------------|
| 数据流 | L1→L2→L3 一次过 | 循环迭代，持续优化 |
| 控制权 | 代码硬编码顺序 | LLM/Agent 自主决策 |
| 记忆 | 三个独立的存/取 | 统一的记忆系统+反思 |
| 工具 | 固定在代码里 | Agent 自主选择工具 |
| 容错 | 直接降级跳过 | 重试/换策略/引入新信息 |

### 一句话思路

> **把管线中的每一步（检测、检索、判定、查询历史）封装成 Agent 可调用的"工具（Tool）"，然后让一个 LLM Agent 通过"思考→选工具→行动→观察→再思考"的循环来驱动整个流程。**

---

## 三、方案一：ReAct Agent 封装（推荐，增量演进）

### 架构图

```
┌──────────────────────────────────────────────────────┐
│                  Agent Core (LLM)                     │
│   Thought → Action → Observation → Thought → Final   │
└────────┬──────────┬──────────┬───────────────────────┘
         │          │          │
    ┌────▼───┐ ┌───▼────┐ ┌──▼──────┐
    │ Tools  │ │ Tools  │ │ Tools   │
    │ Set    │ │ Set    │ │ Set     │
    ├────────┤ ├────────┤ ├─────────┤
    │detect  │ │vector  │ │llm      │
    │quality │ │search  │ │judge    │
    │segment │ │similar │ │reason   │
    │merge   │ │case    │ │explain  │
    │feature │ │        │ │feedback │
    └────────┘ └────────┘ └─────────┘
         ▲          ▲           ▲
         └──────────┴───────────┘
                   │
          ┌────────▼────────┐
          │  Memory System  │
          │ (Unified Store) │
          └─────────────────┘
```

### 具体设计

#### 1. 定义 Tool Set（把各层能力包装成 Agent 工具）

```python
class AISAgentTools:
    """把三级系统的能力封装成 Agent 可调用的工具。"""

    # ── L1 感知工具 ──
    def detect_events(self, ais_data: str) -> list[Event]:
        """检测 AIS 数据中的候选事件"""
    def assess_quality(self, track: Track) -> QualityReport:
        """评估轨迹质量"""
    def extract_features(self, track: Track) -> FeatureVector:
        """提取特征向量"""

    # ── L2 检索工具 ──
    def vector_search(self, feature_vector, top_k=5) -> list[SimilarCase]:
        """在向量库中检索相似案例"""
    def get_ship_history(self, mmsi: str) -> ShipRecord:
        """查询船舶历史记录"""

    # ── L3 推理工具 ──
    def llm_judge(self, context: dict) -> Judgment:
        """（低级）快速判定——单一 prompt"""
    def llm_reason(self, context: dict) -> ReasoningResult:
        """（高级）深度推理——多步思维链"""
    def llm_explain(self, context: dict) -> Explanation:
        """解释判决依据，生成可读报告"""

    # ── 记忆工具 ──
    def recall_pattern(self, scenario: str) -> Pattern | None:
        """从情景记忆中召回已知模式"""
    def memorize(self, key: str, value: Any):
        """写入记忆"""
    def reflect(self) -> Reflection:
        """反思本轮经验，提炼模式"""
```

#### 2. Agent 主循环

```python
class UnifiedAgent:
    """三级合一的 AIS 分析智能体。"""

    def analyze(self, ais_data: str) -> AnalysisResult:
        """智能体主循环：Think → Act → Observe"""

        context = {
            "raw_data": ais_data,
            "mmsi": extract_mmsi(ais_data),
            "memory": self.memory.load_relevant(ais_data),
        }

        while not self.is_final(context):
            # Step 1: Think — LLM 决定下一步做什么
            action = self.llm.decide_next_action(context)

            if action.type == "final_answer":
                return self.build_result(context)

            # Step 2: Act — 执行选中的工具
            observation = self.execute_tool(action)

            # Step 3: Observe — 将结果写入上下文
            context["observations"].append({
                "action": action.name,
                "result": observation,
                "timestamp": now(),
            })

            # 可选：检查是否需要回溯（发现信息不足）
            if self.should_backtrack(context):
                context["backtrack"] = True  # 触发 LLM 重新思考方向

        return self.build_result(context)
```

#### 3. 五种内置策略（Agent 根据情况自动切换）

| 策略 | 条件 | 行动序列 |
|------|------|----------|
| **快速路径** | 置信度>0.95 && Pattern命中 | quality → detect → llm_judge → final |
| **标准路径** | 正常情况 | quality → detect → vector_search → llm_reason → feedback → final |
| **深度路径** | 边界案例/高风险 | quality(细) → detect → search → llm_reason(多轮) → llm_explain → 人工标注建议 → final |
| **重审路径** | LLM与ML路由不一致 | quality(复查) → detect(放宽) → search(扩范围) → llm_reason(对抗) → 比较判决 → final |
| **学习路径** | 遇到新模式/未知案例 | detect(记录) → search(确认新鲜) → llm_reason(详细) → memorize → reflect → final |

Agent 的 LLM 会**自主决定**走哪条路——不需要代码硬编码。

---

## 四、方案二：Multi-Agent 协作（更彻底的解耦）

每条管线环节独立为一个 Agent，它们通过一个"协调员 Agent"交流：

```
                    ┌─────────────────┐
                    │   Coordinator   │  ← LLM 做任务分解和结果综合
                    │   (Meta-Agent)  │
                    └────┬──────┬─────┘
                         │      │
          ┌──────────────┤      ├──────────────┐
          │              │      │              │
    ┌─────▼────┐  ┌─────▼──┐ ┌▼──────┐  ┌────▼─────┐
    │ Quality  │  │ Event  │ │Vector │  │   LLM    │
    │ Analyst  │  │ Hunter │ │Librn  │  │  Judge   │
    └──────────┘  └────────┘ └───────┘  └──────────┘
         │             │         │            │
         └─────────────┴─────────┴────────────┘
                              │
                      ┌───────▼───────┐
                      │  Memory Bank  │
                      │ (Shared Store)│
                      └───────────────┘
```

**优点**：每个 Agent 职责单一，可独立演进
**缺点**：需要 Multi-Agent 编排基础设施，复杂度高

---

## 五、推荐实施路径

### 第一阶段（1-2周）：Tool 封装 + ReAct 循环

1. **创建 `agentic/` 目录**，把现有能力包成 Tool
2. **实现 `UnifiedAgent` 主循环**（上面方案一）
3. **LLM 驱动策略选择**（替代 run_pipeline_only/run_with_ml/run_full 的硬编码）
4. **保留现有入口**（run_* 方法可继续用，不做破坏性重构）

### 第二阶段（3-4周）：记忆系统增强

1. **统一 Memory Store**（把 SessionMemory/ShipMemory/PatternMemory 合并成一个带命名空间的 MemorySystem）
2. **加入反思机制**：每处理完一批数据，Agent 自动调用 `reflect()` 提取新模式
3. **模式复用强化**：PatternMemory 命中时，Agent 可以选择"跳过执行，直接返回结果"

### 第三阶段（可选）：从 ReAct Agent 演进到 Multi-Agent

1. 把第一阶段的 Tool Set 升级为独立 Agent
2. 引入 Coordinator Agent 做任务分解和结果综合
3. Agent 之间通过 Message Bus 通信

---

## 六、关键设计决策

| 决策 | 推荐 | 理由 |
|------|------|------|
| Agent 驱动层 | **ReAct (Think-Act-Observe)** | 代码改动最小，保留现有模块完整性 |
| LLM 选型 | **用你已有的模型**（qwen2.5） | 不引入新依赖，qwen2.5 的 tool-use 能力足够 |
| Tool Schema | **JSON Schema + docstring** | 与 LangChain/原生 LLM 都兼容 |
| 上下文窗口 | **截断策略沿用现有逻辑** | Track 点超过 200 时采样，参考已有 `_render_prompt` |
| 状态管理 | **Session-Memory Driven** | 不引入外部状态机，让 LLM 自己管理中间状态 |
| 降级策略 | **逐步降级** | LLM 不可用→Mock模式；Tool 异常→重试2次→跳过→记录 |

---

## 七、需要你确认的问题

1. **目标深度**：你希望 Agent 做到什么程度？
   - A. 替代当前 run_full()，但让 Agent 自主选择策略（推荐，增量改进）
   - B. 完全重写为 Multi-Agent 系统（更灵活但重构工作量大）
   - C. 仅在当前架构上加一层"Agent 决策层"，做路由选择

2. **与现有系统的关系**：新 Agent 系统是替代 orchestrator.py，还是作为其内部的一个新模块？

3. **运行时模式**：Agent 的"思考"过程是否需要可见（输出到终端/日志），还是黑盒只给结果？

4. **LLM 选型**：你目前用的是 qwen2.5 local + ollama，还是也可以用云端模型（比如通过 API）？