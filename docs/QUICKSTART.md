# 🚀 快速开始 — 智海巡航安装与运行指南

本文档覆盖从零搭建 **智海巡航 — 港口船舶违规行为智能识别平台** 的完整步骤：基础设施（Ollama / Milvus / PostgreSQL+pgvector）→ 依赖安装 → 数据初始化 → 两种运行模式 → 环境变量与常见问题。

---

## 1. 环境要求

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | 3.10+ | 三级级联引擎 / Flask 后端 |
| Ollama | 任意支持 `qwen2.5` 的版本 | 本地 LLM 推理服务 |
| Docker Desktop | 任意 | 运行 Milvus 与 pgvector 容器 |
| PostgreSQL | 14+（含 `vector` 扩展） | 案例库 / 结构化数据 |
| Node.js | 18+ | 构建 Vue 3 前端 |

> 💡 **最小可运行方案**：仅安装 Python 即可运行 CLI（`--no-llm`）和 Web 部分功能；Ollama / Milvus / pgvector 缺失时系统会**优雅降级**并告警，不会中断。

---

## 2. 启动基础设施

### 2.1 Ollama（LLM 服务）

```bash
# 安装并启动 Ollama（Windows 见 ollama.com 安装包）
ollama pull qwen2.5:7b-instruct-q4_K_M   # 拉取推荐模型
ollama serve                              # 默认监听 http://localhost:11434
```

> ⚠️ **Windows 常见坑**：Ollama 启动早期（几秒内）HTTP 端点会返回 500 / 拒连，属于**已启动但尚未就绪**的正常现象。系统内置了**轮询探测 + 自动重试**（最多 ~25s），因此请勿在启动后立即运行全链路分析——等待系统自愈即可。详见 [FAQ](#6-常见问题-faq)。

### 2.2 Milvus（轨迹序列向量库）

本项目**只支持连接外部 Milvus 服务（Docker），不支持 Lite 模式**。若没有可用的 Milvus，系统会打印告警并继续（跳过轨迹相似检索）。

```bash
# 启动 docker compose 中的 Milvus 服务（以项目配套的 F:\milvus 为例）
cd F:\milvus
docker compose up -d
# 默认地址 http://localhost:19530
```

### 2.3 PostgreSQL + pgvector（结构化案例库）

```bash
docker run -d --name ais-pg \
  -e POSTGRES_PASSWORD=<你的密码> \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

> 连接参数统一在 `config.py` 管理，可用环境变量覆盖（见 [第 5 节](#5-环境变量)）。

---

## 3. 安装依赖

### 3.1 Python 依赖

```bash
pip install lightgbm numpy pymilvus langchain-ollama pydantic psycopg2-binary
# 若运行 Web 后端还需：
pip install -r frontend/frontend/backend/requirements.txt   # flask, flask-cors
```

### 3.2 前端构建（Web 模式）

```bash
cd frontend/frontend
npm install
npm run build        # 产物输出到 dist/，由 Flask 直接托管
```

> `start.py` 启动时若 `dist/` 已存在，直接访问 http://localhost:5000 即可；修改前端源码后需重新 build。

---

## 4. 初始化数据（可选）

| 脚本 | 说明 |
|------|------|
| `scripts/init_data.py` | 初始化示例船舶 / 区域等基础数据 |
| `scripts/seed_regulations_milvus.py` | 将法规条款向量化写入 Milvus（供 `RegulationDB` 引用） |
| `scripts/reembed_regulations.py` | 法规重新向量化（换 embedding 模型后使用） |
| `scripts/import_csv_to_sqlite.py` | 将历史 AIS CSV 批量导入 SQLite |
| `scripts/migrate_to_pg.py` / `migrate_sqlite_to_pg.py` | 数据迁移到 PostgreSQL |
| `scripts/backfill_violations.py` | 回填违规记录 |
| `scripts/upgrade_roles_orgs.py` | 用户角色 / 组织升级脚本 |

```bash
python scripts/seed_regulations_milvus.py   # 若使用法规引用功能
```

---

## 5. 运行

### 5.1 Web 管理系统（推荐）

```bash
python start.py                 # 默认端口 5000
python start.py --port 8080     # 指定端口
python start.py --prod          # 生产模式（关闭调试）
```

访问 **http://localhost:5000**。支持：仪表盘、船舶监控、违规管理（文书上报页当前仅展示违规列表，提交处置后端未接入）、级联日志、AI 对话（小慧）。

**Web 数据流**：页面上传 CSV → `CascadeAgent.scan_csv()` → 完整运行 `run.py` 管道 → 结果写入 `detection_results` 表，级联日志实时写入 `cascade_logs` 表（前端级联日志窗口可查看）。

### 5.2 CLI 全链路分析

```bash
# 处理 data/input/ 下所有 CSV
python run.py

# 指定输入 / 输出
python run.py --input data/input --output data/output

# 处理单个文件
python run.py -i data/input/210724铜锣峡210731.csv

# 只处理前 10 艘船
python run.py -n 10

# 跳过 LLM（仅加速器 + ML 路由，速度快）
python run.py --no-llm

# 强制从已累积反馈增量训练模型
python run.py --retrain

# 并行分析（默认 4 线程）
python run.py -w 4
```

结果输出为 `data/output/results_<源文件名>.csv`。

---

## 6. 环境变量

所有连接配置均可通过环境变量覆盖（无需改代码）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PG_HOST` | `localhost` | PostgreSQL 主机 |
| `PG_PORT` | `5432` | PostgreSQL 端口 |
| `PG_DB` | `postgres` | 数据库名 |
| `PG_USER` | `postgres` | 用户名 |
| `PG_PASSWORD` | `wmlwxhn520` | 密码（默认来自 `config.py` 的 `pg_config()`） |
| `MILVUS_URI` | `http://localhost:19530` | Milvus 地址 |
| `MILVUS_ENABLE` | `on` | `0`/`off`/`false` 显式关闭 Milvus |

> ⚠️ 所有默认值统一来自 `config.py`（`pg_config()` / `milvus_uri()`），改一处即可。本机 Docker 部署 PG 宿主端口用 `55500`（`55432` 落在 Hyper-V 排除端口段，绑定会报错）。

```bash
# Windows PowerShell
$env:PG_HOST="192.168.1.10"
$env:PG_PASSWORD="你的密码"
python start.py
```

---

## 7. 常见问题 FAQ

### Q1：Ollama 已启动，却报「LLM 不可用」？
**根因**：Windows 上 Ollama 秒级启动竞态——服务进程已起来，但 HTTP 端点仍返回 500 / 拒连。
**修复**：系统已内置**连通性探测 + 轮询重试**（`run.py` 最多等待 ~25s，`llm_layer/handler.py` 调用时还会再次自愈重连）。请稍等几秒再运行；若仍失败，执行 `curl http://localhost:11434/api/tags` 确认服务就绪。

### Q2：Milvus 初始化失败？
项目**不支持 Lite 模式**，只连 Docker Milvus。请确认：
```bash
docker ps            # 检查 milvus 容器是否运行
# 默认地址 http://localhost:19530
```
连不上只会打印 `⚠` 告警并跳过轨迹检索，不影响其余流程。

### Q3：PostgreSQL / pgvector 未连接？
连接失败不影响主流程，但案例写入 / 相似检索不可用。检查：容器是否启动、端口是否暴露、`PG_*` 环境变量是否正确、`vector` 扩展是否安装（`CREATE EXTENSION vector;`）。

### Q4：首次运行自动训练模型？
`run.py` 检测到 `triage_model.txt` 不存在时会用 500 个样本自动训练初始模型，属于正常现象。

### Q5：前端空白 / 样式丢失？
确认已执行 `npm run build`，且 Flask 托管的是最新 `dist/` 产物。

---

## 8. 相关文档

- [README.md](../README.md) — 项目总览与架构
- [API.md](API.md) — REST API 参考
- [数据库设计方案.md](数据库设计方案.md) — 数据库 E-R 设计
- [智能体记忆系统设计.md](智能体记忆系统设计.md) — 记忆系统
