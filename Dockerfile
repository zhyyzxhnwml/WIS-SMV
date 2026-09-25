# syntax=docker/dockerfile:1
# ═══════════════════════════════════════════════════════════════
# 智海巡航 — 多阶段构建镜像
#   Stage 1: node:20-slim 构建 Vue 前端 → dist/
#   Stage 2: python:3.13-slim 运行时（Flask 托管 dist）
# 入口：docker/docker-entrypoint.sh（种子数据拷贝 + python start.py --prod）
# ═══════════════════════════════════════════════════════════════

# ── Stage 1: 构建 Vue 前端 ──
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/frontend/package.json frontend/frontend/package-lock.json ./
RUN npm ci
COPY frontend/frontend/ ./
RUN npm run build

# ── Stage 2: Python 运行时 ──
FROM python:3.13-slim AS runtime

# Debian 源：默认走 HTTPS（部分受限网络只放行 443，http:// 会被过滤）；
# 中国大陆加速可用 `docker compose build --build-arg APT_MIRROR=mirrors.aliyun.com`。
ARG APT_MIRROR=deb.debian.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mplconfig \
    LANG=C.UTF-8

# lightgbm 需要 libgomp1；curl 供健康检查；ca-certificates 供 pip/HTTPS。
# 其余依赖均为 manylinux wheel 自包含（psycopg2-binary 自带 libpq、shapely 自带 GEOS、PyMuPDF 自带 MuPDF）。
# sed：apt 源统一切到 HTTPS（并用 APT_MIRROR 覆盖主机名），规避 80 端口过滤。
# 兼容 deb822（debian.sources）与传统 sources.list 两种格式；文件不存在时 sed 报错，故先 `-f 存在才 sed`。
RUN [ -f /etc/apt/sources.list.d/debian.sources ] \
      && sed -i "s|http://deb.debian.org|https://${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    [ -f /etc/apt/sources.list ] \
      && sed -i "s|http://deb.debian.org|https://${APT_MIRROR}|g" /etc/apt/sources.list; \
    apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 代码树（.dockerignore 排除重/陈旧的 data/ 子目录与 dev 目录）。
# 根目录的 triage_model.txt / triage_model.txt.meta.json / regulations.json 随此拷贝。
COPY . .

# 消除 Windows 检出带来的 CRLF（entrypoint 必须是 LF；用 sh 调用，不依赖 shebang）
RUN sed -i 's/\r$//' docker/docker-entrypoint.sh 2>/dev/null || true

# 已构建的前端产物（覆盖任何陈旧/被忽略的 dist）
COPY --from=frontend /build/dist frontend/frontend/dist/

# 种子数据放在 /app/seed/data（卷挂载点 /app/data 之外），
# 入口脚本在首次启动时 no-clobber 拷入空卷（卷会遮蔽镜像内的 /app/data）。
COPY data/geo /app/seed/data/geo
COPY data/llm_config.json /app/seed/data/llm_config.json
COPY data/feedback_training_data.json /app/seed/data/feedback_training_data.json
COPY data/.last_retrain /app/seed/data/.last_retrain

EXPOSE 5000
ENTRYPOINT ["sh", "/app/docker/docker-entrypoint.sh"]
