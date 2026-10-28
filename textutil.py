"""文本处理小工具：分词、关键词、截断、文件名。

这个模块刻意不 import astrbot，方便离线测试，也方便以后脱离 AstrBot 复用。
优先使用 jieba；未安装时退化为正则切分，功能会弱一些，但不会崩。
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

try:  # pragma: no cover - 依赖是否安装由运行环境决定
    import jieba

    jieba.setLogLevel(20)
    _HAS_JIEBA = True
except Exception:  # noqa: BLE001
    jieba = None  # type: ignore[assignment]
    _HAS_JIEBA = False

_CJK = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_+\-.]*|[0-9]+|[{_CJK}]+")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s,，。.!！?？;；:：、'\"“”‘’()（）\[\]【】<>《》…—\-_/\\|@#$%^&*+=~`]+")

# 这些词做检索词几乎没有区分度；触发词由 retrieval 的意图正则单独处理。
STOPWORDS: set[str] = {
    "的", "了", "着", "过", "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "哈",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "自己",
    "这", "那", "这个", "那个", "这些", "那些", "这里", "那里", "这样", "那样",
    "什么", "怎么", "怎样", "为什么", "哪个", "哪里", "多少", "几",
    "是", "有", "在", "和", "与", "跟", "就", "都", "也", "还", "又", "很",
    "不", "没", "没有", "不是", "可以", "能", "会", "要", "想", "说", "讲",
    "一个", "一下", "一些", "一点", "现在", "今天", "昨天", "前天", "明天",
    "时候", "然后", "但是", "不过", "因为", "所以", "如果", "虽然", "而且",
    "已经", "还是", "就是", "其实", "真的", "感觉", "觉得", "知道", "认为",
    "帮", "帮我", "给我", "请问", "谢谢", "麻烦", "一下下",
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "or",
    "in", "on", "at", "for", "with", "that", "this", "it", "i", "you", "we",
}


def _clean_token(token: str) -> str:
    token = token.strip().lower()
    token = _PUNCT_RE.sub("", token)
    return token


def segment(text: str) -> list[str]:
    """切词，返回去停用词后的 token 列表（保留原顺序）。"""
    text = _WS_RE.sub(" ", text or "").strip()
    if not text:
        return []
    raw: Iterable[str]
    if _HAS_JIEBA:
        raw = jieba.cut_for_search(text)
    else:
        raw = []
        for m in _TOKEN_RE.finditer(text):
            chunk = m.group(0)
            if re.fullmatch(rf"[{_CJK}]+", chunk):
                if len(chunk) <= 2:
                    raw.append(chunk)
                else:
                    raw.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
            else:
                raw.append(chunk)
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = _clean_token(token)
        if not token or token in STOPWORDS or token in seen:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        seen.add(token)
        out.append(token)
    return out


def tokens_text(text: str) -> str:
    """给 FTS5 用的空格分隔 token 串。"""
    return " ".join(segment(text))


def extract_terms(text: str, max_terms: int = 8) -> list[str]:
    terms = segment(text)
    if not terms:
        return []
    ordered = sorted(enumerate(terms), key=lambda x: (-len(x[1]), x[0]))
    result: list[str] = []
    for _, term in ordered:
        if term in result:
            continue
        result.append(term)
        if len(result) >= max_terms:
            break
    return result


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """按句号/问号/换行截断，避免把一句话砍半。"""
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind(ch) for ch in "。！？!?\n；;")
    if cut >= max_chars * 0.5:
        return window[: cut + 1].strip()
    return window.rstrip() + "…"


def slugify(text: str, max_len: int = 40, fallback: str = "topic") -> str:
    """生成文件名安全的短 slug，保留中文。"""
    text = (text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    text = text[:max_len].strip("._-")
    if text:
        return text
    return hashlib.sha1((fallback or "topic").encode("utf-8")).hexdigest()[:8]


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:n]
