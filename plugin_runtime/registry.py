# -*- coding: utf-8 -*-
"""插件注册表与执行编排 —— 判定链唯一的插件入口。

【它在哪里被调用】
  run.py 里，确定性风险引擎（assess_risk）跑完之后、落库之前。
  这个位置是刻意选的：此时 L1 事件、L2 路由、L3 LLM 判定、risk_engine 的确定性
  覆盖**全部尘埃落定**。插件拿到的是只读的 facts.base，而**代码层面根本没有**
  写 violation_detected / violation_code / risk_level 那三列的入口 ——
  这比任何「运行时约定不要改」都可靠。

【插件能改变的唯一一件事】
  `raise_risk_to`，且必须 can_raise_risk=true（超管逐个插件批准），且只升不降。
  每一次都单独记 plugin.risk_raise 审计。

【fail-closed】
  子进程崩溃/超时/校验不过 → 该插件对该船产出 0 条 findings，写一行
  plugin_findings(result='timeout'|'error'|...)，error_streak += 1。
  连续 3 次 → 自动 suspended + 审计。宁可漏判，不可让一个坏插件拖垮整批。

【每次调用都重读注册表】
  不在模块导入时缓存。否则超管在页面上点了启用要重启才生效 —— 他会以为点了没反应。
  SELECT 的代价（几百微秒）相对每船的子进程调用可以忽略。
"""
import json

from plugin_runtime import store
from plugin_runtime.facts import build_facts
from plugin_runtime.manifest import normalize_meta, parse_meta
from plugin_runtime.sandbox import get_sandbox
from plugin_runtime.validate import check_findings

# 连续失败多少次自动熔断
MAX_ERROR_STREAK = 3
# 风险等级序：只升不降靠它比较
_LEVEL_ORDER = {"low": 0, "mid": 1, "high": 2}
# 子进程报的 stage → 落库的 result。stage 是「哪一步炸的」，result 是「怎么归类」。
_STAGE_TO_RESULT = {
    "timeout": "timeout", "crash": "crash", "protocol": "error",
    "budget": "rejected", "spawn": "error", "manifest": "rejected",
    "validate": "rejected", "exec": "rejected", "apply": "error", "call": "error",
    "pipe": "error",
}


def preload_registry(conn=None) -> list[dict]:
    """每批数据开始时调一次：重置预算 + 报出本批启用的插件。

    run.py 打印一行「本次启用 N 个插件」很有用 —— 否则「插件没生效」和
    「插件启用了但这条船不命中」在日志里长得一模一样，排查全靠猜。
    """
    sb = get_sandbox()
    sb.reset_budget()
    own = conn is None
    conn = conn or _connect()
    try:
        return store.load_runnable(conn)
    finally:
        if own:
            conn.close()


def reset_budget() -> None:
    get_sandbox().reset_budget()


def run_adjudicators(entry: dict, llm_result: dict | None, *, rules=None,
                     user_id: str = "system", source_file: str = "",
                     mmsi: str = "", conn=None) -> list[dict]:
    """对本船跑全部已启用插件。**永不抛异常** —— 失败只记录，不影响主判定。

    返回附加 findings 列表（已带插件归属），供调用方塞进 entry 供前端展示。
    """
    own = conn is None
    conn = conn or _connect()
    try:
        try:
            plugins = store.load_runnable(conn)
        except Exception as e:
            print(f"    ⚠ 读取插件注册表失败（跳过插件判定）: {e}")
            return []
        if not plugins:
            return []

        # facts 组装一次、多个插件共用：几何、抽样、特征归一都只做一遍
        facts = build_facts(entry, llm_result, rules=rules)
        mmsi = mmsi or facts.get("mmsi") or ""
        analysis_time = facts.get("analysis_time") or ""
        sb = get_sandbox()
        collected: list[dict] = []

        for row in plugins:
            try:
                meta = normalize_meta(parse_meta(row.get("manifest_yaml") or ""),
                                      row.get("source_py") or "")
            except Exception as e:
                _fail(conn, row, mmsi, user_id, source_file, analysis_time,
                      "rejected", 0, f"manifest 解析失败：{e}", [])
                continue

            resp = sb.run(
                {"name": row["name"], "source": row.get("source_py") or "",
                 "config": {}, "hooks": meta["hooks"],
                 "timeout_ms": meta["timeout_ms"]},
                facts, hook="adjudicate")

            if not resp.get("ok"):
                stage = resp.get("stage") or "error"
                _fail(conn, row, mmsi, user_id, source_file, analysis_time,
                      _STAGE_TO_RESULT.get(stage, "error"),
                      resp.get("elapsed_ms") or 0, resp.get("error") or "",
                      resp.get("logs") or [])
                continue

            clean, errors = check_findings(resp.get("findings"),
                                          max_findings=meta["max_findings"])
            store.record_run(
                conn, plugin_id=row["id"], plugin_version=row["current_version"],
                mmsi=mmsi, user_id=user_id, source_file=source_file,
                analysis_time=analysis_time, result="ok",
                elapsed_ms=resp.get("elapsed_ms") or 0,
                error_text="; ".join(errors), logs=resp.get("logs") or [],
                findings=clean)
            store.reset_error_streak(conn, row["id"])

            for f in clean:
                item = dict(f)
                item["plugin"] = row["name"]
                item["plugin_label"] = row["label"]
                item["plugin_icon"] = row.get("icon") or "🧩"
                item["plugin_version"] = row["current_version"]
                collected.append(item)

            if clean and row.get("can_raise_risk") and isinstance(llm_result, dict):
                _maybe_raise_risk(conn, row, clean, user_id, mmsi, llm_result)

        return collected
    except Exception as e:
        # 兜底：整个插件层出任何意外都不许影响主判定（对齐 orchestrator 里
        # 「检索失败不影响主流程」的既有取舍）
        print(f"    ⚠ 插件判定层异常（已忽略）: {type(e).__name__}: {e}")
        return []
    finally:
        if own:
            conn.close()


def _fail(conn, row, mmsi, user_id, source_file, analysis_time,
          result, elapsed_ms, error, logs) -> None:
    """记录一次失败，并在连续失败到阈值时自动熔断。"""
    try:
        store.record_run(
            conn, plugin_id=row["id"], plugin_version=row["current_version"],
            mmsi=mmsi, user_id=user_id, source_file=source_file,
            analysis_time=analysis_time, result=result, elapsed_ms=elapsed_ms,
            error_text=error, logs=logs, findings=[])
        streak = store.bump_error_streak(conn, row["id"], error)
    except Exception as e:
        print(f"    ⚠ 插件失败记录写入失败: {e}")
        return
    if streak >= MAX_ERROR_STREAK:
        try:
            store.set_status(conn, row["id"], "suspended", error=error)
            get_sandbox().drop(row["name"])
            _audit("plugin.auto_suspend", user_id,
                   f"插件 {row['name']} 连续失败 {streak} 次，已自动熔断",
                   {"plugin": row["name"], "streak": streak, "error": error[:300]})
            print(f"    ⚠ 插件 {row['name']} 连续失败 {streak} 次，已自动熔断")
        except Exception as e:
            print(f"    ⚠ 插件自动熔断失败: {e}")


def _maybe_raise_risk(conn, row, clean, user_id, mmsi, llm_result: dict) -> None:
    """按插件建议**只升不降**地改 risk_level。

    【为什么是改 llm_result，而不是 UPDATE detection_results】
      run_adjudicators 的调用点在 INSERT 之前（刚 DELETE 过本船旧行），所以那一刻
      库里根本没有本船的结果行，UPDATE 只会打到**上一次运行**的行上 —— 一个
      安静地改错对象的 bug。改内存里的 llm_result，落库时自然带上正确值。

    【为什么只升不降】
      插件是附加判定，没有资格把一个已经由确定性引擎定成 high 的结论降下来。
      允许降就等于让插件可以掩盖违规 —— 那是监管系统里最不能出的错。

    【为什么 score 不跟着改】
      `_risk_score` 是 risk_engine 的确定性打分，不能由插件伪造。所以这里只动
      level，并把原始 level 记进 `_risk_level_before_plugin` —— 页面上要能看出
      「这一条是被人批准过的插件抬上去的」，而不是看起来像引擎自己算出来的。
    """
    best = ""
    for f in clean:
        target = (f.get("raise_risk_to") or "").lower()
        if target in _LEVEL_ORDER and _LEVEL_ORDER[target] > _LEVEL_ORDER.get(best, -1):
            best = target
    if not best:
        return

    current = str(llm_result.get("risk_level") or "").lower()
    if _LEVEL_ORDER.get(best, 0) <= _LEVEL_ORDER.get(current, 0):
        return

    llm_result["_risk_level_before_plugin"] = current or "未定"
    llm_result["risk_level"] = best
    reasons = llm_result.get("_reasons")
    note = (f"插件 {row['name']} 依 can_raise_risk 授权将风险由 "
            f"{current or '未定'} 升至 {best}（引擎打分不变）")
    if isinstance(reasons, list):
        reasons.append(note)
    else:
        llm_result["_reasons"] = [note]

    _audit("plugin.risk_raise", user_id, note,
           {"plugin": row["name"], "mmsi": mmsi, "from": current, "to": best})


def _audit(action: str, user_id: str, detail: str, extra: dict) -> None:
    """写审计。

    注意 audit.log() 的真实签名是 (action, target_type, target_id, detail:dict, ...) ——
    没有 extra 参数。detail 是 dict，`detail` 这个词在这里是人读的一句话，
    塞进 payload 的 "detail" 键，其余字段平铺。
    """
    try:
        import audit
        payload = {"detail": detail}
        payload.update(extra or {})
        audit.log(action, target_type="plugin",
                  target_id=(extra or {}).get("plugin", ""),
                  detail=payload, user_id=user_id)
    except Exception as e:
        print(f"    ⚠ 审计写入失败 {action}: {e}")


def _connect():
    import pg_db
    return pg_db.connect()


def findings_summary(findings: list[dict]) -> str:
    """给日志/前端一行摘要。空列表返回空串。"""
    if not findings:
        return ""
    parts = [f"{f.get('plugin_label') or f.get('plugin')}:{f.get('label') or f.get('code')}"
             for f in findings[:3]]
    if len(findings) > 3:
        parts.append(f"等 {len(findings)} 条")
    return "、".join(parts)


def as_json(findings: list[dict]) -> str:
    try:
        return json.dumps(findings, ensure_ascii=False, default=str)
    except Exception:
        return "[]"
