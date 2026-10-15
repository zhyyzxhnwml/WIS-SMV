# -*- coding: utf-8 -*-
"""插件三张表的读写。SQL 全部收在这里，路由层只做参数校验与审计。

【为什么不把 SQL 写在 app.py 的路由里】
  插件的读路径有三处调用方：开发者页（app.py）、判定链（registry.py）、
  对话技能池（shipclaw/skills.py）。三处各写一份 SELECT 迟早会漂移成
  「网页上能看到、判定链看不到」这种最难查的不一致。收在这里，只有一份定义。

【仓库约定（照抄，别引入新风格）】
  · `?` 占位符 —— pg_db._Compat 会翻成 %s
  · 每次 connect() 是独立连接，写完必须 commit + close
  · 时间是 TEXT 列，用 to_char(now(),...) 而不是 NOW()
  · 建表在 app.py 的 DB_TABLES_SQL 里幂等建，没有迁移文件
"""
from typing import Any

# 版本行不可变：每次保存都新增一行，current_version 指过去。回滚只改指针。
STATUSES = ("draft", "enabled", "disabled", "rejected", "suspended")
ORIGINS = ("manual", "ai", "system")

_NOW = "to_char(now(),'YYYY-MM-DD HH24:MI:SS')"

_PLUGIN_COLS = (
    "id, name, label, icon, description, scope, origin, status, trust, "
    "author_user_id, author_username, created_by_model, current_version, "
    "can_raise_risk, trigger_keywords, min_level, enabled_at, enabled_by, "
    "error_streak, last_error, created_at, updated_at"
)


def _row(row) -> dict | None:
    return dict(row) if row else None


# ═══════════════ 读 ═══════════════

def list_plugins(conn, user_id: str = "", *, see_all: bool = False) -> list[dict]:
    """列表可见性规则写在 SQL 里，不写在路由里。

    超管 see_all=True 看全部；其他人看「自己起草的 + 全部已启用的」——
    启用中的插件是全局生效的，看不到反而会奇怪「判定结果里那条是谁给的」。
    system 预置插件（origin='system'）对所有人可见。
    """
    if see_all:
        sql = f"SELECT {_PLUGIN_COLS} FROM plugins ORDER BY id DESC"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    sql = (f"SELECT {_PLUGIN_COLS} FROM plugins "
           "WHERE status = 'enabled' OR origin = 'system' OR author_user_id = ? "
           "ORDER BY id DESC")
    return [dict(r) for r in conn.execute(sql, (user_id or "",)).fetchall()]


def get_plugin(conn, pid: int) -> dict | None:
    return _row(conn.execute(
        f"SELECT {_PLUGIN_COLS} FROM plugins WHERE id = ?", (pid,)).fetchone())


def get_plugin_by_name(conn, name: str) -> dict | None:
    return _row(conn.execute(
        f"SELECT {_PLUGIN_COLS} FROM plugins WHERE name = ?", (name,)).fetchone())


def get_version(conn, plugin_id: int, version: int) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM plugin_versions WHERE plugin_id = ? AND version = ?",
        (plugin_id, version)).fetchone())


def list_versions(conn, plugin_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, plugin_id, version, source_sha256, facts_schema, validation_report, "
        "created_by, created_by_model, created_at, "
        "length(source_py) AS source_bytes "
        "FROM plugin_versions WHERE plugin_id = ? ORDER BY version DESC",
        (plugin_id,)).fetchall()
    return [dict(r) for r in rows]


def load_runnable(conn, *, names: list[str] | None = None) -> list[dict]:
    """取出「可执行」插件：status='enabled' 且带上它当前版本的 manifest 与源码。

    判定链和对话技能池都走这里。**每次运行前重新调一次**，不在模块导入时缓存 ——
    否则超管启用/停用后要重启才生效，他会以为「点了没反应」。
    """
    sql = ("SELECT p.id, p.name, p.label, p.icon, p.description, p.origin, p.trust, "
           "p.current_version, p.can_raise_risk, p.trigger_keywords, p.min_level, "
           "p.error_streak, v.manifest_yaml, v.source_py, v.facts_schema "
           "FROM plugins p JOIN plugin_versions v "
           "ON v.plugin_id = p.id AND v.version = p.current_version "
           "WHERE p.status = 'enabled'")
    params: tuple = ()
    if names:
        marks = ",".join("?" for _ in names)
        sql += f" AND p.name IN ({marks})"
        params = tuple(names)
    sql += " ORDER BY p.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_findings(conn, plugin_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM plugin_findings WHERE plugin_id = ? ORDER BY id DESC LIMIT ?",
        (plugin_id, max(1, min(500, limit)))).fetchall()
    return [dict(r) for r in rows]


# ═══════════════ 写 ═══════════════

def create_plugin(conn, *, name: str, label: str, icon: str, description: str,
                  origin: str, trust: str, plugin_text: str, source_sha256: str,
                  validation_report: str, author_user_id: str = "",
                  author_username: str = "", created_by_model: str = "",
                  can_raise_risk: bool = False, trigger_keywords: str = "",
                  min_level: int = 1) -> int:
    """新建插件 = 插一行 plugins + 插版本 1。两步同一事务（不 commit 中间态）。

    plugin_text 是**插件文件全文**（frontmatter + Python 正文）。manifest_yaml 只是
    从它切出来的便利副本，供页面上分块编辑 —— 不作为第二份输入，免得两者漂移。
    """
    row = conn.execute(
        "INSERT INTO plugins (name, label, icon, description, scope, origin, status, "
        "trust, author_user_id, author_username, created_by_model, current_version, "
        "can_raise_risk, trigger_keywords, min_level) "
        "VALUES (?, ?, ?, ?, 'global', ?, 'draft', ?, ?, ?, ?, 1, ?, ?, ?) RETURNING id",
        (name, label, icon, description, origin, trust, author_user_id,
         author_username, created_by_model, bool(can_raise_risk),
         trigger_keywords, int(min_level))).fetchone()
    pid = row["id"]
    conn.execute(
        "INSERT INTO plugin_versions (plugin_id, version, manifest_yaml, source_py, "
        "source_sha256, facts_schema, validation_report, created_by, created_by_model) "
        "VALUES (?, 1, ?, ?, ?, 'v1', ?, ?, ?)",
        (pid, _fm_of(plugin_text), plugin_text, source_sha256, validation_report,
         author_username or author_user_id, created_by_model))
    conn.commit()
    return pid


def add_version(conn, plugin_id: int, *, plugin_text: str, source_sha256: str,
                validation_report: str, author_username: str = "",
                created_by_model: str = "", patch: dict | None = None) -> int:
    """加一版并把 current_version 指过去。

    版本号从 MAX(version)+1 取，不读 current_version+1 —— 回滚过之后
    current_version 会指向旧版，用它加一会撞上已存在的版本号。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM plugin_versions WHERE plugin_id = ?",
        (plugin_id,)).fetchone()
    version = int(row["v"]) + 1
    conn.execute(
        "INSERT INTO plugin_versions (plugin_id, version, manifest_yaml, source_py, "
        "source_sha256, facts_schema, validation_report, created_by, created_by_model) "
        "VALUES (?, ?, ?, ?, ?, 'v1', ?, ?, ?)",
        (plugin_id, version, _fm_of(plugin_text), plugin_text, source_sha256,
         validation_report, author_username, created_by_model))
    sets = ["current_version = ?", f"updated_at = {_NOW}"]
    params: list[Any] = [version]
    for col, value in (patch or {}).items():
        if col in ("label", "icon", "description", "trust", "can_raise_risk",
                   "trigger_keywords", "min_level", "status"):
            sets.append(f"{col} = ?")
            params.append(value)
    params.append(plugin_id)
    conn.execute(f"UPDATE plugins SET {', '.join(sets)} WHERE id = ?", tuple(params))
    conn.commit()
    return version


def set_status(conn, plugin_id: int, status: str, *, by: str = "",
               error: str = "") -> None:
    """改状态。启用时记 enabled_at/enabled_by，这是审计之外的运行期留痕。"""
    if status not in STATUSES:
        raise ValueError(f"未知状态 {status!r}")
    if status == "enabled":
        conn.execute(
            f"UPDATE plugins SET status = 'enabled', enabled_at = {_NOW}, "
            "enabled_by = ?, error_streak = 0, last_error = '', "
            f"updated_at = {_NOW} WHERE id = ?", (by, plugin_id))
    elif error:
        conn.execute(
            f"UPDATE plugins SET status = ?, last_error = ?, updated_at = {_NOW} "
            "WHERE id = ?", (status, error[:1000], plugin_id))
    else:
        conn.execute(
            f"UPDATE plugins SET status = ?, updated_at = {_NOW} WHERE id = ?",
            (status, plugin_id))
    conn.commit()


def rollback(conn, plugin_id: int, version: int) -> bool:
    """把 current_version 指回某一版。不删任何版本。"""
    exists = conn.execute(
        "SELECT 1 FROM plugin_versions WHERE plugin_id = ? AND version = ?",
        (plugin_id, version)).fetchone()
    if not exists:
        return False
    conn.execute(
        f"UPDATE plugins SET current_version = ?, error_streak = 0, last_error = '', "
        f"updated_at = {_NOW} WHERE id = ?", (version, plugin_id))
    conn.commit()
    return True


def delete_plugin(conn, plugin_id: int) -> None:
    """真删。findings 与 versions 一并清掉 —— 留着会指向不存在的插件。"""
    conn.execute("DELETE FROM plugin_findings WHERE plugin_id = ?", (plugin_id,))
    conn.execute("DELETE FROM plugin_versions WHERE plugin_id = ?", (plugin_id,))
    conn.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
    conn.commit()


def bump_error_streak(conn, plugin_id: int, error: str) -> int:
    """失败计数 +1，返回新值。调用方据此决定是否自动熔断。"""
    conn.execute(
        f"UPDATE plugins SET error_streak = error_streak + 1, last_error = ?, "
        f"updated_at = {_NOW} WHERE id = ?", (str(error)[:1000], plugin_id))
    conn.commit()
    row = conn.execute("SELECT error_streak FROM plugins WHERE id = ?",
                       (plugin_id,)).fetchone()
    return int(row["error_streak"]) if row else 0


def reset_error_streak(conn, plugin_id: int) -> None:
    conn.execute("UPDATE plugins SET error_streak = 0, last_error = '' WHERE id = ?",
                 (plugin_id,))
    conn.commit()


# ═══════════════ 运行产物 ═══════════════

def record_run(conn, *, plugin_id: int, plugin_version: int, mmsi: str,
               user_id: str, source_file: str, analysis_time: str, result: str,
               elapsed_ms: int, error_text: str, logs: list, findings: list[dict]) -> int:
    """落一次运行的产物。返回写入行数。

    两种情形都要留痕：
      · 成功 → 每条 finding 一行（result='ok'）
      · 失败/超时/被拒 → 一行 result<>'ok' 的空壳，error_text 与 logs 带上
    失败也留痕是必须的：error_streak 熔断要有依据，超管排障要有原文。
    """
    rows = 0
    if result == "ok" and findings:
        for f in findings:
            conn.execute(
                "INSERT INTO plugin_findings (plugin_id, plugin_version, mmsi, user_id, "
                "source_file, analysis_time, result, code, catalog_code, label, severity, "
                "confidence, evidence, metric, metric_unit, span_json, regulation_ref, "
                "raise_risk_to, error_text, logs_json, elapsed_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                (plugin_id, plugin_version, mmsi, user_id or "system", source_file,
                 analysis_time, f.get("code", ""), f.get("catalog_code", ""),
                 f.get("label", ""), f.get("severity", ""),
                 float(f.get("confidence") or 0),
                 f.get("evidence", ""), float(f.get("metric") or 0),
                 f.get("metric_unit", ""), _json(f.get("span") or []),
                 f.get("regulation_ref", ""), f.get("raise_risk_to", ""),
                 _json(logs), int(elapsed_ms)))
            rows += 1
    elif result != "ok":
        conn.execute(
            "INSERT INTO plugin_findings (plugin_id, plugin_version, mmsi, user_id, "
            "source_file, analysis_time, result, error_text, logs_json, elapsed_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plugin_id, plugin_version, mmsi, user_id or "system", source_file,
             analysis_time, result, str(error_text)[:2000], _json(logs), int(elapsed_ms)))
        rows = 1
    if rows:
        conn.commit()
    return rows


def _json(value) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return "[]"


# ═══════════════ 预置播种 ═══════════════

def seed_presets(conn, presets_dir) -> int:
    """把 plugins/<name>/plugin.py 播种成 origin='system' 插件。幂等，返回新建个数。

    【幂等判据是「同名已存在就跳过」，且绝不覆盖】
      这样超管在页面上把预置插件改过一版，重启不会被系统版本冲回去。
      代价是：改了仓库里的预置文件不会自动生效 —— 那本来也该是一次显式的
      「新建一版」，而不是启动时的静默覆盖。

    【播种成 draft 而不是 enabled】
      用户的要求是「全局共享，超管启用」。系统只负责把插件**交到手上**，
      启不启用由超管点。这也让「未启用时零影响」成为默认状态而不是需要验证的例外。
    """
    import hashlib
    from pathlib import Path

    from plugin_runtime.manifest import parse_plugin_file
    from plugin_runtime.validate import check_source

    root = Path(presets_dir)
    if not root.is_dir():
        return 0
    created = 0
    for path in sorted(root.glob("*/plugin.py")):
        try:
            text = path.read_text(encoding="utf-8")
            parsed = parse_plugin_file(text)
            meta = parsed["meta"]
            if get_plugin_by_name(conn, meta["name"]):
                continue                       # 已有同名（不论来源）→ 不碰
            report = _validation_report(parsed)
            create_plugin(
                conn,
                name=meta["name"], label=meta["label"], icon=meta["icon"],
                description=meta["description"], origin="system", trust=meta["trust"],
                plugin_text=text,
                source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                validation_report=report,
                author_user_id="system", author_username="系统预置",
                can_raise_risk=meta["can_raise_risk"],
                trigger_keywords=",".join(meta["trigger_keywords"]),
                min_level=meta["min_level"],
            )
            created += 1
        except Exception as e:
            # 预置插件坏掉不该让应用起不来，但必须吭声 —— 否则「插件页少了一个」
            # 会变成没人知道为什么的悬案
            print(f"    ⚠ 预置插件 {path} 播种失败：{e}")
    return created


def _fm_of(text: str) -> str:
    """取插件文件的 manifest 段原文，存进 plugin_versions.manifest_yaml。"""
    parts = text.split("---", 2)
    return parts[1].strip() if len(parts) >= 2 else ""


def _validation_report(parsed: dict) -> str:
    """跑一次静态校验，把结果存进 plugin_versions.validation_report。

    预置插件在启动时被校验一次，是为了让「仓库里的插件文件坏了」在启动日志里
    就暴露，而不是等超管启用之后在判定链上静默失效。
    """
    import json

    from plugin_runtime.validate import check_source
    try:
        stats = check_source(parsed["body"], parsed["meta"]["hooks"])
        return json.dumps({"ok": True, **stats}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
