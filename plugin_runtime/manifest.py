# -*- coding: utf-8 -*-
"""插件 manifest 解析 —— 与 shipclaw 的 skill.md 同一种写法。

【为什么不用 PyYAML】
  requirements.txt 里没有 PyYAML（本机恰好装了 6.0.3，容器里没有），
  所以这里不能 import yaml。仓库既有的 skill.md（shipclaw/skills.py:822
  _split_frontmatter）就是手写的「key: value」平面解析，本模块沿用同一套格式：
  一个 --- 包住的头部 + 正文。用户看到的是同一种东西，不用学第二套。

【插件文件 = frontmatter + Python 源码，一个文件】
  不拆成 plugin.yaml + plugin.py 两份。理由：一个文件就一次解析、一次校验、
  一次落库、一次版本、一次生成 —— AI 起草时只输出一个 fenced block，
  少一半出错机会。落库时全文存 plugin_versions.source，磁盘预置插件就是
  一个以 --- 开头的 .py 文件。

【hooks 与注册名的约定】
  hooks: adjudicate  ⇔  插件里必须 ctx.tools.register(name="adjudicate", ...)。
  宿主按 hook 名去注册表里找 handler，名字对不上就是「声明了却没注册」，
  静态校验会拒（见 validate.py）。
"""
import re

# ── 取值边界（写在这里，静态校验与运行时都读这一份，避免两处漂移）──
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
ALLOWED_HOOKS = ("adjudicate",)          # 阶段一只开这一个；detect / prompt_hint 留后续
ALLOWED_TRUST = ("sandbox", "trusted")
DEFAULT_TIMEOUT_MS = 1500
HARD_MAX_TIMEOUT_MS = 3000               # 硬顶：manifest 写超了直接拒，不静默截断
DEFAULT_MAX_FINDINGS = 10
HARD_MAX_FINDINGS = 50
MAX_LABEL_LEN = 30
MAX_FACTS_SPEC = "v1"


def split_frontmatter(text: str) -> tuple[str, str]:
    """拆插件文件：返回 (frontmatter 文本, Python 正文)。

    与 skills.py:822 同一手法（split("---", 2)），所以正文里出现 --- 也不会拆错 ——
    最多切两刀，第三段整段是正文。
    """
    if not text or not text.strip().startswith("---"):
        raise ValueError("插件文件必须以 --- 开头的 manifest 段起始（name / label / hooks 等）")
    parts = text.split("---", 2)
    if len(parts) < 3 or not parts[1].strip():
        raise ValueError("缺少或空的 manifest 段，请用一对 --- 包住")
    return parts[1], parts[2].strip()


def parse_meta(fm_text: str) -> dict:
    """解析 manifest 段为 dict（值一律保持字符串，按需再转换）。"""
    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta


def parse_keywords(raw: str) -> list[str]:
    """解析 trigger_keywords：支持逗号 / 中文顿号 / 换行分隔，以及 "- item" 列表。"""
    out = []
    for chunk in re.split(r"[,，、\n]+", raw or ""):
        chunk = chunk.strip()
        if chunk.startswith("- "):
            chunk = chunk[2:].strip()
        if chunk:
            out.append(chunk)
    return out


def _as_bool(raw: str, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")


def _as_int(raw: str, default: int, lo: int, hi: int, field: str) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} 需为 {lo}~{hi} 的整数，收到 {raw!r}")
    if not lo <= n <= hi:
        raise ValueError(f"{field} 需为 {lo}~{hi}，收到 {n}")
    return n


def parse_plugin_file(text: str) -> dict:
    """解析并校验插件文件，返回 {meta: {...规范化后...}, body: Python 正文, raw: 全文}。

    不合法抛 ValueError，消息直接面向用户（会回灌给 AI 起草器，也会显示在开发者页）。
    """
    fm_text, body = split_frontmatter(text)
    return {"meta": normalize_meta(parse_meta(fm_text), body),
            "body": body,
            "raw": text.strip()}


def compose_plugin_file(manifest_yaml: str, body: str) -> str:
    """把 manifest 段与 Python 正文拼回一个插件文件。

    用途：subprocess 里的 runner 只认「一个完整的插件文件」，而库里 manifest 与正文
    是分两列存的（方便页面上分块编辑）。拼回来时统一走这里，别在调用方手写字符串 ——
    `---` 的个数差一个，runner 就会报「缺少 manifest 段」，而那种报错看不出是拼接问题。
    """
    return f"---\n{manifest_yaml.strip()}\n---\n{body.strip()}\n"


def normalize_meta(meta: dict, body: str = "") -> dict:
    """把扁平的 manifest dict 规范化并逐字段校验。

    从 parse_plugin_file 里拆出来，是为了让「库里只存着 manifest_yaml 一列」的
    registry 也能拿到同一套规范化结果 —— 否则字段默认值会在两处各写一遍，
    迟早漂移成「页面上显示 1500ms、实际跑 1000ms」这种查不动的不一致。
    """
    name = (meta.get("name") or "").strip().lower()
    if not NAME_RE.fullmatch(name):
        raise ValueError(
            "name 需为小写字母开头的英文标识（字母/数字/下划线，3~48 字符），如 fishing_zone_judge"
        )

    label = (meta.get("label") or "").strip()
    if not label:
        raise ValueError("label（显示名）必填")
    if len(label) > MAX_LABEL_LEN:
        raise ValueError(f"label 不超过 {MAX_LABEL_LEN} 字，收到 {len(label)} 字")

    description = (meta.get("description") or "").strip()
    if not description:
        raise ValueError("description（一句话说明这个插件判什么）必填")

    icon = (meta.get("icon") or "🧩").strip()[:8] or "🧩"

    spec = (meta.get("spec") or MAX_FACTS_SPEC).strip().lower()
    if spec != MAX_FACTS_SPEC:
        raise ValueError(f"spec 目前只支持 {MAX_FACTS_SPEC}，收到 {spec!r}")

    hooks = parse_keywords(meta.get("hooks") or "adjudicate")
    if not hooks:
        raise ValueError("hooks 至少需要一个（阶段一可用值：adjudicate）")
    bad = [h for h in hooks if h not in ALLOWED_HOOKS]
    if bad:
        raise ValueError(
            f"不支持的 hooks：{', '.join(bad)}（阶段一仅支持 {', '.join(ALLOWED_HOOKS)}）"
        )
    if len(set(hooks)) != len(hooks):
        raise ValueError("hooks 有重复项")

    trust = (meta.get("trust") or "sandbox").strip().lower()
    if trust not in ALLOWED_TRUST:
        raise ValueError(f"trust 需为 {' 或 '.join(ALLOWED_TRUST)}，收到 {trust!r}")

    timeout_ms = _as_int(meta.get("timeout_ms"), DEFAULT_TIMEOUT_MS,
                         100, HARD_MAX_TIMEOUT_MS, "timeout_ms")
    max_findings = _as_int(meta.get("max_findings"), DEFAULT_MAX_FINDINGS,
                           1, HARD_MAX_FINDINGS, "max_findings")
    min_level = _as_int(meta.get("min_level"), 1, 1, 10, "min_level")

    if not (body or "").strip():
        raise ValueError("Python 正文不能为空（需要 def apply(ctx) 与判定函数）")

    return {
        "name": name,
        "label": label,
        "icon": icon,
        "description": description,
        "spec": spec,
        "hooks": hooks,
        "trust": trust,
        "timeout_ms": timeout_ms,
        "max_findings": max_findings,
        "can_raise_risk": _as_bool(meta.get("can_raise_risk"), False),
        "min_level": min_level,
        "trigger_keywords": parse_keywords(meta.get("trigger_keywords") or ""),
    }
