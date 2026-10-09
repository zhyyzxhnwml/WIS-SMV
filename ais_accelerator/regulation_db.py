"""
ais_accelerator/regulation_db.py — 法规向量数据库
===================================================

【这个文件做什么？】
  将 regulations.json 和 rule/ 目录下的法规 PDF 解析为结构化条目，
  向量化后存入 Milvus，提供语义检索能力。

【核心功能】
  1. 法规条目解析：从 JSON 和 PDF 中提取法规条目（编号/标题/正文/来源/罚则）
  2. 向量化：使用 Ollama embedding API 将法规文本转为向量
  3. 语义检索：根据违规场景描述，检索最相关的法规条目
  4. 精确匹配：cascade 检测到违规时，自动关联具体法规条款

【Milvus 集合 regulation_articles】
  - article_id: 法规条目唯一 ID（如 "海上交通安全法_第35条"）
  - source: 法规来源（如 "中华人民共和国海上交通安全法2021"）
  - article_number: 条号（如 "第三十五条"）
  - title: 条目标题/摘要
  - content: 条目正文
  - penalty: 罚则（如有）
  - violation_codes: 关联的违规类型代码列表（JSON）
  - content_vector: 文本嵌入向量（由 Ollama 生成）
  - keywords: 关键词列表（JSON）

【设计原则】
  - 法规条目是"只读"的，初始化时一次性导入，运行时只查询
  - 向量化用 Ollama 本地模型，不依赖外部 API
  - 与现有 MilvusClient 共享同一个 milvus_lite.db，不同集合
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from pymilvus import MilvusClient as MilvusLiteClient
    from pymilvus import DataType
    HAS_MILVUS = True
except ImportError:
    HAS_MILVUS = False

try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False


# ── 常量 ──

REGULATION_COLLECTION = "regulation_articles"
# 用真正的 embedding 模型。qwen2.5:7b-instruct 不支持 embeddings（会 500 报错并
# 回退到哈希伪向量，导致相似度检索完全失效）。nomic-embed-text 是 Ollama 里
# 已装的专用 embedding 模型，768 维；集合是 1024 维，不足部分补 0（余弦不受影响）。
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 1024  # Milvus 集合向量维度（既有集合固定为 1024）

# 法规检索相似度下限：低于它的结果**丢弃**，而不是当成"检索到了"写进 regulation_ref。
# 这是工程下限、不是法规阈值。
#
# ⚠⚠ 关键实测结论（2026-10-09，语料 283 条 / COLREG + 海安法 + 渔港条例 + 福州港 + 防台风指南，
# 复现：data/logs/_run/similarity_recalib.py）：**这个分数分不开"相关"与"不相关"**。
#
#   相关对 top-1 余弦：0.7652(对遇↔COLREG14，命中)   0.8270(交叉↔COLREG15，命中)
#                      0.8133(渔港捕捞↔渔港条例10，命中)
#                      0.7684(AIS中断↔海安法36，top 却是 COLREG37)
#                      0.7512(禁航区↔海安法44，top 却是海安法57)
#                      0.8511(超速↔海安法43，top 却是防台风指南6.1)
#   不相关对 top-1 余弦：0.7265(船员薪酬) 0.7621(台风防台) 0.7717(货运单证)
#
# 两组**重叠**（相关的最低 0.7512 < 不相关的最高 0.7717）：不相关的货运单证(0.7717)
# 比相关的禁航区(0.7512)、AIS中断(0.7684) 都高。所以任何单一阈值都不可能既留下
# "禁航区"又滤掉"货运单证"——nomic 在短中文同域文本上的基线本身就高。
#
# ⚠ 2026-10-09 取文改版（COLREG 去掉英文栏）后新出现的一个副作用，要记在这里：
#   条目正文变短、变"纯中文"之后，在**纯向量**空间里反而更容易压过正确的海安法条目 ——
#   上面三次"top 跑偏"全是这个原因（旧的 192 条语料里，"对遇"top-1 只有 0.583，
#   现在同类查询普遍 0.75+）。短的中文条文对任何中文海事查询都有很高的基线相似度。
#   生产路径不受影响（见下），但**不要**因为"分数变高了"就以为检索变好了。
#
# 结论：**不要把这个下限当相关性判据**。它的真实作用只有一个 —— 砍掉分布最尾部
# 的噪声（自命中是 1.0000，尾部噪声实测 ≤0.5 区间），是个很弱的兜底。
# 真正决定"检索到哪条法"的是另外两样东西：
#   1. violation_catalog.REG_ANCHORS 锚定表（11/11 命中，人工确认过的条号；
#      2026-10-09 复测：11/11 取到非空正文）
#   2. search() 的 violation_code 字段过滤
# 锚定命中时**直接返回、不受本下限约束**（所以第14条 0.583 照样能出来）。
# MIN_SIMILARITY 只在两步都落空、退到纯向量检索时才生效。
MIN_SIMILARITY = 0.60

# 法规条目解析的正则模式。
#
# ARTICLE_PATTERN_ANCHORED 是**实际使用**的：条目开头必须在行首。
# 为什么必须加这个约束：正文里到处是交叉引用（"应鸣放第三十四条3款规定的声号"
# "依照第九条的规定"），不限位置切分会把它们也当成新条目的开头 —— 同一个条号被
# 切出好几份。实测 COLREG 一本不限位置切出 73 条（真值 38），海上交通安全法也多出 3 条。
#
# 判定标准（可复现）：正确的解析应当让每个来源的条号构成**连续无缺口的 1..N**。
# 加行首约束后，四个来源的条号全部连续无缺口。
# 下表是当初定"行首约束"时的实测（原始取文、未容忍空白）；COLREG 那行的行首数
# 后来因为下面的空白容忍从 37 变成 40（多出来的仍是交叉引用，由 dedupe 收掉）：
#   来源        不限位置   行首
#   COLREG        72      37（2..38，缺第1条见下）
#   海安法        125     122（1..122）
#   渔港条例       30      29（1..29）
#   福州港章程     44      44（1..44）
#
# ⚠ COLREG 第1条 曾经整条收不到，而当时把原因判错了：不是"标题不在行首"（它就在行首），
#   而是**这一条是散排的** —— 全书标题写作「第二条」「第三条」，唯独第1条写成
#   「第 一 条」（第 / 一 / 条 之间有空格），旧模式匹配不上，于是静默跳过。
#   现在允许空白，COLREG 条号成为连续的 1..38。
#   （另一个**独立**缺陷是双栏中英对照的取文顺序，见 extract_pdf_text）
# 第 / 数字 / 条 之间的空白必须容忍：这不是理论风险，是上面实测踩到的那一次。
_SP = r"[ \t\xa0]*"
ARTICLE_PATTERN_ANCHORED = re.compile(
    rf"(?:^|\n)[ \t\xa0]*第{_SP}([一二三四五六七八九十百千零\d]+){_SP}条{_SP}"
    rf"(.*?)(?=(?:^|\n)[ \t\xa0]*第{_SP}[一二三四五六七八九十百千零\d]+{_SP}条|$)",
    re.DOTALL,
)

# 兜底用：不限位置切分。仅当行首规则一条都切不出来时回退（见 parse_pdf_text）。
ARTICLE_PATTERN = re.compile(
    rf"第{_SP}([一二三四五六七八九十百千零\d]+){_SP}条{_SP}"
    rf"(.*?)(?=第{_SP}[一二三四五六七八九十百千零\d]+{_SP}条|$)",
    re.DOTALL,
)

# ── 另一种编号体例：小数编号（指南/标准/办法常用）1. / 1.1 / 4.1.1 ──
# 「船舶防台风指南（试行）」这类文件整篇没有「第X条」，用 ARTICLE_PATTERN 解析出 0 条。
# 拿不到「第X条」不等于没有条款 —— 只是换了编号体例。
#
# 首段限 1~2 位是**必须的过滤**：放开会命中行首的普通数字，例如「24 小时值班」
# 「2000 吨以上」会被当成"第24项""第2000项"，切出一堆假条目。文档的顶层章节数
# 不会超过两位数，1~2 位足够，且能挡掉这类误命中。
NUMBERED_ITEM_PATTERN = re.compile(
    r"^[ \t]*(\d{1,2}(?:\.\d{1,2})*)[ \t]*[、.．]?[ \t]*",
    re.MULTILINE,
)

# 页脚页码形如「— 1 —」，先摘掉，否则会混进正文
PAGE_FOOTER_PATTERN = re.compile(r"—\s*\d+\s*—")

# 纯标题项（如「1.依据和适用范围」「2.一般要求」）：正文很短且没有句号 ——
# 当作章节上下文，不产出条目。阈值 12 是实测出来的：真标题「依据」2 字、
# 「一般要求」4 字；而最短的真条款正文「船舶防台风工作遵循"安全第一…"」有 37 字。
_HEADING_MAX_LEN = 12

# 中文数字转阿拉伯数字
CN_NUM_MAP = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
}


def cn_to_int(cn: str) -> int:
    """将中文数字转为阿拉伯数字。如 '三十五' → 35。"""
    if cn.isdigit():
        return int(cn)
    result = 0
    current = 0
    for ch in cn:
        if ch in CN_NUM_MAP:
            val = CN_NUM_MAP[ch]
            if val >= 10:
                if current == 0:
                    current = 1
                result += current * val
                current = 0
            else:
                current = val
        else:
            continue
    result += current
    return result


# ── 法规条目数据结构 ──

class RegulationArticle:
    """一条法规条目。"""

    def __init__(
        self,
        article_id: str,
        source: str,
        article_number: str,
        title: str,
        content: str,
        penalty: str = "",
        violation_codes: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        chapter: str = "",
        priority: int = 9,
        doc_type: str = "其他",
        region: str = "未知",
    ):
        self.article_id = article_id
        self.source = source
        self.article_number = article_number
        self.title = title
        self.content = content
        self.penalty = penalty
        self.violation_codes = violation_codes or []
        self.keywords = keywords or []
        self.chapter = chapter
        # 效力层级/文件性质/适用地域 —— 由 violation_catalog.doc_meta() 按 source 盖章，
        # 解析器本身不猜（见 apply_doc_meta）
        self.priority = priority
        self.doc_type = doc_type
        self.region = region

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "source": self.source,
            "article_number": self.article_number,
            "title": self.title,
            "content": self.content,
            "penalty": self.penalty,
            "violation_codes": json.dumps(self.violation_codes, ensure_ascii=False),
            "keywords": json.dumps(self.keywords, ensure_ascii=False),
            "chapter": self.chapter,
            "priority": int(self.priority),
            "doc_type": self.doc_type,
            "region": self.region,
        }

    def embedding_text(self) -> str:
        """用于向量化的文本（标题 + 正文 + 关键词）。"""
        parts = []
        if self.chapter:
            parts.append(f"【{self.chapter}】")
        parts.append(f"第{self.article_number}条 {self.title}")
        parts.append(self.content)
        if self.penalty:
            parts.append(f"罚则：{self.penalty}")
        if self.keywords:
            parts.append("关键词：" + "、".join(self.keywords))
        return "\n".join(parts)


# ── 法规解析器 ──

class RegulationParser:
    """从各种来源解析法规条目。"""

    @staticmethod
    def parse_regulations_json(path: Path) -> list[RegulationArticle]:
        """从 regulations.json 解析法规条目。

        regulations.json 中的 violation_types 和 zones 的限制条件
        都映射为法规条目。
        """
        articles: list[RegulationArticle] = []
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        meta = raw.get("meta", {})

        # 解析 violation_types → 法规条目
        for code, vtype in raw.get("violation_types", {}).items():
            if code == "normal":
                continue
            article = RegulationArticle(
                article_id=f"regulations_json:{code}",
                source=meta.get("description", "船舶违规检测规则库"),
                article_number=code,
                title=vtype.get("label", code),
                content=vtype.get("description", ""),
                penalty=vtype.get("penalty", ""),
                violation_codes=[code],
                keywords=[vtype.get("label", code), code],
            )
            articles.append(article)

        # 解析 zones → 区域管制法规条目
        for zone in raw.get("zones", {}):
            zone_id = zone.get("id", "")
            zone_name = zone.get("name", "")
            restrictions = zone.get("restrictions", {})
            time_rules = zone.get("time_rules", [])

            # 构建区域管制描述
            desc_parts = [f"区域：{zone_name}（{zone_id}）"]
            if restrictions.get("entry_allowed") is False:
                desc_parts.append("禁止进入")
            if restrictions.get("fishing_allowed") is False:
                desc_parts.append("禁止捕捞")
            if restrictions.get("max_speed_knots") is not None:
                desc_parts.append(f"限速{restrictions['max_speed_knots']}节")

            # 时间规则
            for tr in time_rules:
                if tr.get("type") == "season":
                    desc_parts.append(
                        f"时间限制：每年{tr.get('start_month', '?')}月{tr.get('start_day', '?')}日"
                        f"至{tr.get('end_month', '?')}月{tr.get('end_day', '?')}日"
                        f"（{tr.get('description', '')}）"
                    )

            # 关联违规代码
            codes = []
            if restrictions.get("entry_allowed") is False:
                codes.append("restricted_area_entry")
            if restrictions.get("fishing_allowed") is False:
                codes.append("illegal_fishing")
            if restrictions.get("max_speed_knots") is not None:
                codes.append("speed_violation")

            article = RegulationArticle(
                article_id=f"regulations_json:zone:{zone_id}",
                source=meta.get("description", "船舶违规检测规则库"),
                article_number=f"区域管制-{zone_id}",
                title=f"{zone_name}管制规定",
                content="；".join(desc_parts),
                penalty="；".join(
                    raw.get("violation_types", {}).get(c, {}).get("penalty", "")
                    for c in codes
                    if c in raw.get("violation_types", {})
                ),
                violation_codes=codes,
                keywords=[zone_name, zone_id] + codes,
            )
            articles.append(article)

        return articles

    @staticmethod
    def parse_pdf_text(text: str, source_name: str) -> list[RegulationArticle]:
        """从 PDF 提取的文本中解析法规条目。

        按中文法律文本的"第X条"格式拆分条目。
        """
        articles: list[RegulationArticle] = []
        matches = ARTICLE_PATTERN_ANCHORED.findall(text)
        if len(matches) < 3:
            # 兜底：个别 PDF 抽取出的文本会把整段拼成一行，条目标题不在行首，
            # 行首规则于是一条都切不出来。此时回退到不限位置切分 ——
            # 宁可带回交叉引用造成的重复（后面有 dedupe_articles 兜着），
            # 也不能把整本法规静默丢成 0 条。
            fallback = ARTICLE_PATTERN.findall(text)
            if len(fallback) >= 3:
                print(f"  [WARN] {source_name}: 条目标题不在行首"
                      f"（行首切出 {len(matches)} 条，不限位置切出 {len(fallback)} 条），"
                      f"已回退；该来源的条号可能有重复")
                matches = fallback

        current_chapter = ""
        # 先提取章节信息
        chapter_pattern = re.compile(r"第[一二三四五六七八九十百千零\d]+章\s*(.+?)(?=\n|$)")
        chapter_matches = chapter_pattern.findall(text)

        for cn_num, content in matches:
            content = content.strip()
            if not content:
                continue

            # 提取标题（第一句话或前50字）
            first_sentence = re.split(r"[。；\n]", content, maxsplit=1)[0]
            title = first_sentence[:80] if len(first_sentence) > 80 else first_sentence

            # 提取罚则（包含"罚""处""吊销""没收"等关键词的句子）
            penalty_sentences = []
            for sentence in re.split(r"[。；\n]", content):
                sentence = sentence.strip()
                if any(kw in sentence for kw in ["罚", "处", "吊销", "没收", "拘留", "追究刑事责任"]):
                    penalty_sentences.append(sentence)
            penalty = "；".join(penalty_sentences) if penalty_sentences else ""

            # 提取关键词
            keywords = []
            maritime_kws = [
                "航行", "安全", "碰撞", "避碰", "超速", "限速", "禁航", "禁渔",
                "AIS", "信号", "渔船", "港口", "航道", "锚地", "台风", "锚泊",
                "追越", "对遇", "交叉", "让路", "直航", "搁浅", "触礁",
                "海事", "搜救", "应急", "签证", "检验", "登记",
            ]
            for kw in maritime_kws:
                if kw in content:
                    keywords.append(kw)

            # 关联违规代码
            violation_codes = []
            if any(kw in content for kw in ["禁航", "禁止进入", "军事禁区"]):
                violation_codes.append("restricted_area_entry")
            if any(kw in content for kw in ["超速", "限速", "不得超过"]):
                violation_codes.append("speed_violation")
            if any(kw in content for kw in ["禁渔", "捕捞", "渔业"]):
                violation_codes.append("illegal_fishing")
            if any(kw in content for kw in ["AIS", "信号", "关闭", "关闭设备"]):
                violation_codes.append("ais_off")
            if any(kw in content for kw in ["碰撞", "避碰", "让路", "追越"]):
                violation_codes.append("collision_risk")

            article_num = cn_to_int(cn_num)
            article = RegulationArticle(
                article_id=f"{source_name}:第{article_num}条",
                source=source_name,
                article_number=str(article_num),
                title=title,
                content=content,
                penalty=penalty,
                violation_codes=violation_codes,
                keywords=keywords,
                chapter=current_chapter,
            )
            articles.append(article)

        return articles

    # ── 小数编号体例（1. / 1.1 / 4.1.1）──

    @staticmethod
    def parse_numbered_text(text: str, source_name: str) -> list[RegulationArticle]:
        """解析用小数编号的法规/指南（如「船舶防台风指南（试行）」）。

        与 parse_pdf_text 的差别只在"怎么切条目"：这里按行首的 1. / 1.1 / 4.1.1 切，
        切完之后 title/penalty/keywords 的提取逻辑完全一致（所以标题、罚则、关键词
        的口径不会因为来源不同而漂移）。

        规则：
          - 只有标题没有正文的项（「1.依据和适用范围」）→ 记为章节上下文，不产出条目；
          - 有正文的项 → 一条法规，article_number 用原始小数串（"4.1.1"）；
          - 顶层标题（"4.台风影响中防抗措施"）会成为其下各条的 chapter。

        article_id 用 f"{source}:{num}"（**没有**"第…条"）—— 因为「第4.1.1条」不是
        中文法规的引用写法。展示时 format_article_ref 会对带小数点的编号省掉"第/条"。
        """
        text = PAGE_FOOTER_PATTERN.sub("", text)
        matches = list(NUMBERED_ITEM_PATTERN.finditer(text))
        if not matches:
            return []

        articles: list[RegulationArticle] = []
        chapter = ""

        for i, m in enumerate(matches):
            num = m.group(1)
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end():end].strip()
            if not body:
                continue

            # 顶层且很短、无句号 → 这是章节标题，给后续条目当上下文
            if "." not in num and len(body) <= _HEADING_MAX_LEN and "。" not in body:
                chapter = body
                continue

            first_sentence = re.split(r"[。；\n]", body, maxsplit=1)[0]
            title = first_sentence[:80] if len(first_sentence) > 80 else first_sentence

            penalty_sentences = []
            for sentence in re.split(r"[。；\n]", body):
                sentence = sentence.strip()
                if any(kw in sentence for kw in ["罚", "处", "吊销", "没收", "拘留", "追究刑事责任"]):
                    penalty_sentences.append(sentence)
            penalty = "；".join(penalty_sentences) if penalty_sentences else ""

            articles.append(RegulationArticle(
                article_id=f"{source_name}:{num}",
                source=source_name,
                article_number=num,
                title=title,
                content=body,
                penalty=penalty,
                violation_codes=[],
                keywords=[],
                chapter=chapter,
            ))

        return articles

    @staticmethod
    def parse_pdf_text_auto(text: str, source_name: str) -> list[RegulationArticle]:
        """按文档实际使用的编号体例自动选解析器。

        先试「第X条」（命中 ≥3 条才算数 —— 只有 1~2 次多半是正文里的交叉引用，
        例如"依照《…》第X条的规定"），否则退到小数编号。两条都不成立返回空。

        之所以要一个统一入口：调用方（RegulationDB.initialize 与 seed 脚本）各自
        判断体例的话，迟早会漂移成两种行为。
        """
        by_article = RegulationParser.parse_pdf_text(text, source_name)
        if len(by_article) >= 3:
            return by_article
        by_number = RegulationParser.parse_numbered_text(text, source_name)
        if by_number:
            return by_number
        return by_article


def apply_doc_meta(articles: list[RegulationArticle]) -> list[str]:
    """按 source 给每条法规盖上 效力层级/文件性质/适用地域（就地修改）。

    返回**未登记元数据**的 source 列表，供调用方告警 —— 新放进来一个 PDF 却忘了
    在 violation_catalog.DOC_META 登记，它会以 priority=9 排在最后，不告警的话
    没人会发现（法规照常入库，只是永远排在末尾）。
    """
    from .violation_catalog import doc_meta

    unknown: list[str] = []
    for a in articles:
        meta = doc_meta(a.source)
        a.priority = int(meta["priority"])
        a.doc_type = str(meta["doc_type"])
        a.region = str(meta["region"])
        if meta["doc_type"] == "其他" and a.source not in unknown:
            unknown.append(a.source)
    return unknown


def dedupe_articles(
    articles: list[RegulationArticle],
) -> tuple[list[RegulationArticle], dict[str, int]]:
    """按 article_id 去重，返回 (保留的条目, {被解析出多份的 article_id: 出现次数})。

    为什么非去重不可：article_id 是 Milvus 的主键。同一批数据里出现两个相同主键，
    upsert 会**直接拒绝**（duplicate primary keys are not allowed in the same batch），
    而 insert 会静默留下两个版本 —— get_collection_stats 的 row_count 翻倍，
    向量检索里同一条法条在 top-k 中占多个坑，把别的法条挤出去。

    重复从哪来：解析器按「第X条」切正文，正文里的**交叉引用**（"应鸣放第三十四条
    3款规定的声号"）也被当成新条目的开头，于是同一个条号被切出多份。这是解析器
    的缺陷，各来源都有、COLREG 这种双栏中英对照 PDF 最重（实测它的 71 条里有 33 条
    是交叉引用切出来的碎片，13 个条号重复）。这里兜住，不让它污染库。

    保留哪一份：正文最长的那份。交叉引用切出来的碎片都很短（"所负的 义务。" 7 字、
    "追 越" 2 字），真正的条目正文通常是它的几十倍；同长时保留先出现的。
    ⚠ 这只是止血：COLREG 的条目边界本身仍是错的（正文里混着英文栏和邻条碎片），
    要真正修好得按版式解析这本 PDF。
    """
    best: dict[str, RegulationArticle] = {}
    seen: dict[str, int] = {}
    order: list[str] = []
    for a in articles:
        seen[a.article_id] = seen.get(a.article_id, 0) + 1
        cur = best.get(a.article_id)
        if cur is None:
            best[a.article_id] = a
            order.append(a.article_id)
        elif len(a.content) > len(cur.content):
            best[a.article_id] = a
    dups = {k: v for k, v in seen.items() if v > 1}
    return [best[k] for k in order], dups


def report_duplicates(dups: dict[str, int]) -> None:
    """把去重结果按来源打出来 —— 重复多说明该来源的解析有问题，要看得见。"""
    if not dups:
        return
    by_src: dict[str, int] = {}
    for aid, n in dups.items():
        src = aid.split(":", 1)[0]
        by_src[src] = by_src.get(src, 0) + (n - 1)
    print(f"  [WARN] {len(dups)} 个条号被解析出多份（正文里的交叉引用被当成了新条目），"
          f"已按正文最长保留一份。多出来的条数按来源："
          + "，".join(f"{s} +{n}" for s, n in sorted(by_src.items(), key=lambda x: -x[1])))


# ── PDF 取文（版面感知）──
#
# COLREG 是中英对照版式：同一页里一种语言在一栏、另一种在另一栏。PyMuPDF 的
# page.get_text() 按**块序**拼接，不保证按栏读 —— 实测英文块会插进中文流里，
# 两个后果都实测过：
#   1. 条目正文里混进整段英文：改之前 COLREG 入库正文 100887 字；去掉英文栏后 25310 字；
#      再去掉每页重复的出品方信息（见下"去页眉页脚"）才是条文本身的 21029 字。
#      对中文查询，前两者的差额都只是噪声；
#   2. 中英块交错会把条目边界挤漂：第23条实际是干净的，被夹碎的是第24条。
# 对照页的处理：只保留含中日韩字的块，按「栏 → y」排序（报纸顺序）。
#
# 判定"这一页是不是对照页"用**纯拉丁散文的字数**，而不是"有没有英文"：
# 数字表格、单位、型号（12.5 / N=34 / AIS）不是散文，不该让页面被当成对照页。
#
# 为什么是"字数"而不是"块数"：块数是个坏指标。第28页整栏英文是一个大块（块数=1），
# 第1、2 页每栏各一个大块（块数=2）—— 用「块数 >= 3」判，正是这两类页漏判，
# 而第1页漏判的后果是整段英文 Rule 1 留在「第1条」正文里（2720 字，其中过半是英文）。
# 字数则把两类页分得很开，实测（rule/ 下五个 PDF 全部 113 页）：
#   COLREG 对照页   散文 1168..2503 字，拉丁占比 0.65..0.88  → 38 页全部命中
#   另外四个来源     散文 0 字（一页都没有；福州港有 5 页拉丁占比 0.32..0.74，
#                   但那些是表格与英文名称，散文 0 字）      → 0 页误判
# 阈值取 400：离误判上界（0）有量级余量，离命中下界（1168）留了余量，
# 半页空白的对照页（正文没排满）也不至于漏判。
#
# 取文完整链路的最终实测（五个来源 113 页 → 283 条）：
#   COLREG              38 条 1..38，正文 21029 字，不含英文与页眉页脚
#   海上交通安全法2021     122 条 1..122，18040 字  ┐
#   福州港港口章程          44 条 1..44，22429 字   ├ 与改动前一致，
#   渔港水域条例2019        29 条 1..29，2432 字    │ 只有页码/出品方被去掉
#   船舶防台风指南          47 条 小数编号，5223 字 ┘
_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_PROSE_RE = re.compile(r"(?:\b[A-Za-z]{2,}\b[^A-Za-z]*){5,}")
_BILINGUAL_MIN_PROSE_CHARS = 400
_BILINGUAL_MIN_LATIN_SHARE = 0.25


def _page_layout_stats(page) -> tuple[int, int, int]:
    """(总字数, 纯拉丁块字数, 纯拉丁散文块字数) —— 判断这一页是不是对照页用。"""
    total = latin = prose = 0
    for b in page.get_text("blocks") or []:
        txt = (b[4] or "").strip()
        if not txt:
            continue
        total += len(txt)
        if not _CJK_RE.search(txt):
            latin += len(txt)
            if _LATIN_PROSE_RE.search(txt):
                prose += len(txt)
    return total, latin, prose


def _page_text(page) -> str:
    """单页取文：对照页只留中文栏，其余页维持 page.get_text() 原样。"""
    total, latin, prose = _page_layout_stats(page)
    if not (total and prose >= _BILINGUAL_MIN_PROSE_CHARS
            and latin / total >= _BILINGUAL_MIN_LATIN_SHARE):
        return page.get_text() or ""

    mid = page.rect.width / 2

    def key(b):
        x0, y0, x1 = b[0], b[1], b[2]
        band = -1 if x0 < mid < x1 else (0 if x0 < mid else 1)
        return (band, round(y0, 1), x0)

    blocks = [b for b in (page.get_text("blocks") or [])
              if (b[4] or "").strip() and _CJK_RE.search(b[4])]
    return "".join(b[4] for b in sorted(blocks, key=key))


def extract_pdf_text(pdf_path) -> str:
    """从 PDF 提取文本（版面感知 + 去页眉页脚）。优先 PyMuPDF，回退到 pdfminer。

    **唯一的取文入口**：RegulationDB._extract_pdf_text 与 seed 脚本都走这里。
    两处各写一份的话迟早漂移成两种行为 —— 本文件里已经踩过这种坑。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        fitz = None

    if fitz is not None:
        doc = fitz.open(str(pdf_path))
        try:
            pages = [_page_text(p) for p in doc]
        finally:
            doc.close()
        return _strip_running_heads(pages)

    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(pdf_path))
    except ImportError:
        pass

    print(f"  [WARN] {Path(pdf_path).name}: 无 PDF 解析库（需安装 PyMuPDF 或 pdfminer）")
    return ""


# ── 去页眉页脚（跨页重复行）──
#
# COLREG 每页都印着同一段出品方信息（"中国海员之家网站航海资料系列…"、书名、
# 「第 / N / 页」、"…搜集整理出品，更多航海资料请到网站免费下载。"）。
# 这段字在 38 条法规的正文里各出现一次 —— 对检索是**有害**噪声，不只是占字数：
# 38 条正文共享同一段文字，余弦相似度会被这段共同文字拉平，条目之间反而分不开。
#
# 判据：一行（数字归一后）在**页边**出现在 80% 以上的页面上，就是页眉页脚。
#   - 限定"页边"（每页前 8 行 / 后 8 行）：页眉页脚的定义就是在页边。
#     不限定的话，正文里的项目符号（"（一）"）也会被误判成重复行删掉。
#   - 要 80% 而不是 100%：偶有一页排版不同不该让整条规则失效（实测福州港 36/38）。
#   - 数字归一（123 → #）：页码每页不同，不归一就认不出来。
#   - 限长 160 字：重复的长段落是正文（例如各条共用的标准条款），不能当噪声删。
#   - 少于 4 页的文档不做（样本太小，"3 页里 3 页都有"说明不了什么）。
# 实测（rule/ 下五个 PDF 共 113 页）：
#   COLREG        清掉 6 行 × 38 页（占正文 18%），那是唯一的重复块
#   福州港港口章程   清掉每页一个孤立页码，正文少 ~110 字
#   防台风指南       清掉 "— N —"（该来源本来就在 parse_numbered_text 里清过一次）
#   海安法 / 渔港条例 无跨页重复行，一字不动
_BOILERPLATE_EDGE_LINES = 8
_BOILERPLATE_MIN_PAGES = 4
_BOILERPLATE_PAGE_RATIO = 0.8
_BOILERPLATE_MAX_LEN = 160
_DIGITS_RE = re.compile(r"\d+")


def _norm_line(line: str) -> str:
    """归一化一行以便跨页比对：压空白、把数字统一成 #（页码每页都不同）。"""
    return _DIGITS_RE.sub("#", " ".join(line.split()))


def _strip_running_heads(pages: list[str]) -> str:
    """去掉跨页重复的页眉页脚行。pages 是逐页取到的文本。"""
    if len(pages) < _BOILERPLATE_MIN_PAGES:
        return "".join(pages)

    def edge_lines(text: str) -> list[str]:
        lines = text.splitlines()
        return lines[:_BOILERPLATE_EDGE_LINES] + lines[-_BOILERPLATE_EDGE_LINES:]

    seen: dict[str, int] = {}
    for text in pages:
        for norm in {_norm_line(l) for l in edge_lines(text)
                     if l.strip() and len(l.strip()) <= _BOILERPLATE_MAX_LEN}:
            seen[norm] = seen.get(norm, 0) + 1

    need = max(3, math.ceil(len(pages) * _BOILERPLATE_PAGE_RATIO))
    boiler = {k for k, n in seen.items() if n >= need}
    if not boiler:
        return "".join(pages)

    out = []
    for text in pages:
        lines = text.splitlines(keepends=True)
        keep_n = len(lines) - _BOILERPLATE_EDGE_LINES
        for i, line in enumerate(lines):
            if (i < _BOILERPLATE_EDGE_LINES or i >= keep_n) \
                    and line.strip() and len(line.strip()) <= _BOILERPLATE_MAX_LEN \
                    and _norm_line(line) in boiler:
                continue
            out.append(line)
    return "".join(out)


# ── 向量化 ──

def get_embedding(text: str, model: str = EMBEDDING_MODEL) -> list[float]:
    """使用 Ollama 生成文本嵌入向量。

    用专用 embedding 模型（nomic-embed-text）生成语义向量；
    维度不足 EMBEDDING_DIM 时补 0（Milvus 集合固定 1024 维）。
    若模型不可用，回退到简单哈希向量（保证可运行，但检索质量较低）。

    ⚠ 回退是**静默**的：哈希向量与真 nomic 向量的余弦相似度在 0.01~0.03 量级，
    检索结果会退化成"随机返回几条"，而调用方完全看不出来。实测主机上
    OLLAMA_HOST 指错端口时就是这个后果（HAS_OLLAMA=True 但走的是哈希分支）。
    所以回退时会往 stderr 打警告，并置 EMBEDDING_DEGRADED 供调用方拒绝执行
    写库类操作。
    """
    global EMBEDDING_DEGRADED
    if not HAS_OLLAMA:
        _warn_degraded("未安装/未启用 ollama 客户端")
        return _hash_vector(text)

    try:
        response = ollama.embeddings(model=model, prompt=text[:2000])
        embedding = response.get("embedding", [])
        if embedding and len(embedding) > 0:
            # 如果维度不匹配，截断或填充
            if len(embedding) >= EMBEDDING_DIM:
                return embedding[:EMBEDDING_DIM]
            return embedding + [0.0] * (EMBEDDING_DIM - len(embedding))
        _warn_degraded("ollama 返回空向量")
    except Exception as e:
        _warn_degraded(f"调用 ollama embeddings 失败: {e}")

    # 回退：用哈希生成伪向量（保证可运行，但语义检索质量较低）
    return _hash_vector(text)


# 是否已退化到哈希向量（供调用方判断检索结果是否可信）。单向置位，不自动复位。
EMBEDDING_DEGRADED = False
_degraded_warned = False


def _warn_degraded(reason: str) -> None:
    """回退哈希向量时告警一次（每次都打会把日志刷满）。"""
    global EMBEDDING_DEGRADED, _degraded_warned
    EMBEDDING_DEGRADED = True
    if _degraded_warned:
        return
    _degraded_warned = True
    print(f"[RegulationDB] ⚠ 向量化已退化为哈希伪向量（{reason}）——"
          f"语义检索结果不可信，请检查 OLLAMA_HOST / 模型是否就绪", file=sys.stderr)


def _hash_vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """基于文本哈希生成伪向量（回退方案）。

    不是真正的语义向量，但保证系统可运行。
    后续接入专用 embedding 模型后替换。
    """
    result = []
    for i in range(0, dim, 8):
        chunk = text[i:i + 64] + str(i)
        h = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        for j in range(0, min(8, dim - i)):
            val = int(h[j * 8:(j + 1) * 8], 16) / (2**32)
            result.append(val * 2 - 1)  # 归一化到 [-1, 1]
    return result[:dim]


# ── 法规向量数据库 ──

class RegulationDB:
    """法规向量数据库 — 基于 Milvus 的法规语义检索。

    用法:
        db = RegulationDB()
        db.initialize()  # 首次运行：解析法规 + 向量化 + 导入 Milvus
        results = db.search("船舶在禁渔区捕捞", top_k=5)
        for r in results:
            print(f"{r['source']} 第{r['article_number']}条: {r['title']}")
    """

    def __init__(
        self,
        db_file: Optional[str] = None,
        regulations_path: Optional[str] = None,
        rule_dir: Optional[str] = None,
    ):
        if not HAS_MILVUS:
            raise ImportError("pymilvus 未安装。运行: pip install 'pymilvus>=2.4'")

        # 统一从 config 解析 Milvus 连接地址：只支持 Server（Docker）模式，不用 Lite
        try:
            from config import milvus_uri
            self.uri = milvus_uri()
        except Exception:
            self.uri = None
        self.db_file = db_file or os.environ.get(
            "MILVUS_DB_FILE", "./milvus_lite.db"
        )
        self.regulations_path = Path(
            regulations_path or Path(__file__).parent.parent / "regulations.json"
        )
        self.rule_dir = Path(
            rule_dir or Path(__file__).parent.parent / "rule"
        )
        self._client: Optional[MilvusLiteClient] = None
        self._initialized = False
        # 新增：初始化后自动探测可用性
        self._available = False
        self._probe()

    def _probe(self) -> None:
        """探测 Milvus 是否可用（连接 + 集合存在即视为可用）。"""
        if not self.uri:
            self._available = False
            return
        try:
            self._call("list_collections")
            self._available = True
        except Exception as _e:
            self._available = False

    @property
    def client(self) -> MilvusLiteClient:
        if self._client is None:
            if self.uri:
                # 只支持 Server 模式：连接外部 Milvus（MilvusClient 的 uri 参数支持 http 地址）
                self._client = MilvusLiteClient(uri=self.uri)
            else:
                raise RuntimeError(
                    "RegulationDB 已禁用 Milvus（未配置 MILVUS_URI / MILVUS_ENABLE）。"
                    "本项目只支持连接 Docker Milvus，请启动 Docker 并配置 config.milvus_uri()。"
                )
        return self._client

    # Milvus 连接失效的错误特征（如容器重启后旧 gRPC channel 被关闭）
    _CONN_ERROR_MARKERS = (
        "closed channel",
        "channel is closed",
        "connection refused",
        "connection reset",
        "fail to connect",
    )

    def _call(self, method: str, *args, **kwargs):
        """调用 Milvus 客户端方法；若连接已失效则重建客户端并重试一次。
        仅用于幂等读操作（load_collection / search / get / has_collection / get_collection_stats）。
        Milvus 重启后旧连接成为死通道，全新连接可正常恢复。"""
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except Exception as exc:
            if not any(m in str(exc).lower() for m in self._CONN_ERROR_MARKERS):
                raise
            # 丢弃旧客户端（下次访问 self.client 时重建），再试一次
            self._client = None
            return getattr(self.client, method)(*args, **kwargs)

    def _has_collection(self) -> bool:
        try:
            return self._call("has_collection", REGULATION_COLLECTION)
        except Exception:
            return False

    def count(self) -> int:
        try:
            return int(
                self._call("get_collection_stats", REGULATION_COLLECTION)
                .get("row_count", 0)
            )
        except Exception:
            return 0

    # ── 初始化 ──

    def initialize(self, force: bool = False) -> int:
        """初始化法规数据库：解析法规 → 向量化 → 导入 Milvus。

        Args:
            force: 是否强制重建（删除已有集合并重新导入）

        Returns:
            导入的法规条目总数
        """
        # 检查是否已初始化。
        # ⚠ 这里必须自己 try，不能只写 `if self._has_collection():` ——
        #   _has_collection()/count() 在出错时返回 False/0，而 False 会被下面
        #   当成「集合不存在」，于是往**已存在**的集合里把全部法规再插一遍
        #   （_create_collection 见集合已存在会提前 return，不重建）。
        #   排查到的真实事故：Milvus 一次抖动让这个守卫失效，照常理该跳过的初始化
        #   照跑，重复插入了 37 条（COLREG 34 + 海安法 3），检索时同一条法条占多个坑。
        #   查不到 ≠ 没有：无法确认时就跳过，下次再初始化，代价只是这一轮没跑。
        if not force:
            try:
                exists = bool(self._call("has_collection", REGULATION_COLLECTION))
                count = self.count() if exists else 0
            except Exception as exc:
                print(f"[RegulationDB] 无法确认法规集合状态（{exc}），"
                      f"跳过本次初始化以免重复导入")
                return 0
            if exists and count > 0:
                print(f"[RegulationDB] 已有 {count} 条法规，跳过初始化")
                self._initialized = True
                return count

        # 1. 解析法规条目
        articles = self._parse_all_sources()
        print(f"[RegulationDB] 解析到 {len(articles)} 条法规条目")

        if not articles:
            return 0

        # 2. 创建 Milvus 集合
        self._create_collection(drop_if_exists=force)

        # 3. 向量化并导入
        self._insert_articles(articles)

        self._initialized = True
        return len(articles)

    def _parse_all_sources(self) -> list[RegulationArticle]:
        """从所有来源解析法规条目。"""
        articles: list[RegulationArticle] = []

        # 1. regulations.json
        if self.regulations_path.exists():
            try:
                parsed = RegulationParser.parse_regulations_json(self.regulations_path)
                articles.extend(parsed)
                print(f"  从 {self.regulations_path.name} 解析 {len(parsed)} 条")
            except Exception as e:
                print(f"  [WARN] 解析 {self.regulations_path} 失败: {e}")

        # 2. rule/ 目录下的 PDF（**递归** —— 按文件性质分子目录存放，
        #    见 violation_catalog.DOC_META；子目录不影响 source 名，source 取的是文件名）
        if self.rule_dir.exists():
            for pdf_path in sorted(self.rule_dir.rglob("*.pdf")):
                try:
                    text = self._extract_pdf_text(pdf_path)
                    if not text.strip():
                        print(f"  [SKIP] {pdf_path.name} 无文本")
                        continue
                    source_name = pdf_path.stem
                    parsed = RegulationParser.parse_pdf_text_auto(text, source_name)
                    if not parsed:
                        print(f"  [SKIP] {pdf_path.name} 解析出 0 条（两种编号体例都不匹配）")
                        continue
                    articles.extend(parsed)
                    print(f"  从 {pdf_path.name} 解析 {len(parsed)} 条")
                except Exception as e:
                    print(f"  [WARN] 解析 {pdf_path.name} 失败: {e}")

        # 3. 去重（交叉引用会让同一个条号被切出多份；主键重复导不进 Milvus）
        articles, dups = dedupe_articles(articles)
        report_duplicates(dups)

        # 4. 盖效力层级/文件性质/适用地域，并报告漏登记的来源
        unknown = apply_doc_meta(articles)
        if unknown:
            print(f"  [WARN] 以下来源未在 violation_catalog.DOC_META 登记，"
                  f"将以 priority=9 排在末尾：{unknown}")

        return articles

    @staticmethod
    def _extract_pdf_text(pdf_path: Path) -> str:
        """从 PDF 文件提取文本（实现见模块级 extract_pdf_text，取文只此一处）。"""
        return extract_pdf_text(pdf_path)

    # ── Milvus 集合管理 ──

    def _has_collection(self) -> bool:
        try:
            return self._call("has_collection", REGULATION_COLLECTION)
        except Exception:
            return False

    def _create_collection(self, drop_if_exists: bool = False) -> None:
        if self._call("has_collection", REGULATION_COLLECTION):
            if drop_if_exists:
                try:
                    self.client.drop_collection(REGULATION_COLLECTION)
                except Exception:
                    # Windows file lock issue: try manual cleanup
                    import shutil
                    col_dir = Path(self.db_file) / "collections" / REGULATION_COLLECTION
                    if col_dir.exists():
                        try:
                            shutil.rmtree(str(col_dir), ignore_errors=True)
                        except Exception:
                            pass
                    # Try again
                    try:
                        self.client.drop_collection(REGULATION_COLLECTION)
                    except Exception:
                        print(f"  [WARN] 无法删除旧集合，将尝试覆盖创建")
            else:
                return

        schema = MilvusLiteClient.create_schema(
            auto_id=False,
            enable_dynamic_field=True,
        )
        schema.add_field("article_id", DataType.VARCHAR, max_length=128, is_primary=True)
        schema.add_field("source", DataType.VARCHAR, max_length=256)
        schema.add_field("article_number", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=16384)
        schema.add_field("penalty", DataType.VARCHAR, max_length=2048)
        schema.add_field("violation_codes", DataType.VARCHAR, max_length=512)
        schema.add_field("keywords", DataType.VARCHAR, max_length=1024)
        schema.add_field("chapter", DataType.VARCHAR, max_length=256)
        # 效力层级/文件性质/适用地域（见 violation_catalog.DOC_META）。
        # ⚠ 加字段必须同时改 scripts/seed_regulations_milvus.py 的 schema ——
        #   两处建的是同一个集合，只改一处会出现"本地库有、Docker 库没有"的列不一致。
        schema.add_field("priority", DataType.INT64)
        schema.add_field("doc_type", DataType.VARCHAR, max_length=32)
        schema.add_field("region", DataType.VARCHAR, max_length=32)
        schema.add_field("content_vector", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM)

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            "content_vector",
            metric_type="COSINE",
            index_type="IVF_FLAT",
            params={"nlist": 64},
        )

        self.client.create_collection(
            collection_name=REGULATION_COLLECTION,
            schema=schema,
            index_params=index_params,
        )
        self._call("load_collection", REGULATION_COLLECTION)
        print(f"[RegulationDB] 集合 {REGULATION_COLLECTION} 创建完成")

    def _insert_articles(self, articles: list[RegulationArticle]) -> None:
        """向量化法规条目并批量导入 Milvus。"""
        batch_size = 50
        for i in range(0, len(articles), batch_size):
            batch = articles[i:i + batch_size]
            records = []
            for article in batch:
                emb_text = article.embedding_text()
                vector = get_embedding(emb_text)
                d = article.to_dict()
                d["content_vector"] = vector
                records.append(d)

            # upsert 而不是 insert：article_id 是主键，同一批法规被导入第二次时
            # upsert 覆盖同一条，而 insert 会为同一个主键留下两个版本 ——
            # 查询按主键去重后只回一条，但 get_collection_stats 的 row_count 会把
            # 版本都算上（实测 283 条曾显示 320），且向量检索里同一条法条会在 top-k
            # 里占多个坑、把别的法条挤出去。重复导入不该有副作用。
            try:
                self.client.upsert(
                    collection_name=REGULATION_COLLECTION,
                    data=records,
                )
            except Exception as e:
                print(f"  [WARN] 批量写入失败 ({i}-{i+len(batch)}): {e}")
                # 逐条写入
                for record in records:
                    try:
                        self.client.upsert(
                            collection_name=REGULATION_COLLECTION,
                            data=[record],
                        )
                    except Exception:
                        pass

            print(f"  已导入 {min(i + batch_size, len(articles))}/{len(articles)} 条")

    # ── 检索 ──

    def search(
        self,
        query: str,
        top_k: int = 5,
        violation_code: Optional[str] = None,
        source: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """语义检索法规条目。

        Args:
            query: 查询文本（如"船舶在禁渔区捕捞"）
            top_k: 返回最相关的 K 条结果
            violation_code: 可选，按违规代码过滤
            source: 可选，按法规来源过滤

        Returns:
            法规条目列表，每条包含 article_id/source/article_number/title/content/penalty/similarity
            similarity 为真实余弦相似度（0~1，越大越相关）。集合 metric 为 COSINE，
            **Milvus 返回的 distance 本身就是余弦相似度**（实测：同一条目用自身
            embedding_text 检索，返回 distance≈0.00，即余弦≈1），直接取用即可，
            不要再做 1-distance 换算。
        """
        if not self._initialized and not self._has_collection():
            return []

        query_vector = get_embedding(query)

        # 构建过滤表达式
        filter_expr = ""
        conditions = []
        if violation_code:
            conditions.append(f'violation_codes like "%{violation_code}%"')
        if source:
            conditions.append(f'source == "{source}"')
        if conditions:
            filter_expr = " and ".join(conditions)

        try:
            self._call("load_collection", REGULATION_COLLECTION)
            results = self._call(
                "search",
                collection_name=REGULATION_COLLECTION,
                data=[query_vector],
                limit=top_k,
                filter=filter_expr or None,
                output_fields=[
                    "article_id", "source", "article_number",
                    "title", "content", "penalty",
                    "violation_codes", "keywords", "chapter",
                    "priority", "doc_type", "region",
                ],
            )
        except Exception as e:
            print(f"[RegulationDB] 检索错误: {e}")
            return []

        hits = []
        for result_group in results:
            for hit in result_group:
                entity = hit.get("entity", {})
                hits.append({
                    "article_id": entity.get("article_id", ""),
                    "source": entity.get("source", ""),
                    "article_number": entity.get("article_number", ""),
                    "title": entity.get("title", ""),
                    "content": entity.get("content", ""),
                    "penalty": entity.get("penalty", ""),
                    "violation_codes": json.loads(entity.get("violation_codes", "[]")),
                    "keywords": json.loads(entity.get("keywords", "[]")),
                    "chapter": entity.get("chapter", ""),
                    "priority": int(entity.get("priority") or 9),
                    "doc_type": entity.get("doc_type", ""),
                    "region": entity.get("region", ""),
                    # COSINE 度量下 distance 即余弦相似度，直接用。
                    # （此前写成 1.0 - distance，把分数整个取反了：界面上"相关度27%"
                    #   真实是 73%。排序不受影响——Milvus 已按相似度排好——所以一直没暴露。）
                    "similarity": max(0.0, round(hit.get("distance", 0.0), 4)),
                })
        return hits

    def search_by_violation(
        self,
        violation_type: str,
        zone_name: Optional[str] = None,
        top_k: int = 3,
        min_similarity: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """根据违规类型检索相关法规条目。

        这是 cascade 检测到违规后的主要调用入口。

        策略（三级）：
          1. 锚定条款优先：violation_catalog.REG_ANCHORS 中人工核对过的
             （source, 条号）→ get_article 精确取出，并用真实余弦相似度排序。
             纯向量检索排序不可靠（会把无关条款排到正确条款前面），
             所以"这个违规 → 这条法规"用锚定表保证准确。
          2. 锚定不在库中 → 带 violation_code 过滤的向量检索（法规条目自带
             violation_codes 字段标明适用违规类型）。search() 早就支持这个过滤，
             但在本次修复前**没有任何调用方传过**，等于白写。
          3. 再退纯向量检索，并按 MIN_SIMILARITY 过滤。

        第 3 步的下限是**弱兜底，不是相关性判据**（实测见 MIN_SIMILARITY 上方
        注释：相关对 0.583~0.845 与不相关对 0.727~0.772 完全重叠）。它只能砍掉
        分布最尾部的噪声；真正决定返回哪条法的是第 1 步的锚定表和第 2 步的
        violation_code 过滤。语料一旦混进大量无关条目，纯向量这一层是会误召的，
        所以**新违规类型务必先补 REG_ANCHORS**，别指望下限兜住。

        Args:
            violation_type: 违规类型代码（可能是 LLM 自由输出的杂乱代码，
                如 "AIS01"/"AIS012"，会先经 violation_catalog 归一化为规范码）
            zone_name: 可选，区域名称（用于更精确匹配）
            top_k: 返回数量
            min_similarity: 相似度下限，None 时用模块常量 MIN_SIMILARITY

        Returns:
            法规条目列表（含 source/article_number/title/content/penalty/similarity）
        """
        raw = (violation_type or "").strip()
        if not raw:
            return []
        floor = MIN_SIMILARITY if min_similarity is None else min_similarity
        try:
            from .violation_catalog import normalize_violation_code, describe, REG_ANCHORS
            code = normalize_violation_code(raw)
            info = describe(code)
            # 未知码用原文搜索；规范码用中文含义（英文代码搜不到中文法规）
            query = raw if code == "unknown" else ((info.get("meaning") or "").strip() or (info.get("label") or "").strip())
        except Exception:
            code, info, query = "", {}, raw

        # ── 1) 锚定条款优先 ──
        anchors = (REG_ANCHORS or {}).get(code) or []
        anchored: list[dict[str, Any]] = []
        for _src, _num in anchors:
            art = self.get_article(f"{_src}:第{_num}条")
            if not art:
                continue
            qtext = f"{zone_name} {query}".strip() if zone_name else query
            art["similarity"] = self._similarity(qtext, art) if qtext else 0.0
            anchored.append(art)
        if anchored:
            # 先按效力层级（priority 小=效力高），同层级内再按相似度。
            # 锚定表本身是人工挑过的，这里排序只影响"多条锚定谁在前"
            # （如 collision_risk 同时锚了 COLREG 第14/15 条）。
            anchored.sort(key=lambda a: (a.get("priority") or 9, -(a.get("similarity") or 0.0)))
            return anchored[:top_k]

        # ── 2) 向量检索兜底 ──
        # 未知码不做向量检索（结果不可靠），交回调用方用 FALLBACK_REG 兜底
        if not query or code == "unknown":
            return []
        qtext = f"{zone_name} {query}".strip() if zone_name else query

        hits: list[dict[str, Any]] = []
        if code:
            # 2a) 只用标了本违规类型的条目
            hits = [h for h in self.search(qtext, top_k=top_k, violation_code=code)
                    if (h.get("similarity") or 0.0) >= floor]
        if not hits:
            # 2b) 过滤后不足 → 纯向量检索
            hits = [h for h in self.search(qtext, top_k=top_k)
                    if (h.get("similarity") or 0.0) >= floor]
            # 排前面的优先级（从高到低）：
            #   1. 标了本违规码的条目（这是确定的元数据，不是猜的）
            #   2. 效力层级（priority 小=效力高）—— 分数不可靠（见 MIN_SIMILARITY 注释），
            #      所以用"是哪一级法规"这个确定事实当主排序，相似度只做同层级内的次序
            #   3. 相似度
            if code and code != "unknown":
                hits.sort(key=lambda h: (
                    0 if code in (h.get("violation_codes") or []) else 1,
                    h.get("priority") or 9,
                    -(h.get("similarity") or 0.0),
                ))
            else:
                hits.sort(key=lambda h: (h.get("priority") or 9,
                                         -(h.get("similarity") or 0.0)))
        return hits

    @staticmethod
    def _article_embedding_text(article: dict[str, Any]) -> str:
        """重建法规条目的向量化文本（与 RegulationArticle.embedding_text 一致）。"""
        parts = []
        if article.get("chapter"):
            parts.append(f"【{article['chapter']}】")
        parts.append(f"第{article.get('article_number', '')}条 {article.get('title', '')}")
        parts.append(article.get("content", ""))
        if article.get("penalty"):
            parts.append(f"罚则：{article['penalty']}")
        keywords = article.get("keywords") or []
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except Exception:
                keywords = []
        if keywords:
            parts.append("关键词：" + "、".join(keywords))
        return "\n".join(parts)

    def _similarity(self, query_text: str, article: dict[str, Any]) -> float:
        """查询文本与法规条目的真实余弦相似度（0~1）。"""
        try:
            import math
            qv = get_embedding(query_text[:2000])
            av = get_embedding(self._article_embedding_text(article)[:2000])
            na = math.sqrt(sum(x * x for x in qv))
            nb = math.sqrt(sum(x * x for x in av))
            if na == 0 or nb == 0:
                return 0.0
            dot = sum(a * b for a, b in zip(qv, av))
            return max(0.0, round(dot / (na * nb), 4))
        except Exception:
            return 0.0

    def get_article(self, article_id: str) -> Optional[dict[str, Any]]:
        """根据 article_id 精确获取一条法规。"""
        try:
            self._call("load_collection", REGULATION_COLLECTION)
            results = self._call(
                "get",
                collection_name=REGULATION_COLLECTION,
                ids=[article_id],
                output_fields=[
                    "article_id", "source", "article_number",
                    "title", "content", "penalty",
                    "violation_codes", "keywords", "chapter",
                    "priority", "doc_type", "region",
                ],
            )
            if results:
                r = results[0]
                return {
                    "article_id": r.get("article_id", ""),
                    "source": r.get("source", ""),
                    "article_number": r.get("article_number", ""),
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "penalty": r.get("penalty", ""),
                    "violation_codes": json.loads(r.get("violation_codes", "[]")),
                    "keywords": json.loads(r.get("keywords", "[]")),
                    "chapter": r.get("chapter", ""),
                    "priority": int(r.get("priority") or 9),
                    "doc_type": r.get("doc_type", ""),
                    "region": r.get("region", ""),
                }
        except Exception:
            pass
        return None

    def format_regulation_context(
        self,
        violation_type: str,
        zone_name: Optional[str] = None,
        max_articles: int = 3,
    ) -> str:
        """生成可注入 LLM prompt 的法规上下文。

        Args:
            violation_type: 违规类型代码
            zone_name: 区域名称
            max_articles: 最多返回几条法规

        Returns:
            格式化的法规上下文文本
        """
        articles = self.search_by_violation(violation_type, zone_name, max_articles)
        if not articles:
            return ""

        parts = ["【相关法规】"]
        for i, art in enumerate(articles, 1):
            parts.append(f"  {i}. {art['source']} 第{art['article_number']}条")
            if art.get("title"):
                parts.append(f"     {art['title']}")
            if art.get("content"):
                # 截断过长的内容
                content = art["content"]
                if len(content) > 200:
                    content = content[:200] + "…"
                parts.append(f"     {content}")
            if art.get("penalty"):
                parts.append(f"     罚则：{art['penalty']}")
            parts.append(f"     相关度：{art['similarity']:.0%}")

        return "\n".join(parts)

    def count(self) -> int:
        """获取法规条目总数。"""
        if not self._has_collection():
            return 0
        try:
            stats = self._call("get_collection_stats", REGULATION_COLLECTION)
            return stats.get("row_count", 0)
        except Exception:
            return 0

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
