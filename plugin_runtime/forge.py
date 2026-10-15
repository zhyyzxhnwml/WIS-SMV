# -*- coding: utf-8 -*-
"""AI 造插件 —— 用户说「给我做个 XX 判定插件」，这里把它变成一个可试跑的草稿。

【为什么不做成一个 Skill】
  技能池（shipclaw/skills.py）是「匹配到就执行」的，而造插件是**元操作**：
  它的产物会成为技能池里的新成员。放进同一个池子里，用户说「做个禁渔区插件」
  会和「禁渔区怎么判」抢分（两者词面高度重叠），谁赢全靠关键词权重调参，很脆。
  所以 ShipClaw.chat() 在技能匹配**之前**用 detect_forge_intent() 拦截，
  显式、确定性、可测。

【复用 purpose="draft"，不新增用途】
  llm_providers 里 draft 早就声明了（注释写着「阶段 2 接入」），但全仓没有调用方 ——
  它就是为这件事留的槽位。新增一个 forge 用途的代价是：多一处用途绑定要配、
  多一条 use_ollama_fallback 的回落分支、多一个前端下拉项，收益为零。

【重试是必须的，不是保险】
  LLM 生成可执行判定代码，第一次就有语法/白名单/恒真的问题几乎是必然。
  这里最多重试 MAX_RETRIES 次，每次把**静态校验的原始报错**回灌 ——
  尤其要把「恒真检测失败」的原文给回去，否则模型会一遍遍重复同一个错。
  重试仍失败就如实返回失败与原因，不要假装成功（用户宁可见到「AI 第一次没写通」，
  也不愿见到一个悄悄永远返回空列表的插件被启用）。
"""
import json
import re
import traceback

from plugin_runtime.facts import FEATURE_DOC, SAMPLE_FACTS, FACTS_VERSION
from plugin_runtime.manifest import (
    ALLOWED_HOOKS, HARD_MAX_FINDINGS, HARD_MAX_TIMEOUT_MS,
    compose_plugin_file, parse_plugin_file,
)
from plugin_runtime.validate import ValidationError, check_findings, check_source

MAX_RETRIES = 2                 # 首次 + 最多 2 次重写
DRAFT_TIMEOUT_S = 240.0         # 起草调用的超时（比 chat 宽得多：要连吐 YAML + 整段 Python）
DRAFT_MAX_TOKENS = 1500         # 覆盖「起草」用途绑定的 num_predict，理由见 llm_providers.complete
# 重试时回灌的「上一版插件」字符上限。**由上下文预算倒推，不是拍的**：
# 容器 ollama 的 n_ctx=8192（见 docker-compose 的 OLLAMA_CONTEXT_LENGTH），其中
# 系统提示词实测 1736 token、用户需求约 60、校验报错数百、留给模型的输出预算
# DRAFT_MAX_TOKENS=1500 —— 剩下给「上一版」的约 4500 token，代码约 3 字符/token。
# 取 10000 字符（≈3100 token）留足余量，也盖得住真实插件（现有那份 5.9 KB）。
#
# 原值 24000（≈8000 token）**必然超窗**，而 ollama 超窗只保留最前 4 个 token
# （日志原文 `truncating input prompt limit=4098 prompt=12030 keep=4`）—— 它把系统
# 提示词里的输出格式契约整段砍掉，模型失去格式要求后瞎写。于是表现成：第一轮模型
# 手滑写歪 manifest（这本来是重试该修的），重试却因为它自己把提示词撑爆而越修越乱，
# 直到 240 秒超时。**重试这条路从来没通过。**
MAX_DRAFT_CHARS = 10_000

# ── 意图识别关键词。确定性，不用 LLM。 ──
_FORGE_VERBS = ("做", "写", "造", "生成", "新建", "创建", "搞", "加", "来")
_FORGE_NOUNS = ("插件", "plugin")

# 这些词一出现就说明用户是在**问插件**而不是**让造插件**：
# 「插件怎么启用」「有哪些插件」不该触发起草。
_QUESTION_HINTS = ("怎么", "如何", "是什么", "什么是", "哪些", "有没有", "在哪", "为什么",
                   "能不能用", "支持吗", "介绍", "说明")


def detect_forge_intent(message: str) -> bool:
    """判断一句话是不是「让 AI 造插件」。确定性关键词，不调模型。

    判据：出现「插件/plugin」+ 至少一个造物动词。显式命令 `/plugin new` 直接命中。
    否定条件优先 —— 「插件怎么启用」里有「启用」没动词，但「插件怎么做」里有
    「做」和「怎么」，靠 _QUESTION_HINTS 挡掉。

    宁可漏判（用户再说一遍「帮我做个 X 插件」就中了），不可误判 ——
    误判会把一次正常提问变成一次几十秒的模型调用。
    """
    text = (message or "").strip()
    if not text:
        return False
    low = text.lower()
    if low.startswith("/plugin new") or low.startswith("/plugin 新建"):
        return True
    if not any(n in low for n in _FORGE_NOUNS):
        return False
    if any(h in text for h in _QUESTION_HINTS):
        return False
    return any(v in text for v in _FORGE_VERBS)


# ═══════════════════════════════════════════════════════════
# 提示词
# ═══════════════════════════════════════════════════════════

# 注入点用哨兵串，不用 `{...}` —— 模板正文里全是 YAML/Python 代码样例
# （`PLUGIN = {...}`、`DEFAULTS = {...}`、`ctx.config or {}`、正则注释
# `^[a-z][a-z0-9_]{2,47}$`），那些花括号是**给模型看的字面量**。见 system_prompt()。
# 哨兵要**原样写在模板里**（写 `<<FACTS_SCHEMA>>`，不是写 `_S_SCHEMA`）——
# 模板是普通字符串不是 f-string，写变量名只会把名字本身留在正文里。
_S_SCHEMA = "<<FACTS_SCHEMA>>"
_S_DOC = "<<FACTS_DOC>>"

_SYSTEM_PROMPT = '''你是一个 AIS 船舶违规判定系统的插件作者。你要为用户生成一个**可执行**的判定插件。

## 输出格式（严格遵守）
只输出两个 fenced block，先 yaml 后 python，不要任何解释文字：

```yaml
name: my_plugin            # ^[a-z][a-z0-9_]{2,47}$
label: 中文显示名            # 不超过 30 字
icon: 🧩
description: 一句话说明这个插件判什么
spec: v1
hooks: adjudicate
trust: sandbox
timeout_ms: 1200           # 100~3000
max_findings: 5            # 1~50
can_raise_risk: false
trigger_keywords: 关键词1, 关键词2
```

```python
"""插件的文档字符串。"""
import datetime as _dt        # 只能用允许的模块，见下

PLUGIN = {"name": "my_plugin", "entry_tool": "adjudicate"}
DEFAULTS = {"阈值": 1.0}

def apply(ctx):
    cfg = dict(DEFAULTS); cfg.update(ctx.config or {})
    ctx.tools.register(name="adjudicate",
                       description="一句话说明",
                       handler=lambda facts: _judge(facts, cfg))

def _judge(facts, cfg):
    return []
```

## 硬性规则（违反任意一条插件都装不进去）
1. 顶层必须有 `PLUGIN = {...}` 和 `def apply(ctx)`；`apply` 恰好一个位置参数。
2. `hooks` 里声明的每个名字，都必须在 `apply()` 内用 `ctx.tools.register(name="...")` 注册。
   阶段一只支持 `adjudicate`。
3. 模块级除了 import / def / `PLUGIN = {...}` / 常量赋值，**不许有别的语句**
   （尤其不许在模块级调用函数）。文档字符串可以有。
4. 只能 import 这些模块：math, statistics, decimal, json, re, datetime,
   collections, itertools, functools, typing, dataclasses, enum。**别的都不行**
   （不许 os / sys / pathlib / requests / numpy / pandas）。
5. 不许使用这些名字：eval, exec, compile, open, input, globals, locals, vars,
   getattr, setattr, delattr, __import__, breakpoint, memoryview, exit, quit, help,
   object, type, super。不许访问任何 `__xxx__` 形式的属性。不许 global / nonlocal。
6. 不许 `while True`。不要写耗时超过 1 秒的逻辑。

## handler 的输入 facts（只读，结构固定）
<<FACTS_SCHEMA>>

<<FACTS_DOC>>

## handler 的返回值
返回 `list[dict]`，**没命中就返回空列表 `[]`**。每条 finding：
```python
{"code": "英文码，如 fishing_in_restricted_zone",
  "label": "中文短标签",
  "confidence": 0.0~1.0,          # 命中把握
  "severity": "low|mid|high",
  "evidence": "把关键数字写进来，例如：区内低速作业 42 分钟，区内点 37，低速 29",
  "metric": 42.0, "metric_unit": "分钟",
  "span": ["2026-01-01T10:00:00", "2026-01-01T10:42:00"],
  "regulation_ref": "《渔业法》相关条款"}
```

## 最重要的一条
**不要写出恒真插件。** 判定条件必须真的用到数据：区域要判 bbox 命中、时长要算时间差、
速度要比阈值。如果数据不足以支撑结论，就返回 `[]`。
只写你确定存在的字段 —— 上面没列出的字段一律视为不存在。
'''


def build_prompt(user_request: str, existing: str = "", errors: list[str] | None = None) -> str:
    """拼用户消息。errors 非空时是重试轮：把上一版的校验报错原样回灌。"""
    parts = [f"请生成一个判定插件。用户的需求是：\n{user_request.strip()}"]
    if existing:
        clipped = existing[:MAX_DRAFT_CHARS]
        parts.append("## 上一版插件（请在此基础上修正，不要推倒重写）\n"
                     f"```\n{clipped}\n```")
        if len(existing) > MAX_DRAFT_CHARS:
            # 截断要**说出来**。默默切一刀的话，模型会以为看到的是一份完整文件，
            # 照着残缺的结尾往下写，越修越乱 —— 这和超窗被 ollama 偷偷砍头是同一类错。
            parts.append(f"（注意：上一版共 {len(existing)} 字符，上面只给了前 "
                         f"{MAX_DRAFT_CHARS} 字符，结尾是断的 —— 缺失的部分按需求重写，"
                         "不要照抄断口）")
    if errors:
        parts.append("## 上一版没通过校验，错误如下（必须逐条修掉）\n"
                     + "\n".join(f"- {e}" for e in errors))
        parts.append("特别注意：如果错误里提到「不该命中的样本返回了结果」，"
                     "说明你的判定条件太松或写成了恒真，请把条件收紧扣住真实字段。")
    return "\n\n".join(parts)


def system_prompt() -> str:
    """把 facts 契约渲染进系统提示。

    FEATURE_DOC 必须原样嵌进来 —— 模型凭空引用不存在的特征名是本项目踩过的坑
    （方案初稿写过 in_anchorage / duration_hours，两个都不存在，插件会静默返回空）。

    **这里用 replace，不用 str.format。** 模板正文带着 YAML / Python 代码样例
    （`PLUGIN = {...}`、`DEFAULTS = {...}`、`ctx.config or {}`、
    `# ^[a-z][a-z0-9_]{2,47}$` 这个正则注释），那些花括号是**给模型看的字面量**，
    不是占位符。先前用 `.format()` 时它们被当成字段名，撞上第一个 `{2,47}` 就抛
    `KeyError: '2,47'` —— 起草从来没走到模型那一步，每次点都是这个错。
    现在占位符换成哨兵串（`_S_SCHEMA` / `_S_DOC`），示例代码以后怎么改都不会再撞上。
    """
    out = (_SYSTEM_PROMPT
           .replace(_S_SCHEMA, _facts_schema_text())
           .replace(_S_DOC, FEATURE_DOC))
    # 双向自检：占位符既不能**剩下**（那说明 replace 没命中，正文里会留一坨哨兵），
    # 注入内容也必须**真的到了**（那说明模板被改、占位符丢了但没人发现）。
    # 少了这一条，提示词会静默变残 —— 模型看不到字段清单就开始编字段名。
    for tok, injected in ((_S_SCHEMA, _facts_schema_text()), (_S_DOC, FEATURE_DOC)):
        if tok in out:
            raise RuntimeError(f"插件提示词模板里的占位符 {tok} 没被替换掉")
        if injected[:40] and injected[:40] not in out:
            raise RuntimeError(f"插件提示词模板缺少占位符 {tok}（注入内容没进正文）")
    return out


def _facts_schema_text() -> str:
    return f"""```
{{
  "facts_version": "{FACTS_VERSION}",
  "mmsi": "413000000", "source_file": "...", "analysis_time": "...",
  "quality":  {{"level": "good|fair|poor", "invalid_points": 3, "duplicate_points": 0}},
  "track":    {{"n_points": 812, "duration_min": 47.3, "avg_speed": 5.2, "max_speed": 12.4,
               "points": [{{"ts": "2026-01-01T10:00:00", "lon": 120.1, "lat": 30.1,
                          "sog": 2.0, "cog": 90.0}}, ...]}},   # 最多 200 点
  "features": {{ ...见下方特征清单... }},
  "l1_events": [{{"candidate_type": "...", "start_ts": "...", "end_ts": "...",
                 "features": {{...}}}}],
  "l2":       {{"ml_category": "...", "ml_confidence": 0.83, "routing": "...", "probs": {{}}}},
  "zones":    [{{"id": "...", "name": "...", "restrictions": {{...}}, "bbox": [x0,y0,x1,y1]}}],
  "regulations": [{{"code": "...", "title": "...", "ref": "..."}}],
  "base":     {{"violation_detected": 1, "violation_code": "ais_gap",
               "risk_level": "mid", "confidence": 0.62, "assessment": "..."}}
}}
```

说明：
- `track.points[].ts` 是 `YYYY-MM-DDTHH:MM:SS` 字符串；`sog` 是节，`cog` 是度。
- `zones[].bbox` 是宿主**预先算好的** `[min_lon, min_lat, max_lon, max_lat]`，
  插件只做 `bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]` 这种区间比较，
  不要自己做几何运算（没有 shapely，也不要引入）。
- `zones[].restrictions` 里的键值表示该区域限制什么，例如
  `{{"fishing_allowed": false}}` / `{{"entry_allowed": false}}`。
- `base` 是系统的确定性基线结论，**你只能读，不能改**。"""


# ═══════════════════════════════════════════════════════════
# 生成
# ═══════════════════════════════════════════════════════════

_YAML_BLOCK = re.compile(r"```(?:yaml|yml)\s*\n(.*?)```", re.S | re.I)
_PY_BLOCK = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.S | re.I)


class ForgeError(Exception):
    """起草失败。消息面向用户，直接显示在对话里。"""


def extract_blocks(text: str) -> tuple[str, str]:
    """从模型输出里取 (manifest 段, python 正文)。

    容错：模型经常只给一个 python block，或者把 manifest 直接写在 python 里。
    没有 yaml block 时，从 python block 里找模块级 PLUGIN 字典反推 name ——
    总比直接报「格式不对」让用户重说一遍好。
    """
    text = text or ""
    py_m = _PY_BLOCK.search(text)
    body = (py_m.group(1) if py_m else text).strip()
    yaml_m = _YAML_BLOCK.search(text)
    fm = (yaml_m.group(1) if yaml_m else "").strip()
    if not fm:
        # 退路：从正文里的 PLUGIN 字典取 name，其余字段留空由 normalize_meta 报错
        m = re.search(r"""["']name["']\s*:\s*["']([a-z][a-z0-9_]*)["']""", body)
        fm = f"name: {m.group(1)}\n" if m else ""
    if not body:
        raise ForgeError("模型没有返回可用的 Python 代码块")
    return fm, body


def generate(user_request: str, *, existing: str = "",
             errors: list[str] | None = None) -> tuple[str, str]:
    """调 LLM 生成一份插件文件。返回 (插件文件全文, 错误的 manifest 段)。

    调用方拿到的是「一个完整的插件文件」（frontmatter + 正文）—— 与磁盘预置插件、
    与 plugin_versions.source_py 存的东西完全同构，后面走同一条解析/校验/落库路径。
    """
    try:
        import llm_providers
    except Exception as e:
        raise ForgeError(f"模型服务不可用：{e}")

    prompt = build_prompt(user_request, existing, errors)
    text, err = llm_providers.complete(prompt, system_prompt(),
                                       purpose="draft", timeout=DRAFT_TIMEOUT_S,
                                       max_tokens=DRAFT_MAX_TOKENS)
    if err:
        raise ForgeError(_model_error_hint(err))
    if not text.strip():
        raise ForgeError("模型返回了空内容（一个字符都没有）")

    # 先判截断，再判格式 —— 截断的典型样子是 YAML 块完整、Python 块少了收尾的
    # ```，于是 extract_blocks 取不到 py 块、把整段文本当正文，最后在静态校验里
    # 报一句莫名其妙的语法错。在这里点明「是被截断了」，用户才知道该换模型，
    # 而不是以为自己的需求写错了。
    fences = text.count("```")
    if fences % 2:
        raise ForgeError(
            f"模型输出被截断了：代码块的 ``` 只出现 {fences} 次（有个块没闭合），"
            f"共收到 {len(text)} 字符。多半是输出预算用尽 —— "
            "请把「起草」用途绑到输出更长的模型")

    fm, body = extract_blocks(text)
    if not fm:
        raise ForgeError(
            f"模型输出里找不到 manifest 段（name/label/hooks）—— "
            f"它没按「先 yaml 块、后 python 块」的格式回，实际回了 {len(text)} 字符")
    return compose_plugin_file(fm, body), fm


def _model_error_hint(err: str) -> str:
    """把底层报错翻成一句用户能照着做的说明。

    先前这里写的是「（用途「起草」绑定未配置？）」—— 一个**猜**，而且常常猜错：
    绑定明明配着，真实原因是超时。
    """
    low = (err or "").lower()
    if "timed out" in low or "timeout" in low:
        return (f"模型 {DRAFT_TIMEOUT_S:.0f} 秒内没能写完这个插件（{err}）。"
                "本地小模型起草往往就是这个量级 —— 到「开发者 → 提供商」"
                "把「起草」用途绑到云端模型（如 DeepSeek）会快得多。")
    if "connect" in low or "refused" in low or "unreachable" in low:
        return (f"连不上「起草」用途绑定的提供商（{err}）—— "
                "到「开发者 → 提供商」核对服务地址与密钥。")
    return f"模型调用失败：{err}"


# ═══════════════════════════════════════════════════════════
# 动态试跑
# ═══════════════════════════════════════════════════════════

def trial(plugin_text: str, facts_bundle: list[dict] | None = None) -> dict:
    """在子进程里拿内置样本跑一遍，返回 {ok, errors, results}。

    【为什么必须有一份「不该命中」的样本】
      最常见的 LLM 生成缺陷是无脑 `return [finding]` —— 它语法没错、白名单也没错，
      静态校验完全看不出来，启用后每条船都会被它判成违规。用「明显不该命中」的
      样本一跑就现形。这条比其它所有检查都值钱。
    """
    from plugin_runtime.sandbox import get_sandbox

    # runner 会核对「请求里的 name」与「manifest 里的 name」是否一致（防串号），
    # 所以这里必须用插件自己的名字，不能随便给个占位名。
    try:
        meta = parse_plugin_file(plugin_text)["meta"]
    except ValueError as e:
        return {"ok": False, "errors": [f"manifest 不合法：{e}"], "results": []}

    samples = facts_bundle or SAMPLE_FACTS
    sb = get_sandbox()
    errors: list[str] = []
    results: list[dict] = []

    for sample in samples:
        expect = sample.get("_expect", "any")
        facts = {k: v for k, v in sample.items() if not k.startswith("_")}
        resp = sb.run({"name": meta["name"], "source": plugin_text, "config": {},
                       "hooks": list(ALLOWED_HOOKS),
                       "timeout_ms": min(HARD_MAX_TIMEOUT_MS, meta["timeout_ms"])},
                      facts, hook="adjudicate")
        entry = {"expect": expect, "ok": bool(resp.get("ok")),
                 "n": 0, "error": resp.get("error") or "", "stage": resp.get("stage") or ""}
        if resp.get("ok"):
            clean, ferr = check_findings(resp.get("findings"), max_findings=HARD_MAX_FINDINGS)
            entry["n"] = len(clean)
            entry["codes"] = [f["code"] for f in clean]
            errors.extend(ferr)
            if expect == "none" and clean:
                errors.append(
                    f"【恒真检测】样本「{sample.get('_name')}」明显不该命中，"
                    f"插件却返回了 {len(clean)} 条（{entry['codes']}）—— "
                    "判定条件太松或写成了恒真，必须收紧到真正用到数据")
            if expect == "hit" and not clean:
                # 不是硬错误：AI 的阈值取得比样本更严是允许的，只提醒
                entry["note"] = "该命中的样本没命中（阈值可能偏严）"
        else:
            errors.append(f"样本「{sample.get('_name')}」执行失败"
                          f"（{entry['stage']}）：{entry['error']}")
        results.append(entry)
    return {"ok": not errors, "errors": errors, "results": results}


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def forge_plugin(user_id: str, request_text: str, *, username: str = "",
                 conn=None) -> dict:
    """造一个插件草稿。**不启用** —— 启用是超管的动作（plugin.publish）。

    返回：
      {"ok": True,  "plugin_id": 7, "name": ..., "version": 1, "label": ...,
       "validation": {...}, "trial": {...}, "attempts": 2, "model": "..."}
      {"ok": False, "error": "面向用户的原因", "attempts": n, "report": [...]}
    """
    import hashlib

    from plugin_runtime import store

    own = conn is None
    if own:
        import pg_db
        conn = pg_db.connect()
    try:
        report: list[dict] = []
        existing = ""
        errors: list[str] | None = None
        plugin_text = ""
        meta: dict = {}
        stats: dict = {}
        attempt = 0

        for attempt in range(1, MAX_RETRIES + 2):
            try:
                plugin_text, _fm = generate(request_text, existing=existing, errors=errors)
            except ForgeError as e:
                return {"ok": False, "kind": "model", "error": str(e),
                        "attempts": attempt, "report": report}

            # ① 解析 manifest
            try:
                parsed = parse_plugin_file(plugin_text)
            except ValueError as e:
                errors = [f"manifest 不合法：{e}"]
                existing, report = plugin_text, report + [
                    {"attempt": attempt, "stage": "manifest", "errors": errors}]
                continue

            # ② 静态校验
            try:
                stats = check_source(parsed["body"], parsed["meta"]["hooks"])
            except ValidationError as e:
                errors = [str(e)]
                existing, report = plugin_text, report + [
                    {"attempt": attempt, "stage": "validate", "errors": errors}]
                continue

            # ③ 动态试跑（含恒真检测）
            tr = trial(plugin_text)
            report.append({"attempt": attempt, "stage": "trial",
                           "errors": tr["errors"], "results": tr["results"]})
            if tr["ok"]:
                meta = parsed["meta"]
                break
            errors, existing = tr["errors"], plugin_text
        else:
            return {"ok": False, "kind": "validation", "attempts": attempt,
                    "report": report,
                    "error": f"生成 {attempt} 次都没通过校验，最后一次的问题："
                             + "；".join(errors or ["未知"])}

        # ④ 落库为 draft。重名直接拒绝 —— 不自动改名，用户要的是这个名字。
        if store.get_plugin_by_name(conn, meta["name"]):
            return {"ok": False, "kind": "duplicate", "attempts": attempt,
                    "report": report,
                    "error": f"已存在同名插件 {meta['name']}，请换个名字或直接编辑它"}

        model = _current_draft_model()
        pid = store.create_plugin(
            conn, name=meta["name"], label=meta["label"], icon=meta["icon"],
            description=meta["description"], origin="ai", trust=meta["trust"],
            plugin_text=plugin_text,
            source_sha256=hashlib.sha256(plugin_text.encode("utf-8")).hexdigest(),
            validation_report=json.dumps({"ok": True, **stats}, ensure_ascii=False),
            author_user_id=user_id, author_username=username, created_by_model=model,
            can_raise_risk=False,          # AI 生成的插件一律不许抬风险，要抬由超管另开
            trigger_keywords=",".join(meta["trigger_keywords"]),
            min_level=meta["min_level"],
        )
        _audit("plugin.ai_draft", user_id,
               f"AI 起草插件 {meta['name']}（{meta['label']}），{attempt} 次通过",
               {"plugin": meta["name"], "plugin_id": pid, "attempts": attempt,
                "model": model, "request": (request_text or "")[:300]})
        return {"ok": True, "plugin_id": pid, "name": meta["name"],
                "label": meta["label"], "icon": meta["icon"], "version": 1,
                "attempts": attempt, "model": model,
                "validation": stats, "trial": report[-1].get("results", []),
                "report": report,
                "hint": "草稿已保存，等待超级管理员在「开发者 → 插件」里试跑并启用"}
    except Exception as e:
        # 内部错误 —— 必须和「模型写不过校验」区分开。先前两种情况共用一个
        # 面向用户的说法，于是 KeyError 这种系统故障被包装成「你的需求讲得不够具体，
        # 换个说法再试」，把用户支去反复改一个根本无关的东西。
        # 完整栈写进服务端日志（stderr），只把一句「这是系统故障」回给用户。
        traceback.print_exc()
        return {"ok": False, "kind": "internal",
                "error": f"起草过程出错：{type(e).__name__}: {e}",
                "attempts": 1, "report": []}
    finally:
        if own and conn is not None:
            conn.close()


def _current_draft_model() -> str:
    """当前「起草」用途绑的模型名，落进 created_by_model 供追溯。

    取不到不算错 —— 插件照样能造，只是这一栏空着。
    """
    try:
        import llm_providers
        r = llm_providers.resolve("draft")
        return f"{r.get('provider_name') or r.get('kind') or ''}/{r.get('model') or ''}"[:120]
    except Exception:
        return ""


def _audit(action: str, user_id: str, detail: str, extra: dict) -> None:
    try:
        import audit
        payload = {"detail": detail}
        payload.update(extra or {})
        audit.log(action, target_type="plugin",
                  target_id=(extra or {}).get("plugin", ""),
                  detail=payload, user_id=user_id)
    except Exception as e:
        print(f"    ⚠ 审计写入失败 {action}: {e}")
