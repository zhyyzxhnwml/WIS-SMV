"""
audit.py — 审计日志写入与查询
================================

【这个文件做什么？】
  记录「谁在什么时候对什么做了什么、结果如何」。阶段 0 只覆盖认证与系统管理动作
  （登录/登出/登录失败、用户增删改、组织增删改、系统参数修改）；
  provider 与密钥变更在阶段 1 接入，插件动作在阶段 2 接入。

【两条硬约束】
  1. **写入失败绝不抛异常**。审计是旁路，不能拖垮业务 —— 登录该成功还得成功。
     失败时打一条 stderr 日志（logger 由调用方配置，这里只 print 到 stderr），
     真实原因不吞掉，便于排查。
  2. **不导入 app**。app.py 会 import audit，反向 import 会成环。所以本模块
     直接读 flask 的 session/request 代理对象；离开请求上下文（CLI 调用）时
     这些代理会抛 RuntimeError，一律吞掉并按「无用户 / 无 IP」记录。

【连接】
  用 pg_db.connect() 取独立短连接，与 app.py 的 get_db() 行为一致
  （该函数本就是「每次请求独立连接」）。写审计不复用业务连接是刻意的：
  业务事务回滚不该把审计记录一起回滚，反之亦然。
"""

from __future__ import annotations

import json
import sys

# ── 动作名清单 ──
# 用 <领域>.<动作> 形式，值是该动作的中文标签（审计页展示用）。
# 新增动作时在这里补一行；没登记的 action 也能落库，UI 退化为显示原始 action，
# 这样漏登记一个动作不会导致写不进日志。
KNOWN_ACTIONS: dict[str, str] = {
    # 认证
    "auth.login":            "登录",
    "auth.login_failed":     "登录失败",
    "auth.logout":           "登出",
    "auth.register":         "注册",
    "auth.denied":           "越权访问被拒",
    # 用户
    "user.create":           "创建用户",
    "user.update_role":      "修改用户角色",
    "user.reset_password":   "重置密码",
    "user.reset_self":       "修改自己的密码",
    "user.update_profile":   "修改用户资料",
    "user.disable":          "停用用户",
    "user.enable":           "启用用户",
    "user.delete":           "删除用户",
    # 组织
    "org.create":            "创建组织",
    "org.update":            "修改组织",
    "org.delete":            "删除组织",
    "org.add_member":        "加入组织",
    "org.remove_member":     "移出组织",
    # 系统参数 —— 仅用于渲染历史记录。
    # 「系统参数」功能已按需求整体移除（级联判定阈值不作为运行时旋钮暴露），
    # 不再有任何地方写这两种动作；保留标签是为了老记录在审计页里仍显示中文。
    "settings.update":       "修改系统参数",
    "settings.reset":        "恢复参数默认值",
    # 模型与提供商（阶段 1 接入）
    "provider.create":       "新建模型提供商",
    "provider.update":       "修改模型提供商",
    "provider.delete":       "删除模型提供商",
    "provider.key_update":   "修改 API 密钥",
    "binding.update":        "修改模型用途绑定",
    # 可执行插件（阶段 2：dsh 风格，manifest + Python 判定代码）
    # 这组动作替换了原先预声明的 rulepack.*（从未被任何代码写入过）。
    # ai_draft / trial 是「不影响线上」的草稿期动作；activate 之后才是真生效。
    "plugin.ai_draft":       "AI 起草插件",
    "plugin.create":         "新建插件",
    "plugin.update":         "编辑插件（新版）",
    "plugin.validate":       "校验插件",
    "plugin.trial":          "试跑插件",
    "plugin.activate":       "启用插件",
    "plugin.disable":        "停用插件",
    "plugin.rollback":       "回滚插件版本",
    "plugin.delete":         "删除插件",
    # 下面两条由系统自己写（不是人在页面上点的），是运行期熔断与风险抬升的留痕：
    #   auto_suspend —— 连续失败达阈值后自动停用，必须有据可查「哪来的插件不见了」
    #   risk_raise   —— 插件把某船的 risk_level 抬了一级，是唯一能影响基线结论的路径
    "plugin.auto_suspend":   "插件自动熔断",
    "plugin.risk_raise":     "插件抬升风险等级",
    # 数据
    "data.upload":           "上传数据",
    "data.clean":            "清理数据",
    "report.export":         "导出报告",
    "pipeline.run":          "触发流水线",
}

RESULT_OK = "ok"
RESULT_FAIL = "fail"
RESULT_DENIED = "denied"

_RESULT_LABELS = {RESULT_OK: "成功", RESULT_FAIL: "失败", RESULT_DENIED: "拒绝"}


def label_for(action: str) -> str:
    """动作的中文标签；未登记的动作原样返回。"""
    return KNOWN_ACTIONS.get(action, action)


def result_label(result: str) -> str:
    return _RESULT_LABELS.get(result, result)


# ═══════════════════════════════════════════════════════════
# 请求上下文勘采（全部容错）
# ═══════════════════════════════════════════════════════════

def _client_ip() -> str:
    """取客户端 IP。优先 X-Forwarded-For（容器/反代部署下 remote_addr 是网关）。"""
    try:
        from flask import request
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
        return (request.remote_addr or "")[:64]
    except Exception:
        return ""


def _actor() -> tuple[str, str]:
    """取当前登录用户的 (id, username)。未登录/无请求上下文 → ('', '')。

    session 里只有 uid，所以 username 需要查库。为避免一次审计多开一条连接，
    这里查库失败就只留 uid；调用方也可显式传 username 覆盖。
    """
    try:
        from flask import session
        uid = session.get("uid") or ""
    except Exception:
        return "", ""
    if not uid:
        return "", ""
    try:
        import pg_db
        conn = pg_db.connect()
        row = conn.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
        conn.close()
        return uid, (row["username"] if row else "")
    except Exception:
        return uid, ""


# ═══════════════════════════════════════════════════════════
# 写入
# ═══════════════════════════════════════════════════════════

def log(action: str,
        target_type: str = "",
        target_id: str = "",
        detail: dict | None = None,
        result: str = RESULT_OK,
        *,
        user_id: str | None = None,
        username: str | None = None) -> bool:
    """写一条审计日志。返回是否成功 —— **调用方不需要也不应该检查返回值**。

    参数：
      action      动作名，建议取自 KNOWN_ACTIONS
      target_type 操作对象类型，如 'user' / 'org' / 'setting'
      target_id   操作对象标识
      detail      结构化细节（会被 JSON 编码；不要塞密码、密钥原文）
      result      'ok' / 'fail' / 'denied'
      user_id     显式指定操作者，省略则从 session 推断。
                  **登录失败时 session 为空，但你想记录「谁试图登录」→ 用 username
                  传被尝试的用户名，用 user_id='' 表示未认证。**
      username    显式指定操作者用户名，省略则从 session 推断

    注意：登录失败这一类「操作者未知但目标已知」的记录，把被尝试的用户名放进
    detail（如 {'username': 'alice'}），username 字段留给已认证的身份。
    """
    try:
        if user_id is None or username is None:
            _uid, _uname = _actor()
            if user_id is None:
                user_id = _uid
            if username is None:
                username = _uname

        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)

        import pg_db
        conn = pg_db.connect()
        conn.execute(
            "INSERT INTO audit_logs "
            "(ts, user_id, username, action, target_type, target_id, detail, ip, result) "
            "VALUES (to_char(now(),'YYYY-MM-DD HH24:MI:SS'), ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id or "", username or "", action or "",
             target_type or "", str(target_id or ""), payload,
             _client_ip(), result or RESULT_OK),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        # 审计失败绝不能冒泡到业务路径。打到 stderr 保留排查线索。
        print(f"[audit] 写入失败 action={action!r}: {e}", file=sys.stderr)
        return False


# ═══════════════════════════════════════════════════════════
# 查询（供 /api/v1/admin/audit-logs）
# ═══════════════════════════════════════════════════════════

def search(*, action: str = "", username: str = "", result: str = "",
           date_from: str = "", date_to: str = "",
           page: int = 1, page_size: int = 50) -> dict:
    """分页查询审计日志。返回 {'total': int, 'items': [...]}；出错返回空结果。

    筛选条件全为可选；日期按 ts 的字符串前缀比较（ts 存的是
    'YYYY-MM-DD HH24:MI:SS' 文本，与本项目其它时间列的存法一致）。
    """
    where: list[str] = []
    params: list = []
    if action:
        where.append("action = ?")
        params.append(action)
    if username:
        where.append("username LIKE ?")
        params.append(f"%{username}%")
    if result:
        where.append("result = ?")
        params.append(result)
    if date_from:
        where.append("ts >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        where.append("ts <= ?")
        params.append(f"{date_to} 23:59:59")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    page = max(1, int(page or 1))
    page_size = min(200, max(1, int(page_size or 50)))

    try:
        import pg_db
        conn = pg_db.connect()
        total_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM audit_logs{clause}", tuple(params)
        ).fetchone()
        total = int(total_row["n"]) if total_row else 0
        rows = conn.execute(
            f"SELECT id, ts, user_id, username, action, target_type, target_id, "
            f"detail, ip, result FROM audit_logs{clause} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, (page - 1) * page_size),
        ).fetchall()
        conn.close()
    except Exception as e:
        return {"total": 0, "items": [], "error": str(e)}

    items = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.get("detail") or "{}")
        except (TypeError, ValueError):
            d["detail"] = {}
        d["action_label"] = label_for(d.get("action", ""))
        d["result_label"] = result_label(d.get("result", ""))
        items.append(d)

    return {"total": total, "items": items, "page": page, "page_size": page_size}


def distinct_actions() -> list[dict]:
    """库里实际出现过的动作（供筛选下拉），带中文标签。出错返回已登记的全集。"""
    try:
        import pg_db
        conn = pg_db.connect()
        rows = conn.execute(
            "SELECT action, COUNT(*) AS n FROM audit_logs GROUP BY action ORDER BY n DESC"
        ).fetchall()
        conn.close()
        return [{"action": r["action"], "label": label_for(r["action"]), "count": int(r["n"])}
                for r in rows]
    except Exception:
        return [{"action": k, "label": v, "count": 0} for k, v in KNOWN_ACTIONS.items()]
