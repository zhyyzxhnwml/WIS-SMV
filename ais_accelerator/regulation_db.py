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
# 当前取值依据（2026-09 实测，语料只有 3 条区域管制规定）：
#   不相关对（AIS 违规 ↔ 军事禁区/禁渔区/限速区）  余弦 0.49 ~ 0.59
#   相关对  （违规捕捞 ↔ 禁渔区）                  余弦 0.63
#   相关对  （进入禁航区 ↔ 军事禁区）              余弦 0.72
#   自命中                                          余弦 1.00
# 0.60 取在"实测不相关上界 0.59"之上。
#
# ⚠ 这个分离度**很勉强**：相关对 0.63 只比不相关上界 0.59 高 0.04。原因是语料
# 太小且同域（都是"船舶…禁止…管制"的短中文文本，nomic 对短中文的同域基线本就
# 偏高）。等 E:\AIS\rule\ 的法规原文入库、语料有几十上百条之后，必须按同一方法
# 重测（取一条已知相关对与一批已知不相关对，看分离度）再定这个数。
MIN_SIMILARITY = 0.60

# 法规条目解析的正则模式
ARTICLE_PATTERN = re.compile(
    r"第([一二三四五六七八九十百千零\d]+)条\s*(.*?)(?=第[一二三四五六七八九十百千零\d]+条|$)",
    re.DOTALL,
)

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
        matches = ARTICLE_PATTERN.findall(text)

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
        # 检查是否已初始化
        if not force and self._has_collection():
            count = self.count()
            if count > 0:
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
                articles.extend(
                    RegulationParser.parse_regulations_json(self.regulations_path)
                )
                print(f"  从 {self.regulations_path.name} 解析 {len(articles)} 条")
            except Exception as e:
                print(f"  [WARN] 解析 {self.regulations_path} 失败: {e}")

        # 2. rule/ 目录下的 PDF 文件
        if self.rule_dir.exists():
            for pdf_path in sorted(self.rule_dir.glob("*.pdf")):
                try:
                    text = self._extract_pdf_text(pdf_path)
                    if text:
                        source_name = pdf_path.stem
                        parsed = RegulationParser.parse_pdf_text(text, source_name)
                        articles.extend(parsed)
                        print(f"  从 {pdf_path.name} 解析 {len(parsed)} 条")
                except Exception as e:
                    print(f"  [WARN] 解析 {pdf_path.name} 失败: {e}")

        return articles

    @staticmethod
    def _extract_pdf_text(pdf_path: Path) -> str:
        """从 PDF 文件提取文本。优先用 PyMuPDF，回退到 pdfminer。"""
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
            text = ""
            for page in doc:
                text += page.get_text()
            doc.close()
            return text
        except ImportError:
            pass

        try:
            from pdfminer.high_level import extract_text
            return extract_text(str(pdf_path))
        except ImportError:
            pass

        # 最后回退：用 ollama 的多模态能力（如果支持）
        print(f"  [WARN] {pdf_path.name}: 无 PDF 解析库（需安装 PyMuPDF 或 pdfminer）")
        return ""

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

            try:
                self.client.insert(
                    collection_name=REGULATION_COLLECTION,
                    data=records,
                )
            except Exception as e:
                print(f"  [WARN] 批量插入失败 ({i}-{i+len(batch)}): {e}")
                # 逐条插入
                for record in records:
                    try:
                        self.client.insert(
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

        第 3 步的下限是必要的：语料里只要混进与本违规无关的条目（实测库中
        只有 3 条"军事禁区/禁渔区/港口限速"时，任何 AIS 违规查询都会返回它们），
        没有下限就会把无关法规当成检索结果写进 regulation_ref —— 看起来像查到了，
        实际是硬塞。宁可返回空，让调用方走兜底。

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
            anchored.sort(key=lambda a: -(a.get("similarity") or 0.0))
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
            # 法规条目自带 violation_codes 命中规范码的排前面（提高相关性）
            if code and code != "unknown":
                hits.sort(key=lambda h: 0 if code in (h.get("violation_codes") or []) else 1)
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
