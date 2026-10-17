# -*- coding: utf-8 -*-
"""注入集上的「谁来判」对照臂：把三级级联逐级拆开，量出质量-成本前沿。

【这份脚本回答的问题】
  论文主张"级联用更低的 LLM 成本达到不劣甚至更好的判定质量"。
  要证明它，必须把**同一批船、同一份真值**交给不同配置去判，
  并且把**LLM 的真实 token 成本**记下来（不是估算）。

【臂（arm）】
  A  l1_only      只跑 L1 确定性规则引擎            —— 0 次 LLM
  B  l2_screen    只跑 L2 LightGBM 风险门控          —— 0 次 LLM
  C  cascade      生产链路 L1 → L2 → L3              —— 只给被门控放行的船调 LLM
  D  raw_llm      原始轨迹（本船 + 邻近船）→ LLM      —— 全部船调 LLM
  E  compress_llm RDP 压缩后的轨迹 → LLM             —— 全部船调 LLM
  F  annotate_llm L1 标注轨迹（事件+特征）→ LLM       —— 全部船调 LLM（跳过 ML 门控）
  G  frugal_llm   FrugalGPT 式两级：先"特征摘要"短提示，
                  置信度不足才升级到全轨迹长提示        —— 1~2 次 LLM/船

【为什么 D 必须带上邻近船】
  违规住在**两船的关系**里（谁该让路、追越距离多少）。只给本船轨迹，
  再强的 LLM 也判不出来——那不是对比，是稻草人。所以 D/E/F 都把本窗口内
  3 海里内的邻近船轨迹一并喂进去。这也正是 L1 的规则在用的信息。

【成本口径】
  · LLM 调用次数（次数）
  · prompt token / completion token —— 取自 Ollama 原生 /api/chat 的
    prompt_eval_count / eval_count，**实测值**，不是字符数除以 4 的估算。
  · 时延按每次调用的墙钟时间累加。

【可续跑】
  每条 (arm, mmsi) 的 LLM 结果落在 data/logs/paper/llm_cache/<arm>.jsonl，
  已算过的直接复用；L1 的标注轨迹落在 paper/l1_inject.json。
  关机重启后重跑同一条命令即从断点继续。

用法：
  python experiments/paper/llm_arms.py                 # 全部臂
  python experiments/paper/llm_arms.py --arms C F G    # 只跑指定臂
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = next(p for p in _HERE.parents if (p / "run.py").exists())
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAPER = ROOT / "data/logs/paper"
CACHE = PAPER / "llm_cache"
GT_PATH = ROOT / "data/logs/inject_v1.gt.json"
CSV_PATH = ROOT / "data/logs/inject_v1.csv"
L1_CACHE = PAPER / "l1_inject.json"

OLLAMA = "http://localhost:11434"
MODEL = "qwen2.5:7b-instruct-q4_K_M"
# 级联实验需要同一份代码跑**两档模型**，只有模型名不同（这是 FrugalGPT 的设定：
# 同一 query 交给不同档位的模型，比较成本-质量）。两个开关：
#   MODEL_TAG —— 非空时缓存写到 llm_cache/<TAG>/，与默认档（qwen2.5:7b）的结果并存，
#                默认空串保持向后兼容，已有 240 条结果不会被覆盖。
#   THINK     —— qwen3 系默认会先生成一整段思维链（实测 completion 297 token vs 47），
#                两档模型的"输出形态"就不同了，成本差变成输出长度的差而不是模型的差。
#                统一关掉，让两档都走"直接作答"模式。
MODEL_TAG = ""
THINK = False

CHAINS = ("give_way_failure", "overtaking_violation")
NEIGHBOR_NM = 3.0          # 把本窗口内 3 海里内的船算作"邻近船"，喂给 LLM
MAX_NEIGHBORS = 3
MAX_PTS = 20               # 单船提示里最多给多少个轨迹点（等间隔抽样）
# 生产提示（F/C 臂）里轨迹点的上限。原值 200 会让提示涨到 8000 token、单次 20 s。
# 只影响"给 LLM 看多细"，不影响判据；所有臂用同一个值即可。
MAX_PTS_PROD = 30


# ── 几何 ──────────────────────────────────────────────────────────
def _hav_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ── 数据 ──────────────────────────────────────────────────────────
# 本地数据的原始格式是 **日/月/年**（"09/10/2022" = 2022-10-09）。
# 写成 月/日/年 会静默解析成 10 月 9 日以外的日子（月份 ≤12 时两种解释都"合法"），
# 所以顺序必须先试 %d/%m/%Y。绝对时刻在本文里只用于"同一窗口内取邻近船"，
# 只要全表一致即可，naive 一律当 UTC。
_TIME_FMTS = ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def load_tracks(path: Path) -> dict[str, list[dict]]:
    """读 7 列 CSV（mmsi,time,lon,lat,course,heading,speed）。"""
    from datetime import datetime, timezone
    out: dict[str, list[dict]] = defaultdict(list)
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            m = (r.get("mmsi") or "").strip()
            if not m:
                continue
            raw = (r.get("time") or "").strip()
            t = None
            for fmt in _TIME_FMTS:
                try:
                    t = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
                    break
                except ValueError:
                    continue
            if t is None:
                continue
            try:
                out[m].append({"t": t, "lat": float(r["lat"]), "lon": float(r["lon"]),
                               "sog": float(r.get("speed") or 0.0),
                               "cog": float(r.get("course") or 0.0)})
            except (KeyError, TypeError, ValueError):
                continue
    for m in out:
        out[m].sort(key=lambda p: p["t"])
    return dict(out)


def _sample(pts: list[dict], n: int = MAX_PTS) -> list[dict]:
    if len(pts) <= n:
        return pts
    step = len(pts) / float(n)
    return [pts[int(i * step)] for i in range(n)]


def neighbors_of(mmsi: str, win: list[dict], tracks: dict[str, list[dict]]) -> list[tuple[str, list[dict]]]:
    """窗口内离本船最近、且曾经进过 NEIGHBOR_NM 的船。"""
    if not win:
        return []
    t0, t1 = win[0]["t"], win[-1]["t"]
    best: dict[str, float] = {}
    for m, pts in tracks.items():
        if m == mmsi:
            continue
        sub = [p for p in pts if t0 <= p["t"] <= t1]
        if len(sub) < 2:
            continue
        d = min(_hav_nm(a["lat"], a["lon"], b["lat"], b["lon"])
                for a in win for b in sub)
        if d <= NEIGHBOR_NM:
            best[m] = d
    picked = sorted(best.items(), key=lambda kv: kv[1])[:MAX_NEIGHBORS]
    return [(m, [p for p in tracks[m] if t0 <= p["t"] <= t1]) for m, _ in picked]


def fmt_track(pts: list[dict], t0: float) -> str:
    rows = []
    for p in _sample(pts):
        rows.append(f"  t+{(p['t'] - t0) / 60.0:6.1f}min  lon={p['lon']:.5f}  lat={p['lat']:.5f}  "
                    f"sog={p['sog']:5.2f}kn  cog={p['cog']:5.1f}deg")
    return "\n".join(rows)


# ── Ollama 原生调用（带 token 实测）────────────────────────────────
def ollama_chat(prompt: str, timeout: int = 300, attempts: int = 6) -> dict:
    payload: dict = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        # num_ctx 必须显式给：Ollama 按显存推的默认上下文只有 4096，
        # 而本任务的提示（本船+3 条邻近船 ×30 点）可到 5~6k token，
        # 超过就会被**静默截断**（不报错，只是模型看到的是半截轨迹）。
        # 统一给 8192，所有臂一致。temperature=0 + 固定 seed → 可复现。
        "options": {"temperature": 0, "seed": 0, "num_predict": 320,
                    "num_ctx": 8192},
    }
    if not THINK:
        # 老版本 Ollama 不认识 think 字段会直接 400，所以只在需要时带。
        payload["think"] = False
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for a in range(attempts):
        try:
            req = urllib.request.Request(
                f"{OLLAMA}/api/chat", data=body,
                headers={"Content-Type": "application/json"})
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            dt = time.perf_counter() - t0
            return {
                "text": (obj.get("message") or {}).get("content", "") or "",
                "prompt_tokens": int(obj.get("prompt_eval_count") or 0),
                "completion_tokens": int(obj.get("eval_count") or 0),
                "latency_s": round(dt, 3),
            }
        except Exception as exc:                                     # noqa: BLE001
            # Ollama 可能正在重启（改并发参数）或被抢占；退避重试，
            # 不要因为一次连接失败就把整条实验打断。
            last = exc
            time.sleep(min(30, 2 ** a))
    raise RuntimeError(f"ollama 调用失败（已重试 {attempts} 次）：{last}")


def parse_verdict(text: str) -> dict:
    """从 LLM 回复里抠出 JSON。抠不出就当"未检测到违规"（保守，不虚增召回）。

    ⚠ 必须容忍**双花括号**：提示里的 JSON 格式示例如果写成 `{{...}}`，
    模型有时会把外层那对花括号原样抄回来。此时 `{...}}` 不是合法 JSON，
    直接解析会失败 —— 失败会被记成「未检测到违规」，于是召回被人为压到接近 0，
    而表面上一切正常（不报错、只是数字难看）。实测踩过一次：
    某一臂 240 条里有 222 条是双花括号，召回被压到 5.8%。
    所以这里把「原样」和「剥一层花括号」两种都试一遍。
    """
    s = text.strip()
    if "```" in s:
        for seg in s.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:]
            if seg.startswith("{"):
                s = seg.strip()
                break
    i, j = s.find("{"), s.rfind("}")
    cands: list[str] = []
    if i >= 0 and j > i:
        cands.append(s[i:j + 1])
    for c in list(cands):
        if "{{" in c or "}}" in c:
            cands.append(c.replace("{{", "{").replace("}}", "}"))
    for c in cands:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        return {
            "violation_detected": bool(obj.get("violation_detected")),
            "violation_type": obj.get("violation_type"),
            "confidence": obj.get("confidence"),
            "risk_level": obj.get("risk_level"),
        }
    return {"violation_detected": False, "violation_type": None,
            "confidence": None, "risk_level": None, "parse_failed": True}


# ── 缓存 ──────────────────────────────────────────────────────────
def cache_path(arm: str) -> Path:
    """`MODEL_TAG` 非空时按模型分目录，避免两档模型的缓存互相覆盖。"""
    d = CACHE / MODEL_TAG if MODEL_TAG else CACHE
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{arm}.jsonl"


def load_cache(arm: str) -> dict[str, dict]:
    p = cache_path(arm)
    out: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    out[str(r["mmsi"])] = r
                except json.JSONDecodeError:
                    continue
    return out


def append_cache(arm: str, rec: dict) -> None:
    with open(cache_path(arm), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── L1 标注轨迹（缓存）────────────────────────────────────────────
def l1_tracks() -> dict[str, dict]:
    if L1_CACHE.exists():
        return json.loads(L1_CACHE.read_text(encoding="utf-8"))
    from ais_accelerator.facade import AISAccelerationFacade
    print(f"[L1] 跑加速器 {CSV_PATH.name} ...", flush=True)
    t0 = time.perf_counter()
    facade = AISAccelerationFacade()
    tracks, metrics = facade.annotate_file(str(CSV_PATH))
    print(f"[L1] {metrics.ship_count} 船 / {metrics.valid_rows:,} 行 / "
          f"{time.perf_counter() - t0:.1f}s", flush=True)
    out: dict[str, dict] = {}
    for t in tracks:
        ai = t.to_agent_input()
        out[str(t.mmsi)] = {
            "feature_vector": list(t.feature_vector or []),
            "agent_input": ai,
            "events": [
                {"type": (e.candidate_type.value if hasattr(e.candidate_type, "value")
                          else str(e.candidate_type)),
                 "start": e.start_time.timestamp(), "end": e.end_time.timestamp(),
                 "features": dict(e.features or {})}
                for e in (t.events or [])
            ],
            "n_annotations": len(t.annotations or []),
        }
    PAPER.mkdir(parents=True, exist_ok=True)
    L1_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[L1] 标注轨迹 → {L1_CACHE}", flush=True)
    return out


# ── 提示词 ────────────────────────────────────────────────────────
RAW_SYS = """你是船舶交通违规判断专家，熟悉《国际海上避碰规则》(COLREG)。
下面给出某船在一个时间窗内的 AIS 轨迹，以及同一窗口内离它最近的邻近船轨迹。
请判断**本船**在这段会遇中是否违反了避碰规则：
  · give_way_failure        = 本船是让路船，但未采取足够的让路动作（保向保速/动作过小），形成紧迫局面
  · overtaking_violation    = 本船在追越他船时未保持安全距离 / 未让清被追越船
  · 都不构成 → violation_detected = false

严格要求：只输出一个 JSON 对象，不要任何解释文字。字段：
{{"violation_detected": true|false, "violation_type": "give_way_failure"|"overtaking_violation"|null, "confidence": 0~1, "reason": "一句话依据"}}
"""

COMPRESS_SYS = """你是船舶交通违规判断专家，熟悉《国际海上避碰规则》(COLREG)。
下面给出某船在一个时间窗内的 AIS 轨迹（**已做 RDP 几何压缩**，只保留形状关键点），
以及同一窗口内邻近船的压缩轨迹。请判断**本船**是否违反避碰规则。
判据同标准定义：give_way_failure（让路船未让路）/ overtaking_violation（违规追越）。
只输出一个 JSON 对象：
{{"violation_detected": true|false, "violation_type": "give_way_failure"|"overtaking_violation"|null, "confidence": 0~1, "reason": "一句话依据"}}
"""

FRUGAL_SHORT_SYS = """你是船舶交通违规判断专家。下面是某船的**运动学特征摘要**（不含完整轨迹）。
请先做一个粗判：这条船有没有可能出现"让路失败"或"违规追越"。
只输出一个 JSON 对象：
{{"violation_detected": true|false, "confidence": 0~1, "reason": "一句话"}}
"""


def build_raw_prompt(mmsi: str, win: list[dict], neigh: list[tuple[str, list[dict]]],
                     compress: bool = False) -> str:
    t0 = win[0]["t"]
    parts = [COMPRESS_SYS if compress else RAW_SYS]
    parts.append(f"\n【窗口时长】{(win[-1]['t'] - t0) / 60.0:.1f} 分钟，共 {len(win)} 个 AIS 点\n")
    parts.append(f"\n【本船 {mmsi} 轨迹】\n{fmt_track(win, t0)}\n")
    for m, pts in neigh:
        parts.append(f"\n【邻近船 {m} 轨迹】\n{fmt_track(pts, t0)}\n")
    if not neigh:
        parts.append("\n【邻近船】本窗口内 3 海里范围内没有其它船。\n")
    parts.append("\n请输出 JSON：")
    return "".join(parts)


GEOM_SYS = """你是船舶交通违规判断专家，熟悉《国际海上避碰规则》(COLREG)。
下面**已经把会遇几何算好了**（不需要你自己从坐标推算）。请依据给定量值做判断。

【判据（本系统的运营标准，与生产规则引擎一致）】
· give_way_failure（让路失败）：本船在交叉相遇中负有让路义务，却未采取足够避让动作，
  与目标船形成紧迫局面。判定要点：
    紧迫局面   = 与目标船的**最近会遇距离 < 0.2 海里**
    已充分避让 = 窗口内**改向 ≥ 30°** 或 **减速 ≥ 2 节**
    → 最近距离 < 0.2 海里 **且** 未达上述避让标准 ⇒ 判「让路失败」
· overtaking_violation（违规追越）：本船从后方追越他船，最近横距 < 0.1 海里
  且未让清被追越船 ⇒ 判「违规追越」
· 不满足上述任一条 ⇒ violation_detected = false

严格要求：只输出一个 JSON 对象，不要任何解释文字。字段：
{{"violation_detected": true|false, "violation_type": "give_way_failure"|"overtaking_violation"|null, "confidence": 0~1, "reason": "一句话依据（引用上面给量值）"}}
"""


def geom_summary(mmsi: str, win: list[dict],
                 neigh: list[tuple[str, list[dict]]]) -> str:
    """把会遇几何算成数字（不丢给模型自己算）。

    这是本臂的全部意义：模型的短板是「从坐标推几何」，不是「按判据下结论」。
    把几何算好、判据列清，剩下的推理它才做得动。
    """
    t0 = win[0]["t"]
    dur = (win[-1]["t"] - t0) / 60.0
    c0 = [p["cog"] for p in win]
    s0 = [p["sog"] for p in win]
    # 净改向：首尾航向差，规范到 (-180, 180]
    d_course = ((c0[-1] - c0[0] + 180.0) % 360.0) - 180.0
    d_speed = max(s0) - min(s0)
    lines = [f"【本船 {mmsi}】",
             f"  窗口时长 {dur:.1f} 分钟，AIS 点数 {len(win)}",
             f"  平均航速 {sum(s0)/len(s0):.2f} 节，最高 {max(s0):.2f} 节",
             f"  窗口内净改向 {d_course:+.1f} 度（已充分避让的门槛：≥ 30 度）",
             f"  窗口内航速变化 {d_speed:.2f} 节（已充分避让的门槛：≥ 2 节）"]
    if not neigh:
        lines.append("  3 海里内没有其它船 → 不存在会遇，不构成会遇类违规")
        lines.append("\n请输出 JSON：")
        return GEOM_SYS + "\n" + "\n".join(lines)

    for m, pts in neigh:
        # 逐点最近距离
        best, bt = None, None
        for a in win:
            for b in pts:
                d = _hav_nm(a["lat"], a["lon"], b["lat"], b["lon"])
                if best is None or d < best:
                    best, bt = d, (a, b)
        a, b = bt
        # 目标船相对本船的方位（相对本船航向；正 = 右舷）
        brg = math.degrees(math.atan2(
            math.sin(math.radians(b["lon"] - a["lon"])) * math.cos(math.radians(b["lat"])),
            math.cos(math.radians(a["lat"])) * math.sin(math.radians(b["lat"]))
            - math.sin(math.radians(a["lat"])) * math.cos(math.radians(b["lat"]))
            * math.cos(math.radians(b["lon"] - a["lon"]))))
        rel = ((brg - a["cog"] + 180.0) % 360.0) - 180.0
        lines.append(f"【邻近船 {m}】")
        lines.append(f"  与本船最近会遇距离 {best:.3f} 海里"
                     f"（紧迫局面门槛 < 0.2）")
        lines.append(f"  最近点时刻 t+{(a['t'] - t0)/60.0:.1f} 分钟，"
                     f"目标相对本船方位 {rel:+.1f} 度（正 = 右舷）")
        lines.append(f"  本船在最近点时航速 {a['sog']:.2f} 节、航向 {a['cog']:.1f} 度；"
                     f"目标船航速 {b['sog']:.2f} 节、航向 {b['cog']:.1f} 度")
    lines.append("\n请依据上述量值判断本船是否违规，并输出 JSON：")
    return GEOM_SYS + "\n" + "\n".join(lines)


RULE_EXEC_SYS = """你是船舶避碰规则判定程序的执行器。
下面**已经把会遇几何算好**，并且**给出了完整的判定过程**。
请**严格按给定的判定过程执行**，不要自行发挥、不要凭经验加减条件。

【判定过程】
第 1 步：在下面列出的邻近船中，找出**最近会遇距离 D 最小**的那一条。
第 2 步：判定本船是否「已充分避让」：
         本船窗口内净改向 ≥ 30 度 **或** 航速变化（最高−最低）≥ 2 节 → 已充分避让 = 是
第 3 步：按顺序判定：
   ① 若 D < 0.2 海里 **且** 已充分避让 ≠ 是  → violation_type = "give_way_failure"
   ② 否则若 D < 0.1 海里 且 该目标的相对方位在 ±67.5 度以内（即正后方扇区）
      → violation_type = "overtaking_violation"
   ③ 否则 → violation_detected = false
第 4 步：输出结论。

严格要求：只输出一个 JSON 对象，不要任何解释文字。字段：
{{"violation_detected": true|false, "violation_type": "give_way_failure"|"overtaking_violation"|null, "confidence": 0~1, "reason": "引用第 1~3 步的量值"}}
"""


def build_rule_exec_prompt(mmsi: str, win: list[dict],
                           neigh: list[tuple[str, list[dict]]]) -> str:
    """把本系统的判据**写成提示**交给 LLM 执行。

    ⚠ 注意这不是「更强的 LLM 基线」，而是**同一条规则的两种实现方式**对照：
       L1 用代码执行这条规则（0 token），这个臂用 LLM 执行同一条规则（要花钱）。
       它回答的是「规则层的价值是知识，还是成本」。
    几何部分直接复用 geom_summary（已经算好），避免两臂在输入上不可比。
    """
    body = geom_summary(mmsi, win, neigh)
    # geom_summary 自带的系统提示换成「执行器」提示，几何量值部分原样保留
    body = body.split("\n", 1)[1] if "\n" in body else body
    return RULE_EXEC_SYS + "\n" + body


def rdp(points: list[dict], eps_deg: float = 1e-4) -> list[dict]:
    """RDP 折线简化（经/纬度平面，容差 eps_deg ≈ 11 m）。"""
    if len(points) < 3:
        return list(points)

    def _perp(p, a, b):
        (x, y), (x1, y1), (x2, y2) = (p["lon"], p["lat"]), (a["lon"], a["lat"]), (b["lon"], b["lat"])
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(x - x1, y - y1)
        return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / math.hypot(dx, dy)

    def _rec(pts):
        if len(pts) < 3:
            return pts
        a, b = pts[0], pts[-1]
        dmax, idx = 0.0, 0
        for i in range(1, len(pts) - 1):
            d = _perp(pts[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps_deg:
            left = _rec(pts[:idx + 1])
            right = _rec(pts[idx:])
            return left[:-1] + right
        return [a, b]

    return _rec(points)


def feature_summary(fv: list[float], events: list[dict]) -> str:
    bits = [f"特征向量维度={len(fv)}"]
    if events:
        bits.append("L1 检出事件: " + "，".join(
            f"{e['type']}({ {k: v for k, v in list(e['features'].items())[:4]} })" for e in events))
    else:
        bits.append("L1 未检出任何候选事件")
    return "\n".join(bits)


# ── 指标 ──────────────────────────────────────────────────────────
def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="*", default=["A", "B", "C", "D", "E", "F", "G"])
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条船（调试用）")
    ap.add_argument("--threads", type=int, default=2,
                    help="并发分片数，应与 Ollama 的 OLLAMA_NUM_PARALLEL 对齐")
    ap.add_argument("--model", default=None,
                    help="覆盖 LLM 模型名（级联实验用它跑第二档模型）")
    ap.add_argument("--tag", default=None,
                    help="缓存子目录名；跑第二档模型时必须给，否则会覆盖第一档的缓存")
    ap.add_argument("--out", default="data/logs/paper/llm_arms.json")
    args = ap.parse_args()

    global MODEL, MODEL_TAG
    if args.model:
        MODEL = args.model
    if args.tag is not None:
        MODEL_TAG = args.tag
    print(f"模型 {MODEL}   缓存目录 llm_cache/{MODEL_TAG or '.'}")

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    scen = {str(s["own_mmsi"]): s for s in gt["scenarios"]}
    mmsis = sorted(scen)
    if args.limit:
        mmsis = mmsis[:args.limit]
    print(f"注入集：{len(mmsis)} 条合成船（正 {sum(1 for m in mmsis if scen[m]['code'])}）")

    tracks = load_tracks(CSV_PATH)
    print(f"轨迹文件：{len(tracks)} 船")
    l1 = l1_tracks()

    # ── L2 路由 ───────────────────────────────────────────────────
    from triage_engine.model import TriageModel
    from triage_engine.router import TriageRouter, RISK_GATE_THRESHOLD
    model = TriageModel(model_path=str(ROOT / "triage_model.txt"))
    router = TriageRouter(model=model)
    print(f"L2：门控阈值 RISK_GATE_THRESHOLD={RISK_GATE_THRESHOLD}")

    # 每个 arm 的船级预测 + 成本
    preds: dict[str, dict[str, bool]] = {a: {} for a in args.arms}
    cost: dict[str, dict] = {a: {"calls": 0, "prompt_tokens": 0,
                                 "completion_tokens": 0, "latency_s": 0.0}
                             for a in args.arms}

    # ── LLMHandler 只为"生产标注提示"渲染 ─────────────────────────
    renderer = None
    if "F" in args.arms or "C" in args.arms:
        try:
            from llm_layer.handler import LLMHandler
            renderer = LLMHandler(model_name=MODEL)
        except Exception as exc:                                    # noqa: BLE001
            print(f"⚠ LLMHandler 不可用（{exc}），F/C 臂将跳过")
            args.arms = [a for a in args.arms if a not in ("F", "C")]

    lock = threading.Lock()

    def call(arm: str, mmsi: str, prompt: str) -> dict:
        """带缓存的一次 LLM 调用。"""
        rec = {"mmsi": mmsi, "arm": arm, "prompt": prompt}
        r = ollama_chat(prompt)
        v = parse_verdict(r["text"])
        rec.update({**v, "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "latency_s": r["latency_s"], "raw": r["text"][:2000]})
        with lock:                    # 多线程追加同一个 jsonl，必须串行写
            append_cache(arm, rec)
        return rec

    def acc(arm: str, rec: dict) -> None:
        with lock:
            c = cost[arm]
            c["calls"] += 1
            c["prompt_tokens"] += int(rec.get("prompt_tokens") or 0)
            c["completion_tokens"] += int(rec.get("completion_tokens") or 0)
            c["latency_s"] += float(rec.get("latency_s") or 0.0)

    t_start = time.time()
    done = {"n": 0}

    def work(shard: list[str]) -> None:
        """一个分片的船。多个分片并发跑 —— 每条船内部 5 次调用是串行的，
        并发只能在**船之间**发生，否则 Ollama 的 NUM_PARALLEL 用不上。"""
        for mmsi in shard:
            one_ship(mmsi)
            with lock:
                done["n"] += 1
                if done["n"] % 10 == 0:
                    el = time.time() - t_start
                    print(f"  [{done['n']}/{len(mmsis)}] {el:.0f}s  " +
                          "  ".join(f"{a}:{cost[a]['calls']}call" for a in args.arms),
                          flush=True)

    def one_ship(mmsi: str) -> None:
        s = scen[mmsi]
        win = tracks.get(mmsi) or []
        if not win:
            for a in args.arms:
                preds[a][mmsi] = False
            return
        neigh = neighbors_of(mmsi, win, tracks)
        info = l1.get(mmsi) or {}
        fv = info.get("feature_vector") or []
        events = info.get("events") or []

        # ── A: L1 规则 ────────────────────────────────────────────
        if "A" in args.arms:
            preds["A"][mmsi] = bool({e["type"] for e in events} & set(CHAINS))

        # ── B: L2 门控 ────────────────────────────────────────────
        route = None
        if "B" in args.arms or "C" in args.arms:
            try:
                d = router.route(feature_vector=fv, mmsi=mmsi)
                route = d.to_dict()
            except Exception:                                       # noqa: BLE001
                route = None
        if "B" in args.arms:
            rs = float((route or {}).get("risk_score") or 0.0)
            preds["B"][mmsi] = rs >= RISK_GATE_THRESHOLD

        # ── C: 生产级联 L1 → L2 → LLM ─────────────────────────────
        if "C" in args.arms:
            l1_hit = bool({e["type"] for e in events} & set(CHAINS))
            needs = bool((route or {}).get("needs_llm"))
            if needs and renderer is not None:
                tmpl = (route or {}).get("llm_prompt_template") or "needs_review"
                from llm_layer.prompts import PROMPT_TEMPLATES
                prompt = renderer._render_prompt(
                    PROMPT_TEMPLATES.get(tmpl, PROMPT_TEMPLATES["needs_review"]),
                    info.get("agent_input") or {}, route or {}, MAX_PTS_PROD)
                cached = load_cache("C").get(mmsi)
                rec = cached if cached else call("C", mmsi, prompt)
                if not cached:
                    acc("C", rec)
                preds["C"][mmsi] = bool(rec.get("violation_detected")) or l1_hit
            else:
                preds["C"][mmsi] = l1_hit

        # ── D: 原始轨迹 → LLM ─────────────────────────────────────
        if "D" in args.arms:
            c = load_cache("D")
            rec = c.get(mmsi) or call("D", mmsi, build_raw_prompt(mmsi, win, neigh, False))
            if mmsi not in c:
                acc("D", rec)
            preds["D"][mmsi] = bool(rec.get("violation_detected"))

        # ── E: RDP 压缩轨迹 → LLM ─────────────────────────────────
        if "E" in args.arms:
            c = load_cache("E")
            if mmsi in c:
                rec = c[mmsi]
            else:
                cw = rdp(win)
                cn = [(m, rdp(pts)) for m, pts in neigh]
                rec = call("E", mmsi, build_raw_prompt(mmsi, cw, cn, True))
            if mmsi not in c:
                acc("E", rec)
            preds["E"][mmsi] = bool(rec.get("violation_detected"))

        # ── F: 标注轨迹 → LLM（跳过 L2 门控）──────────────────────
        if "F" in args.arms and renderer is not None:
            c = load_cache("F")
            if mmsi in c:
                rec = c[mmsi]
            else:
                from llm_layer.prompts import PROMPT_TEMPLATES
                prompt = renderer._render_prompt(
                    PROMPT_TEMPLATES["careful_review"],
                    info.get("agent_input") or {}, {}, MAX_PTS_PROD)
                rec = call("F", mmsi, prompt)
            if mmsi not in c:
                acc("F", rec)
            preds["F"][mmsi] = bool(rec.get("violation_detected"))

        # ── G: FrugalGPT 式两级 ───────────────────────────────────
        if "G" in args.arms:
            c = load_cache("G")
            if mmsi in c:
                rec = c[mmsi]
            else:
                short = FRUGAL_SHORT_SYS + "\n\n【特征摘要】\n" + feature_summary(fv, events) + "\nJSON："
                r1 = ollama_chat(short)
                v1 = parse_verdict(r1["text"])
                conf = v1.get("confidence")
                conf = float(conf) if isinstance(conf, (int, float)) else 0.0
                used_long = False
                r2 = None
                # 置信度落在不确定区间 → 升级到全轨迹长提示
                if not (conf >= 0.9) and not (conf <= 0.1 and not v1["violation_detected"]):
                    used_long = True
                    r2 = ollama_chat(build_raw_prompt(mmsi, win, neigh, False))
                    v2 = parse_verdict(r2["text"])
                else:
                    v2 = v1
                rec = {"mmsi": mmsi, "arm": "G", "used_long": used_long,
                       "violation_detected": bool(v2.get("violation_detected")),
                       "violation_type": v2.get("violation_type"),
                       "confidence": v2.get("confidence"),
                       # 原始回答必须落盘：否则事后无法用新解析器复核，
                       # 一旦解析器有 bug 就再也查不出来（踩过一次）
                       "raw": (r2["text"] if r2 else r1["text"])[:2000],
                       "prompt_tokens": r1["prompt_tokens"] + (r2["prompt_tokens"] if r2 else 0),
                       "completion_tokens": r1["completion_tokens"] + (r2["completion_tokens"] if r2 else 0),
                       "latency_s": r1["latency_s"] + (r2["latency_s"] if r2 else 0.0)}
                with lock:
                    append_cache("G", rec)
            if mmsi not in c:
                acc("G", rec)
            preds["G"][mmsi] = bool(rec.get("violation_detected"))

        # ── H: 算好几何 + 列出判据 → LLM ───────────────────────────
        # 与 D/E 的区别：不再让模型自己从坐标推几何，而是把最近会遇距离、
        # 相对方位、净改向、减速量算好写进提示，并把运营判据列清。
        # 这一臂回答的是「LLM 那一级到底是能力不足，还是信息形态不对」。
        if "H" in args.arms:
            c = load_cache("H")
            if mmsi in c:
                rec = c[mmsi]
            else:
                rec = call("H", mmsi, geom_summary(mmsi, win, neigh))
            if mmsi not in c:
                acc("H", rec)
            preds["H"][mmsi] = bool(rec.get("violation_detected"))

        # ── I: 把判据写进提示交给 LLM 执行（与 L1 同一条规则，不同实现）──
        if "I" in args.arms:
            c = load_cache("I")
            if mmsi in c:
                rec = c[mmsi]
            else:
                rec = call("I", mmsi, build_rule_exec_prompt(mmsi, win, neigh))
            if mmsi not in c:
                acc("I", rec)
            preds["I"][mmsi] = bool(rec.get("violation_detected"))

    # ── 并发跑分片（默认 2，与 Ollama 的 NUM_PARALLEL 对齐）──────────
    nthreads = max(1, args.threads)
    shards = [mmsis[k::nthreads] for k in range(nthreads)]
    print(f"并发 {nthreads} 个分片（每个 {len(shards[0])} 条船）", flush=True)
    ths = [threading.Thread(target=work, args=(sh,), daemon=True) for sh in shards]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    # ── 成本从**缓存全量重算** ────────────────────────────────────
    #
    # 不能在循环里累加：循环里那句 `if mmsi not in c: acc(...)` 只统计了
    # **本次运行新发起的**调用，命中磁盘缓存的那些（上次跑到一半关机留下的）
    # 一次都不计。结果是成本被系统性低估，而质量是全量算的 —— 分子分母不同源，
    # 成本-质量图上就会得出"某个臂又便宜又好"的假象。这里统一从缓存重算。
    for a in args.arms:
        c = load_cache(a)
        cost[a] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                   "latency_s": 0.0}
        for m in mmsis:
            r = c.get(m)
            if not r:
                continue
            cost[a]["calls"] += 1
            cost[a]["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
            cost[a]["completion_tokens"] += int(r.get("completion_tokens") or 0)
            cost[a]["latency_s"] += float(r.get("latency_s") or 0.0)

    # ── 汇总 ──────────────────────────────────────────────────────
    res = {"n_ships": len(mmsis), "arms": {}}
    print("\n" + "=" * 96)
    print(f"{'臂':<16}{'P':>8}{'R':>8}{'F1':>8}{'LLM调用':>9}{'调用率':>8}"
          f"{'prompt tok':>12}{'compl tok':>11}{'时延(s)':>10}")
    print("-" * 96)
    names = {"A": "A L1 规则", "B": "B L2 门控", "C": "C 生产级联",
             "D": "D 原始→LLM", "E": "E 压缩→LLM", "F": "F 标注→LLM",
             "G": "G FrugalGPT", "H": "H 几何+判据→LLM",
             "I": "I 判据写成提示→LLM"}
    for a in args.arms:
        pd = preds[a]
        tp = fp = fn = tn = 0
        for m in mmsis:
            want = scen[m]["code"] is not None
            got = bool(pd.get(m))
            if want and got:
                tp += 1
            elif want and not got:
                fn += 1
            elif not want and got:
                fp += 1
            else:
                tn += 1
        p, r, f = prf(tp, fp, fn)
        c = cost[a]
        res["arms"][a] = {"name": names[a], "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                          "precision": p, "recall": r, "f1": f, **c}
        print(f"{names[a]:<16}{p*100:>7.1f}%{r*100:>7.1f}%{f*100:>7.1f}%"
              f"{c['calls']:>9}{c['calls']/max(1,len(mmsis))*100:>7.1f}%"
              f"{c['prompt_tokens']:>12,}{c['completion_tokens']:>11,}"
              f"{c['latency_s']:>10.0f}")
    outp = ROOT / args.out
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
