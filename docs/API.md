# 智海巡航 API 文档 `v2.1`

> **项目名称：** 智海巡航（港口船舶违规行为智能识别平台）
>
> **Base URL:** `http://<host>:<port>/api/v1`
>
> **版本说明：** 本版基于 `frontend/frontend/backend/app.py` 实际路由重写（2026-08-22 核对），共约 **62 个端点**。

---

## 1. 通用约定

### 响应格式

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | `200` 成功，`4xx` 客户端错误，`5xx` 服务端错误 |
| `data` | object/array | 响应数据体 |
| `message` | string | 错误或操作提示信息 |

大部分接口统一由 `ok(data)` 包装返回 `{"code": 200, "data": ...}`。

### 认证

- 除 `auth/register`、`auth/login` 外，绝大多数接口都需登录（Flask session）。
- 所有数据查询按**用户作用域**隔离（`user_scope`）：`system` / `default` 用户可见全局数据，其余用户只见自己的检测结果。
- 未登录访问返回 401。

### 违规状态枚举

| 值 | 说明 |
|-----|------|
| `pending` | 待处置 |
| `processing` | 处置中 |
| `done` | 已处置 |

### 风险等级枚举

| 值 | 说明 |
|-----|------|
| `high` | 高风险 |
| `mid` | 中风险 |
| `low` | 低风险 |

---

## 2. 认证 `auth`

### `POST /auth/register`

注册新用户（SHA256 密码存储）。

**请求体：** `{"username": "...", "password": "...", ...}`

### `POST /auth/login`

登录（写 Flask session）。

**请求体：** `{"username": "...", "password": "..."}`

### `POST /auth/logout`

退出登录。

### `GET /auth/status`

当前会话状态（是否登录、当前用户）。

---

## 3. 仪表盘 `dashboard`（数据来自 `detection_results` 表）

> 均为 `@login_required`。空数据时部分接口返回**硬编码演示数据**（见各接口说明）。

### `GET /dashboard/stats`

顶部统计卡片。

**响应 `data`：**

```json
{
  "shipsTotal": 603,
  "violationShips": 42,
  "violationTotal": 47,
  "aisTotal": 128400,
  "avgSpeed": 8.6,
  "avgMaxSpeed": 14.2,
  "maxSpeedRecord": 32.5,
  "shipTypes": [{"type": "has_risk", "count": 12}],
  "dataTimeStart": "2026-08-01",
  "dataTimeEnd": "2026-08-21"
}
```

### `GET /dashboard/traffic-trend`

按日统计检测量与违规量（近 14 天）。**无数据时返回硬编码演示数据。**

**响应 `data`：** `{"dates": ["07/25", ...], "aisPoints": [42000, ...], "activeShips": [85, ...]}`

### `GET /dashboard/violation-distribution`

违规类型分布（经 `violation_catalog.describe()` 归一化为中文标签）、风险等级分布、违规日趋势。**无数据时返回硬编码演示数据。**

**响应 `data`：**

```json
{
  "types": [{"type": "超速", "count": 15}],
  "riskLevels": [{"risk_level": "high", "count": 15}],
  "dailyTrend": [{"day": "07/20", "count": 3}]
}
```

### `GET /dashboard/alerts`

最近 20 条违规告警。**`position` 为写死值 `{lat: 29.582, lng: 106.663}`。**

**响应 `data`：** 数组，元素含 `id / time / shipName(=mmsi) / mmsi / violationType / tagType / position / riskLevel`。

> `tagType` 映射：`high→danger / mid→warning / low→info`。

---

## 4. 船舶监控 `ships`

### `GET /ships`

船舶列表（从 `detection_results` 去重查询）。**`position` 写死 `{lat: 29.582, lng: 106.663}`。**

**参数：** `search` (MMSI 模糊), `page`, `pageSize`（默认 50）

**响应 `data`：**

```json
{
  "total": 603,
  "list": [
    {
      "mmsi": "412345678",
      "name": "412345678",
      "type": "has_risk",
      "status": "航行中",
      "speed": 8.6,
      "course": 0,
      "position": {"lat": 29.582, "lng": 106.663},
      "dotColor": "violating",
      "totalPoints": 245
    }
  ]
}
```

> `dotColor`：`violating`（违规）/ `red`（avg_speed>15）/ `yellow`（<1）/ `green`。

### `GET /ships/<mmsi>/track`

获取某船最近一次检测详情（含轨迹、证据链）。

### `GET /ships/all-tracks`

全量轨迹 + WaterChecker 陆地标记（带内存缓存）。

### `GET /ships/speed-stats`

航速分布统计。

---

## 5. 违规管理 `violations`

> 数据来自 `detection_results` 表的 `violation_label` / `violation_event` 列（run.py 写入）。

### `GET /violations`

违规列表。

**参数：** `page`, `pageSize`

**响应 `data`：**

```json
{
  "total": 47,
  "list": [
    {
      "id": 1,
      "mmsi": "412345678",
      "violation_label": "非法捕捞",
      "violation_event": "非法捕捞",
      "risk_level": "high",
      "final_assessment": "...",
      "recommended_action": "...",
      "analysis_time": "2026-08-20 10:00:00"
    }
  ]
}
```

### `GET /violations/<int:vid>`

违规详情（按 id 查 `detection_results`）。

> ⚠️ **注意：`POST /violations/:id/disposal`（提交处置）无后端实现。** 前端 `src/api/violations.js` 中留有 `submitDisposal` 存根（`POST /violations/{id}/disposal`），但后端无此路由，调用会 404。文书上报功能当前为**未接入状态**（`ReportDocuments.vue` 仅展示违规列表）。

---

## 6. 智能体 `ai`

> AI 对话已接入**真实 ShipClaw 可养成智能体**（不再是「测试中」）。

### `GET /ai/profile`

智能体资料（名称、头像、Logo、颜色配置）。

### `PUT /ai/profile`

更新智能体资料（改名、改头像）。

### `PUT /ai/colors`

更新船舶轨迹/状态颜色配置。

**请求体：**

```json
{
  "colors": {
    "normal": "#0f9d58",
    "anchored": "#f59e0b",
    "berthed": "#4285f4",
    "violating": "#ea4335",
    "track": "#1a73e8"
  }
}
```

### `POST /ai/chat`

智能体对话（聊天界面与悬浮球共用）。

**请求体：** `{"message": "查询闽海之星最近航行轨迹"}`

**响应 `data`**（ShipClaw 成功时）：

```json
{
  "reply": "自然语言回答",
  "skill": "ship_query",
  "tool_calls": [{"name": "ship_query", "label": "船舶查询", "status": "ok"}],
  "level": 5,
  "exp_gained": 10,
  "level_up": false,
  "growth_event": null,
  "personality": {"专业度": 52, "亲切度": 50, "严谨度": 50, "主动性": 50}
}
```

- 优先走 ShipClaw（技能路由 + 养成 + 记忆）；ShipClaw 不可用时降级为直接 Ollama 调用（仅返回 `reply`）；再失败则返回兜底问候。
- 登录用户身份来自会话（`_current_uid()`），防伪造。

### `GET /ai/tools`

智能体工具目录（9 个内置技能 + 当前用户自定义技能）。新增 `violation_evidence`（违规证据链，Lv4）：按 MMSI 查单船违规判定的完整依据——① 轨迹分析 ② 规则匹配 ③ 法规引用 ④ LLM 复核，回答"这艘船怎么违规的、对应法规第几条"。

### `GET /ai/query/ship` / `GET /ai/query/violation-stats` / `GET /ai/query/zone-status`

数据查询工具接口（供智能体工具调用）。

### `POST /ai/logo`

AI 生成 Logo（返回 SVG data URI）。

---

## 7. 开发者 `dev`

> LLM 全局配置，全部 `@login_required`（已登录成员即可访问）；保存后全局即时生效并持久化到 `data/llm_config.json`。

### `GET /dev/llm-config`

获取全局 LLM 配置。

### `PUT /dev/llm-config`

保存全局 LLM 配置（校验后落盘）。

**请求体：** `{"base_url": "...", "model": "...", "temperature": 0.7, "num_predict": 600, "top_p": null, "top_k": null, "system_prompt": ""}`

### `POST /dev/llm-config/test`

用当前配置直连 Ollama 发短 prompt 验证连通性。

**响应 `data`：** `{"reply": "...", "elapsed_ms": 1234}`（失败返回 502）。

### `GET /dev/models`

列出 Ollama 已安装模型。

**响应 `data`：** `{"online": true, "models": [{"name": "...", "size": 0, "modified_at": ""}]}`

### `POST /dev/llm-config/reset`

恢复 LLM 默认配置。

### 自定义技能 CRUD `dev/skills`

| 端点 | 方法 | 说明 |
|------|------|------|
| `/dev/skills/template` | GET | 技能模板（skill.md 格式） |
| `/dev/skills/validate` | POST | 校验技能 md（name/触发词/正文等） |
| `/dev/skills` | GET/POST | 技能列表 / 新建 |
| `/dev/skills/<int:sid>` | GET/PUT/DELETE | 详情 / 更新 / 删除 |

> 技能按登录用户隔离（`custom_skills` 表）；`shipclaw/skills.py` 的 `validate_custom_skill_md()` 校验 name 为小写英文且不冲突内置、label/description 必填、min_level 1~10、触发词非空、正文非空。

---

## 8. ShipClaw 养成 `claw`

| 端点 | 方法 | 说明 |
|------|------|------|
| `/claw/status` | GET | 智能体状态（等级 / 经验 / 性格） |
| `/claw/personality` | GET/PUT | 四维性格读取 / 更新 |
| `/claw/growth-log` | GET | 成长日志 |
| `/claw/preferences` | GET/POST | 偏好读取 / 更新（自动学习 focus_zones / frequent_ships / language_style） |

---

## 9. 级联 `cascade`

### `GET /cascade/stats`

检测统计。**accuracy 当前硬编码 `96.8`（非真实计算）。**

### `GET /cascade/logs`

`cascade_logs` 表最新 500 条日志（用户隔离）。字段：`id / user_id / timestamp(毫秒) / date / stage / level / message`。

### `GET /cascade/accuracy`

从 `training_set` 表动态计算 ML vs LLM 一致率（无表时回退 `96.0`）。

---

## 10. 区域分析 `zone`

### `POST /zone/analyze`

多边形框选区域行为分析（射线法，纯 Python 计算方向 / 速度 / 时间分布）。

---

## 11. 数据上传 `data`

### `POST /data/upload`

**唯一检测入口**：接收 CSV 上传，落盘后调用 `CascadeAgent.scan_csv()` 触发全链路检测（后台任务）。

### `GET /data/user-stats`

当前用户数据概况。

### `GET /data/detection-results`

检测结果列表。

### `GET /data/detection-result/<mmsi>`

某船检测详情。

### `GET /data/detection-stats`

检测统计。

---

## 12. 用户 `user`

| 端点 | 方法 | 说明 |
|------|------|------|
| `/user/profile` | GET/PUT | 个人资料读取 / 更新 |
| `/user/password` | POST | 修改密码 |
| `/user/avatar` | POST | 上传头像 |

另：`GET /avatars/<filename>` 提供头像静态文件。

---

## 13. 管理 `admin`（仅超级管理员）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/users` | GET | 用户列表 |
| `/admin/users/<uid>/role` | PUT | 修改用户角色 |

---

## 14. 组织 `orgs`（管理员 / 超级管理员）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/orgs` | POST/GET | 创建组织 / 组织列表 |
| `/orgs/<org_id>` | GET/DELETE | 组织详情 / 删除 |
| `/orgs/<org_id>/members` | POST | 添加成员 |
| `/orgs/<org_id>/members/<uid>` | DELETE | 移除成员 |

---

## 附录：已知实现状态（与文档 v2.0 的差异）

| 项目 | 状态 |
|------|------|
| AI 对话 | ✅ **已接入真实 ShipClaw + Ollama 降级**（v2.0 写「测试中」已过时） |
| 文书上报（`/reports`、`POST /violations/:id/disposal`） | ❌ **后端未实现**；`disposal_docs` 表存在但无代码写入 |
| 仪表盘假数据 | ⚠️ `traffic-trend` / `violation-distribution` / `cascade/stats` 在无数据时返回硬编码演示值 |
| 船舶 position | ⚠️ 当前写死 `{lat: 29.582, lng: 106.663}`，非真实轨迹位置 |
| detection_results 表 | 由 app.py 运行时 `CREATE TABLE IF NOT EXISTS` 创建（不在 `database/schema.sql`），列含 `violation_label` / `violation_event` |
