# 🐳 Docker 一键部署 — 智海巡航

> 面向其它开发者：**一条命令**跑起「数据库 + 大模型 + Web 管理端」全套系统，无需手动装 Ollama / 起 Milvus / pip install / npm build。

前置要求：**Docker Desktop（或 Docker Engine + compose 插件）**。不需要本地装 Python / Node / Ollama。

---

## 1. 快速开始（轻量默认）

```bash
# 1) 准备环境变量（首次）
cp .env.example .env
#    建议改 SECRET_KEY 与 ADMIN_PASSWORD

# 2) 构建并启动（完整栈：postgres + 向量库[etcd/minio/milvus] + ollama + app）
docker compose up -d --build

# 3) 打开系统
#    http://localhost:5000   （默认账号 admin / admin123，可在 .env 里改）
```

首次启动时，**没有本地模型的用户**会自动拉取模型（qwen2.5:7b-instruct-q4_K_M ≈4.7GB + nomic-embed-text ≈274MB），
`docker compose logs -f ollama` 可看进度；模型进命名卷缓存，重启秒过。**拉取是尽力而为**，失败不阻塞任何服务（app 自动降级为「加速器 + ML」判定）。

**自带模型（推荐给已有模型 / 网络受限的部署方）**——模型不进容器就绪，拉取即可跳过：

```bash
# 方式一：容器内拉取（需能连 ollama.com；成功后入卷，一次即可）
docker compose exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M

# 方式二：直接把本机 Ollama 模型目录整卷拷进容器（本机模型在 ~/.ollama）
docker compose cp ~/.ollama ollama:/root/
```

只要 `.env` 里的 `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` 与卷里实际存在的模型名一致，健康检查即通过、LLM 全功能可用。

> ⚠️ **端口冲突**：若本机 5432 / 11434 / 5000 已被占用，改 `.env` 里的 `PG_HOST_PORT` / `OLLAMA_HOST_PORT` / `APP_PORT` 再 `up`。

---

## 2. 向量库（默认随完整栈启动）

Milvus（etcd + minio + milvus）已并入 `docker-compose.yml`，`docker compose up -d` 即包含，**什么都不能少**。法规语义检索 / 轨迹相似检索默认可用，无需额外命令。

- `MILVUS_ENABLE=on`（`.env` 默认 `on`）时 app 使用向量库；想跳过向量库可设 `0`（法规检索降级为哈希向量，检测管道不受影响）。
- Milvus 首次启动约 60~90s（`docker compose ps` 见 milvus healthy 后再用）。
- 若本机 19530 已被占用（如本机已有 Milvus），改 `.env` 的 `MILVUS_PORT` 再 `up`。

---

## 3. 验证清单

```bash
docker compose ps                      # postgres healthy / app running / ollama running
curl -fsS http://localhost:5000/       # 返回 index.html

# 登录默认管理员（返回 {"code":200,...}）
curl -X POST http://localhost:5000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'

# 数据库表已自动建好（detection_results / users / cascade_logs ...）
docker compose exec postgres psql -U postgres -d postgres -c '\dt'

# 种子数据已入卷（陆地掩码）
docker compose exec app ls data/geo/

# 上传 CSV → 触发全链路检测 → 写库（页面上传即可，或：）
#   模型未就绪时会返回 503「LLM 模型未启动」，等 ollama 拉完模型再试
# curl -b cookies.txt -F 'file=@sample.csv' http://localhost:5000/api/v1/data/upload
docker compose exec postgres psql -U postgres -d postgres -c "SELECT COUNT(*) FROM detection_results;"

# 等 ollama 拉完模型后，AI 对话触发法规向量化
#   页面 AI 对话试一句，或在容器里：
docker compose exec app python -c \
  "from ais_accelerator.regulation_db import get_embedding; print(len(get_embedding('test')))"
```

---

## 4. 数据与重置

| 操作 | 效果 |
|------|------|
| `docker compose down` | 停服务，**数据保留**（命名卷） |
| `docker compose up -d` | 恢复，数据仍在 |
| `docker compose down -v` | 全清（含数据库 / 模型 / 运行数据），下次 up 自动重新播种默认管理员 + 陆地掩码 |

数据卷：
- `zhihaixunhang_app_data` → `/app/data`（输入输出 / 缓存 / 日志 / 记忆 / 头像 / LLM 配置）
- `zhihaixunhang_pg_data` → PostgreSQL 数据
- `zhihaixunhang_ollama_models` → `/root/.ollama`（模型）
- `milvus_data` / `milvus_etcd` / `milvus_minio` → Milvus 向量库数据（默认随完整栈启动）

开发想直接往 `data/input/` 塞 CSV？用 `docker compose cp ./xxx.csv app:/app/data/input/`，然后页面上传触发扫描即可（上传入口在系统内）。

---

## 5. 环境变量速查

见 `.env.example`。关键项：

| 变量 | 默认 | 说明 |
|------|------|------|
| `PG_PASSWORD` | `wmlwxhn520` | postgres 密码（postgres 容器与 app 必须一致） |
| `SECRET_KEY` | 占位 | **生产务必改** |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | 全新库首次启动自动播种的超级管理员 |
| `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` | qwen2.5:7b-instruct-q4_K_M / nomic-embed-text | 换模型改这里 |
| `MILVUS_ENABLE` | `on` | 标准部署默认开启向量库；`0` = 跳过 Milvus（法规检索降级） |
| `APP_PORT` / `PG_HOST_PORT` / `OLLAMA_HOST_PORT` / `MILVUS_PORT` | 5000/5432/11434/19530 | 宿主机端口（冲突时改） |

---

## 6. 故障排查

**app 起不来 / 反复重启** → 先看日志 `docker compose logs app`。最常见是 `_init_users_table` 连不上 PG：
确认 postgres healthy（`docker compose ps`），且 `.env` 的 `PG_PASSWORD` 与 postgres 容器一致。

**上传返回 503「LLM 模型未启动，级联检测暂停」** → ollama 模型还没就绪。等 `docker compose logs -f ollama` 看到两个模型 `success` 后重试；系统在拉取期间会降级运行（加速器 + ML 结果保留，上传接口明确提示模型未就绪）。

**Milvus 连不上** → 确认 `.env` 里 `MILVUS_ENABLE=on` 且 `docker compose ps` 里 milvus healthy（etcd/minio/milvus 三容器都要 healthy）。首次启动需 60~90s。

**改了 .env 不生效** → 改完需 `docker compose up -d` 重建受影响容器（或 `--force-recreate`）。
