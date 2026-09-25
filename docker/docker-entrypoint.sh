#!/bin/sh
# ═══════════════════════════════════════════════════════════════
# 智海巡航容器入口：
#   1) 确保数据卷可写目录存在
#   2) 首次启动把镜像内种子数据（geo 掩码 / LLM 配置等）no-clobber 拷入数据卷
#   3) exec 启动 Flask（PID 1，正确收 SIGTERM）
# ═══════════════════════════════════════════════════════════════
set -eu

SEED=/app/seed/data
DATA=/app/data

# 1) 可写运行目录（命名卷首次挂载时为空）
mkdir -p "$DATA/input" "$DATA/output" "$DATA/cache" "$DATA/logs" "$DATA/memory" "$DATA/avatars"

# 2) 种子数据 → 数据卷（no-clobber：绝不覆盖用户已有数据）
if [ -d "$SEED" ]; then
  cp -rn "$SEED/." "$DATA/" || true
fi

# 3) 启动 Web 服务（--prod 关调试、关 reloader）
exec python start.py --prod --port 5000
