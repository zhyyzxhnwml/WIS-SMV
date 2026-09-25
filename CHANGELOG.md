# 📝 更新日志

> 本项目尚未接入语义化版本（SemVer）与自动发布流程。以下为基于代码库演化记录整理的**主要里程碑**，日期来自对应改动时间点。

---

## 2026-08-22 — 证据链补强：AIS 中断类事件补中断上下文

- **文本证据链**：`ViolationEvidenceSkill._build_gap_context` 对 AIS 中断类事件（`features.gap_minutes` 存在）补全证据——证据点通常只存"中断恢复后首点"无法说明违规，现用该点时间戳在原始轨迹中定位，取前一点作为**中断前最后信号点**，配合中断时长构成完整链条：`AIS 信号中断 481 分钟（约 8.0 小时）` + `中断前：13:29 最后正常信号 …` + `恢复后：21:30 …`，直接回答"为什么这个点就违规"。
- **结构化数据**：`_build_evidence_data` 事件对象新增 `gap_minutes` / `pre_point` / `post_point`，供前端绘图。
- **前端 `EvidenceCard.vue`**：① 轨迹图新增图例说明行（轨迹全程首→尾 / 起点 / 终点 / 违规事件点 / 中断前最后信号 / 信号中断期），明确"起/终"是全程轨迹首尾而非违规点；② 黄色 pin「前」标记中断前最后信号点、红色虚线 + 箭头绘制"信号中断期（无回传）"；③ 坐标轴防重影：x/y 轴**各自独立**的小数位（按本轴跨度 2/3/4 位自适应）+ `hideOverlap: true` + 刻度降到 3 个 + x 轴标签旋转 20°。

## 2026-08-22 — 证据链展示修复（单证据点"区间 A→A" + 轨迹图重影）

- **文本证据链**：`ViolationEvidenceSkill._build_evidence` 单证据点事件（首尾同点）由误导性的「区间 A→A」改为「位置：…」，`_CODE_MAP` 归一化后仅 1 个证据点的事件同样适用（`_fmt_latlon` 有值才显示，否则隐藏该行）。
- **轨迹图重影**：`EvidenceCard.vue` 轨迹散点抽稀到最多 60 点（`MAX_DOTS=60`，相邻近重合点不再叠成一片）；坐标轴刻度小数位按数据范围自适应（跨度 >0.1° 用 3 位、否则 4 位，长标签不再互相遮挡）；x/y 轴 `splitNumber` 5/4 减少刻度重叠；起终点改 pin 标记（"起"/"终"），tooltip 支持 `_mk` 标记。

## 2026-08-22 — 违规证据链结构化展示（判定卡片 + 轨迹图 + 事件表格）

- **后端 `ViolationEvidenceSkill` 结构化输出**：`execute()` 找到记录时返回 `{"text","data"}`——`text` 保留四段纯文本证据链（兜底），`data` 为结构化 JSON（判定 / 轨迹点 / 事件 / 法规 / 复核）。新增 `_build_evidence_data()` 组装，轨迹点采样 ≤300；`quality_level` 用表列（`quality_report.level` 是 @property 取不到）。
- **`core.py` 拆包透传**：新增 `_split_result()`，`chat()` 统一执行技能后拆出 `structured` 字段；确定性技能返回 dict 不破坏 `tool_calls.summary` 与等级锁定分支。
- **`/api/v1/ai/chat` 透传 `structured`**：前端直接拿到结构化数据。
- **前端新增 `EvidenceCard.vue`**：白底自包含卡片，含 ① 判定卡片（违规标签/风险/置信度/AIS质量/L1打标）② ECharts 轨迹图（蓝线轨迹 + 红涟漪事件点，hover 显示时间/航速/航向）③ 违规事件表格 ④ 法规引用/LLM复核/建议处置；`FloatingAIChat.vue` 与 `AIChat.vue` 均 `v-if="m.structured"` 条件渲染，无结构化数据回退纯文本；两处 quickQuestions 加"413570530 的违规证据链"示例。

## 2026-08-22 — 论文研究方案 + 全量文档代码对齐

- **新增 `论文/方案A_三级级联论文方案.md`**：论文方向 A「面向海事监管的 LLM 三级异构级联：成本-质量帕累托与可追溯证据链」研究方案（GT 构建 / 6 基线 / 5 核心实验 / 统计方法 / 16 周实施计划）
- **全量文档按代码现状对齐**：
  - `docs/API.md` 重写为 v2.1：实际约 62 个端点分组（新增 dev / claw / user / admin / orgs / data）；AI 对话标注为已接入真实 ShipClaw（不再"测试中"）；标明文书上报（disposal）无后端端点、仪表盘空数据时返回硬编码演示值
  - `docs/DESIGN.md` 标记 Phase 1–6 完成状态、三分类→四分类、特征 22→28 维、相关文件索引去"待创建"
  - `docs/数据库设计方案.md` 修正 PG 默认值（`postgres` / `wmlwxhn520`，统一到 config.py）、特征向量维度说明、运行时 DDL 表补充、disposal 无写入说明
  - `docs/实验方案.md` 顶部加现状对齐：实际命令为 `experiments/run.py` 与 `experiments/feedback_experiment/`，附录对齐代码结构
  - `docs/智能体记忆系统设计.md` 标注三类记忆已实现、PatternMemory 不再用于检测管道、Milvus Lite 表述修正、特征 22→28 维
  - `README.md` 特征 22→28 维、API 分组与端点数更新、实验章节对齐、新增论文文档导航
- **新增 `shipclaw` 内置技能 `violation_evidence`（违规证据链，Lv4）**：按 MMSI 查 `detection_results`，从 `detailed_result` / `violation_event` / `regulation_ref` / `final_assessment` 组装 ① 轨迹分析 ② 规则匹配 ③ 法规引用 ④ LLM 复核 四段证据链，回答"这艘船怎么违规、对应法规第几条"；内置 Python 代码技能（查库组装），非纯 LLM 的 skill.md；配套操作手册 `shipclaw/skills/violation_evidence.md`；匹配优先级已避让 ShipQuery（0.85）/ RegulationQA（0.85）/ ViolationList（0.92）——内置技能 8→9
- **三处修复（用户实测"413570530的违规证据链给我"只回法规科普、无证据链）**：
  - **确定性技能机制**：`core.py._execute_skill` 对 `deterministic=True` 的技能直接返回查询结果、跳过 Ollama 人格渲染——避免 LLM 把"查无记录/证据链"这类确定性结果加戏编造成法规科普（`violation_evidence` 即确定性技能）
  - **系统数据对登录用户可见**：`pg_db.user_scope` 与 `app.py._user_scope` 统一为 `(user_id = ? OR user_id = 'system')`——CLI（`run.py` 以 `user_id='system'` 写入）扫描出的船此前对 Web 登录用户不可见，导致查无记录
  - **MMSI 正则修复**：`\b(\d{6,9})\b` → `(?<!\d)(\d{6,9})(?!\d)`——Python3 的 `\w` 含中文，数字紧贴中文（如"413570530的违规"）时 `\b` 边界失效提取不到 MMSI（`ship_query` 同修）

## 2026-08-21 — 开发者自定义技能（提交 skill.md 智能体直接可用）

- **开发者页新增「自定义技能」页签**：用户按模板编写 skill.md（YAML 元信息 + 操作手册正文），在线提交后智能体「小慧」在对话中命中触发词即自动使用该技能，按用户手册操作
- 新增 `custom_skills` 表（按登录用户隔离）；`/api/v1/dev/skills` CRUD + `template` + `validate` 端点（校验：name 小写英文不冲突内置、label/description 必填、min_level 1~10、触发词非空、正文非空）
- `shipclaw/skills.py` 新增 `CustomSkill`（LLM 驱动、安全不执行代码）、`validate_custom_skill_md()`、`load_user_custom_skills()`；`match_best_skill()` 支持额外技能，**平局时自定义技能优先**
- `ShipClaw.chat()` 加载用户自定义技能参与路由，按自定义 min_level 判定解锁；自定义技能手册注入 Ollama 提示；`/api/v1/ai/tools` 合并展示自定义技能
- 前端 `DevSkills.vue`：技能列表（生效/停用徽标、触发词、解锁等级）+ 编辑器（模板/校验/新建/编辑/删除/启停）

## 2026-08-21 — 智能体技能操作手册（skill.md）

- 新增 `shipclaw/skills/*.md`：为全部 8 个技能各写一份操作手册（触发条件 / 操作步骤 / 输出规范 / 禁止事项）
- `shipclaw/skills.py` 新增 `load_skill_manual()`（剥掉 YAML frontmatter）；`ShipClaw` 执行技能时把对应手册注入 Ollama 系统提示，LLM **严格按手册操作**，解决答非所问（例如闲聊不再编造船舶数据、违规记录用中文标签、法规不捏造条款号）
- 修复路由平局：`trend_analysis` 关键词分 0.85→0.9，避免「违规趋势分析」同时命中法规「违规」时误路由到法规问答

## 2026-08-21 — Milvus 连接自愈加固 + 时区修正 + 智能体数据工具

- **Milvus 容器重启后连接静默失效**：`Cannot invoke RPC on closed channel!`（elapsed 0.3ms）——容器重启导致一次性 gRPC channel 失效，法规/轨迹检索静默返回空
  - `ais_accelerator/regulation_db.py`：新增 `_call()` 自愈包装，命中连接错误标记时重建连接并重试一次（幂等读操作全部走它）
  - `triage_engine/milvus_client.py`：补齐 ORM `default` 连接（此前轨迹检索一直 `ConnectionNotExistException`、集合为空），新增 `_coll()` 自愈包装；`_connect_server` 的 host/port fallback 块保持原位
- **容器时区修正**：app 容器默认 UTC，`cascade_logs` / `detection_results` 时间比本地晚 8 小时——compose `app` 服务加 `TZ=Asia/Shanghai`；`cascade_agent` 日志时间戳加毫秒精度（同秒多行不再混淆）
- **智能体数据查询工具**：`shipclaw/skills.py` 新增 3 个技能——`ship_list`（我的船舶列表，Lv2）、`user_overview`（我的数据概况，Lv3）、`violation_records`（我的违规记录，Lv4），全部按登录用户 `user_scope` 隔离查询 `detection_results`；`ship_query` 匹配词增加「查询/查一下」以命中「查询闽海之星」这类船名问题
  - 技能补齐 `label/description/icon/params` 元数据，`get_tool_catalog()` 暴露 8 个工具
  - `ShipClaw.chat()` 返回 `tool_calls`（工具名/图标/说明/状态），等级锁定也展示「想调用的工具 + 🔒 未解锁」
  - 后端新增 `GET /api/v1/ai/tools`；前端 AI 对话（悬浮窗 + 全页）渲染工具调用卡片、新增「🔧 工具」Tab
- **本地部署踩坑**：`docker-compose.local.yml` 改用 `ports: !reset []` 清掉 ollama 端口映射（否则与宿主原生 Ollama 抢 11434）；pg 镜像切回 pg16（pg_data 卷为 pg16 初始化，pg17 版本不兼容崩循环）

## 2026-08-13 — Web 管理端迭代

- `frontend/frontend/backend/app.py` 接口重构扩容（当时约 40 个 `/api/v1` 接口；至 2026-08-22 已增至约 62 个端点）
- 前端新增/迭代页面：仪表盘、船舶监控、违规管理、文书上报、数据上传、级联日志、AI 对话
- 根目录新增 `README.md` 与文档导航（本组文档）

## 2026-08-12 — ShipClaw 可养成智能体「小慧」

- 新增 `shipclaw/` 模块：ShipClaw 主类 + 四维性格系统 + 技能系统 + 养成记忆
- 等级体系 Lv1~10，交互产生经验与性格成长
- 智能体记忆重构（`claw_memory.py`）

## 2026-08-06 — 三级级联全链路打通

- `run.py` 重构为**全系统唯一数据处理管道**，Web / CLI / CascadeAgent 统一入口
- `cascade_agent.py`：直接调用 `run.py` 的 `main()`，终端输出实时写入 `cascade_logs` 表
- 修复 **Ollama 可用性竞态**：Windows 上 Ollama 秒级启动导致「已启动却报不可用」，引入 HTTP 连通性探测 + 轮询重试（详见 [docs/QUICKSTART.md](docs/QUICKSTART.md#q1)）
- `config.py` 统一配置中心（Milvus / PostgreSQL 环境变量覆盖）
- `triage_engine/` 全面完善：router / model / train / milvus_client / pgvector_client / schema.sql
- `llm_layer/` 完善四级 Prompt 模板与反馈闭环

## 2026-08-04 — 法规向量数据库与统一入口

- 新增 `ais_accelerator/regulation_db.py`：法规条款向量化，LLM 判定注入具体条款，输出 `regulation_ref`
- 新增 `cascade_agent.py`（CascadeAgent 雏形）
- `regulations.json` 迁移：违规类型定义迁入 RegulationDB，本文件仅保留区域与船型

## 2026-08-01 — PostgreSQL + pgvector 集成

- 新增 `database/schema.sql`：完整建表（ships / raw_ais / cases / tracks / training_set / ml_feedback / violations / disposal_docs / regulations）
- `triage_engine/pgvector_client.py`：案例写入、相似检索、ML/LLM 结果回填
- 智能体记忆系统上线：`memory/`（session / ship / pattern）

## 2026-07-20 — ais_accelerator 包建立

- `ais_accelerator/` 包初始化（facade / pipeline / quality / segment / detect / merge / compress / rules / models）
- `triage_engine/` 与 `llm_layer/` 骨架建立

## 2026-07-16 — 架构设计定稿

- `docs/DESIGN.md`：三层级联架构设计（AIS 加速器 → ML Triage → LLM 判定）+ 消融实验方案
- 确立「召回优先 + 可追溯 + 优雅降级」设计原则

---

## 🎯 规划方向（Roadmap）

- [ ] 语义化版本管理与自动发布
- [ ] ML vs LLM 一致率监控仪表盘
- [ ] 人工复核工作流
- [ ] 多 LLM 模型对比评估
