"""
permissions.py — 权限点定义与三个内置角色的固定权限集
========================================================

【这个文件做什么？】
  系统**固定三个角色，不增不减**：超级管理员 / 管理员 / 用户。
  本模块把「谁能做什么」从散落的角色字符串比较，收敛成一组具名权限点。

【为什么需要它？】
  现有鉴权是 8 处 @role_required("管理员","超级管理员") 的字符串比较（app.py:238-262）。
  新增的两处收紧（provider 管理含 API 密钥、插件启用影响全局判定）用角色串表达
  会越写越糊，且无法回答「这个角色到底能干什么」。权限点让这件事可枚举、可展示、可审计。

【明确不做】
  - 不开放自定义角色：roles 表只播种这三个 builtin，无 CRUD 路由、无管理页面
  - 不替换 @role_required：现有 8 处一行不改，两者并存。
    ROLE_PERMISSIONS 的角色集与现有 @role_required 的可达性**严格等价**，
    保证引入权限点不改变任何现有路由的访问结果。

【与前端的关系】
  权限点随 /api/v1/auth/status 下发给前端（store/user.js 的 perms + can()），
  菜单与按钮据此显隐。
"""

from __future__ import annotations

# ── 角色名（与 users.role 的存储值严格一致） ──
# 注意：普通用户在库里的字串就是「用户」，不是「普通用户」。文档与 UI 文案统一称
# 「普通用户」，但代码/数据不动 —— 改字串要迁移存量数据，收益不抵风险。
ROLE_SUPER = "超级管理员"
ROLE_ADMIN = "管理员"
ROLE_USER = "用户"

ALL_ROLES = (ROLE_SUPER, ROLE_ADMIN, ROLE_USER)

# ── 展示名与说明 ──
# 这里就是「代码不动、叫法改一下」的落点：库里的字串仍是「用户」，
# 界面上统一显示为「普通用户」。所有面向用户的角色名都必须经 role_label()，
# 不要直接把 role 原始字串贴到 UI 上。
ROLE_LABELS: dict[str, str] = {
    ROLE_SUPER: "超级管理员",
    ROLE_ADMIN: "管理员",
    ROLE_USER: "普通用户",
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_SUPER: "系统所有者。独占用户管理（创建/停用/改角色/重置密码），"
                "并拥有全部权限。",
    ROLE_ADMIN: "业务负责人。管本组织与成员、配置模型与提供商、"
                "查看审计日志；不能管理用户账号。",
    ROLE_USER: "一线业务人员（只读的领导也用它）。查违规、看证据、导出报告、上传数据、"
               "用智能体问数；可让 AI 起草与试运行插件，但不能启用。",
}


def role_label(role: str | None) -> str:
    """角色的展示名（「用户」→「普通用户」）。未知角色原样返回。"""
    return ROLE_LABELS.get(role or "", role or "")


def role_description(role: str | None) -> str:
    return ROLE_DESCRIPTIONS.get(role or "", "")


# ── 权限点清单 ──
# key 用 <资源>.<动作> 形式；group 仅用于 UI 分组展示。
PERMISSIONS: list[dict] = [
    # 数据
    {"key": "violation.view",   "label": "查看违规数据",   "group": "数据"},
    {"key": "violation.export", "label": "导出违规报告",   "group": "数据"},
    {"key": "data.upload",      "label": "上传 AIS 数据",  "group": "数据"},
    # 可执行插件（阶段 2：dsh 风格，manifest + Python 判定代码）
    # 命名说明：这两个键曾是预声明的 rulepack.*，从未被任何代码写入过，故直接改名 ——
    # 用户与 UI 的词汇都是「插件」，留两套并行权限点会让「这角色到底能干什么」答不清。
    {"key": "plugin.draft",   "label": "起草/编辑/试跑插件", "group": "插件",
     "desc": "可自由生成、校验、试跑；草稿不参与线上判定，故对所有角色开放"},
    {"key": "plugin.publish", "label": "启用/停用/回滚插件", "group": "插件",
     "desc": "启用后插件对所有用户的判定生效且跑的是真代码，故收紧到超级管理员"},
    # 模型
    {"key": "model.config",     "label": "配置模型与提供商", "group": "模型",
     "desc": "含 API 密钥的读写，故收紧到管理员"},
    # 系统管理
    {"key": "user.manage",      "label": "用户管理",       "group": "系统管理"},
    {"key": "org.manage",       "label": "组织管理",       "group": "系统管理"},
    {"key": "audit.view",       "label": "查看审计日志",   "group": "系统管理"},
    {"key": "pipeline.run",     "label": "触发流水线任务", "group": "系统管理"},
]

PERMISSION_KEYS: tuple[str, ...] = tuple(p["key"] for p in PERMISSIONS)

PERMISSION_LABELS: dict[str, str] = {p["key"]: p["label"] for p in PERMISSIONS}


# ── 三个内置角色的固定权限集 ──
# 对齐现有行为（这一条是硬约束，改动前先核对 app.py 的 8 处 @role_required，
# 逐一核过：admin_users/admin_set_role 是超管专属；org_* 六处是管理员及以上）：
#   仅超管：用户管理（app.py:3286, 3316）
#   管理员及以上：组织管理（app.py:3367-3488 的六个路由）
#   三个角色都可：登录后的数据/对话/开发者页（@login_required）
_USER_PERMISSIONS = [
    "violation.view",
    "violation.export",
    "data.upload",
    "plugin.draft",       # 可让 AI 起草、编辑、试跑，但不能启用
]

# 注意 plugin.publish 不在这里。用户原话是「全局共享，**超管**启用」——
# 插件跑的是真代码、启用后影响所有人的判定，比原来的 rulepack.publish 更该收紧。
# 超管通过 ROLE_SUPER = list(PERMISSION_KEYS) 自动拥有它。
_ADMIN_PERMISSIONS = [
    *_USER_PERMISSIONS,
    "model.config",
    "org.manage",
    "audit.view",
    "pipeline.run",
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    ROLE_SUPER: list(PERMISSION_KEYS),   # 超管 = 全部
    ROLE_ADMIN: _ADMIN_PERMISSIONS,
    ROLE_USER: _USER_PERMISSIONS,
}


def permissions_for(role: str | None) -> list[str]:
    """返回某角色的权限点列表。未知/空角色 → 空列表（默认拒绝）。"""
    return list(ROLE_PERMISSIONS.get(role or "", []))


def has(role: str | None, key: str) -> bool:
    """判断角色是否拥有某权限点。"""
    return key in ROLE_PERMISSIONS.get(role or "", ())


def is_known_permission(key: str) -> bool:
    """校验权限点是否已定义（供 API 层挡掉拼错的动作名）。"""
    return key in PERMISSION_KEYS
