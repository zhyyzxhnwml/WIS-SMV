"""
E:\AIS 项目根目录 — 项目结构说明
==================================

智海巡航 — 港口船舶违规行为智能识别平台

【三层架构】
  L1: ais_accelerator/    — AIS数据加速器（质量清洗→分段→事件检测→特征提取）
  L2: triage_engine/      — ML路由引擎（随机森林四分类 + pgvector/Milvus检索）
  L3: llm_layer/          — LLM判定层（Qwen2.5 + 四级Prompt模板 + 反馈闭环）
  memory/                 — 智能体记忆系统（会话记忆/船舶记忆/情景记忆）

【Web前端】
  frontend/frontend/      — Vue 3 + Vite 前端
  frontend/frontend/backend/app.py — Flask后端API

【数据库】
  database/schema.sql     — PostgreSQL + pgvector 完整建表SQL
  环境变量配置连接        — PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASSWORD

【运行入口】
  run.py                  — 三级级联全链路运行（CLI模式）
  start.py                — Web服务启动（Flask后端 + 前端静态文件服务）
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.absolute()
DATA_DIR = PROJECT_ROOT / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
MEMORY_DIR = DATA_DIR / "memory"