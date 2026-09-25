# 📚 智海巡航 — 文档中心

> ⚓ **港口船舶违规行为智能识别平台** · 三级级联架构 + Web 管理系统

项目总览、特性与快速开始请见 **[根目录 README](../README.md)**。本页是各专项文档的导航索引。

---

## 📖 文档索引

| 文档 | 内容 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | **快速开始** — 环境依赖、Ollama / Milvus / pgvector 启动、安装、运行、FAQ |
| [API.md](API.md) | **REST API 参考** — `/api/v1` 全部接口、请求 / 响应示例、状态枚举 |
| [DESIGN.md](DESIGN.md) | **架构设计** — 三层级联架构、标记体系、路由决策、反馈闭环、消融实验方案 |
| [数据库设计方案.md](数据库设计方案.md) | **数据库设计** — PostgreSQL + pgvector E-R 图、表结构、索引与配置 |
| [智能体记忆系统设计.md](智能体记忆系统设计.md) | **记忆系统** — 会话 / 船舶 / 情景三类记忆的设计与实现 |
| [实验方案.md](实验方案.md) | **实验方案** — 对比实验（速度 / 容量 / 容错）与消融实验设计 |
| [../论文/方案A_三级级联论文方案.md](../论文/方案A_三级级联论文方案.md) | 📄 **论文研究方案** — 方向 A：成本-质量帕累托 + 可追溯证据链 |

---

## 🧭 快速导览

### 目录速览

```
├── ais_accelerator/   ⚡ L1 · AIS 数据加速器（清洗/分段/事件检测/规则/特征）
├── triage_engine/     🧠 L2 · ML Triage 路由引擎（LightGBM 四分类 + 向量检索）
├── llm_layer/         🤖 L3 · LLM 判定层（Qwen2.5 + 四级 Prompt + 反馈闭环）
├── memory/            🧠 智能体记忆系统
├── shipclaw/          ⚓ 可养成海事智能体「小慧」
├── frontend/          🌐 Vue 3 前端 + Flask 后端
├── database/          🗄️ PostgreSQL + pgvector 建表 SQL
└── experiments/       🔬 对比实验与消融
```

### 快速开始

```bash
# Web 管理系统
python start.py                    # 访问 http://localhost:5000

# CLI 全链路分析
python run.py -i data/input/xxx.csv -n 10

# 跳过 LLM（仅加速器 + ML 路由）
python run.py --no-llm
```

完整步骤见 [QUICKSTART.md](QUICKSTART.md)。

### 三级架构一句话

```
原始 AIS → [L1 加速器·打标记] → [L2 ML·四分类路由] → [L3 LLM·最终判定] → 反馈闭环 → ML 增量学习
```

---

## 🗂️ 文档维护

修改功能或接口时，请同步更新对应文档（见 [CONTRIBUTING.md](../CONTRIBUTING.md#文档规范)）。
