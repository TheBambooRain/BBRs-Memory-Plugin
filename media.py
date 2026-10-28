"""媒体接口（v0.1 只留协议与占位实现）。

v0.2 要做的事：
- 语音：下载 → STT → 写 transcript；
- 表情包/图片：下载 → 视觉模型 → caption / display_name；
- 文件仍按 sha256 存，display_name 只用于展示。

v0.1 不改原文，只在渲染时把附件标记拼进文本；这样 v0.2 补上转写后，
重新渲染即可，历史消息和已提炼话题不会因为“文本被替换过”而不一致。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

KIND_LABELS = {
    "voice": "语音",
    "sticker": "表情包",
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "face": "表情",
}


@dataclass
class AttachmentRef:
    """从平台消息里抽出的附件引用；v0.1 不下载。"""

    id: str
    kind: str
    platform_ref: str = ""
    mime: str = ""
    size: int = 0
    sha256: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def make_attachment_id(umo: str, message_uid: str, index: int, ref: str) -> str:
    seed = f"{umo}|{message_uid}|{index}|{ref}"
    return hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]


def sanitize_display_name(name: str, ext: str = "jpg", max_len: int = 40) -> str:
    """把模型给的描述变成安全文件名，例如“初音未来_开心.jpg”。"""
    name = (name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._- ")
    name = name[:max_len].strip("._- ")
    if not name:
        name = "media"
    ext = (ext or "jpg").lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{2,5}", ext):
        ext = "jpg"
    return f"{name}.{ext}"


def render_message_text(
    content: str,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """把一条消息渲染成可以送去提炼/展示的文本。

    v0.1：只有 `[语音]`、`[表情包]` 这类占位；
    v0.2：自动带上 transcript / caption / display_name。
    """
    content = (content or "").strip()
    parts: list[str] = []
    for att in attachments or []:
        kind = str(att.get("kind") or "file")
        label = KIND_LABELS.get(kind, "附件")
        att_id = str(att.get("id") or "")[:12]
        suffix = f"#{att_id}" if att_id else ""
        if kind == "voice":
            text = f"[语音{suffix}]"
            transcript = str(att.get("transcript") or "").strip()
            if transcript:
                text += f" 转写：{transcript}"
        elif kind in {"sticker", "face"}:
            display = str(
                att.get("display_name") or att.get("caption") or "",
            ).strip()
            text = f"[{label}:{display}{suffix}]" if display else f"[{label}{suffix}]"
        else:
            text = f"[{label}{suffix}]"
            caption = str(att.get("caption") or "").strip()
            if caption:
                text += f" 描述：{caption}"
        parts.append(text)
    if content and parts:
        return content + " " + " ".join(parts)
    return content or " ".join(parts)


@runtime_checkable
class MediaStore(Protocol):
    """v0.2 实现这个协议即可接入，不需要改存储层。"""

    async def save(self, att: AttachmentRef, data: bytes) -> str:
        """保存原始媒体，返回相对路径。"""
        ...

    async def transcribe(self, att: AttachmentRef, path: str) -> str:
        """语音转文字。"""
        ...

    async def describe(self, att: AttachmentRef, path: str) -> dict[str, str]:
        """图片/表情包描述，返回 caption / display_name 等。"""
        ...


class NoopMediaStore:
    """v0.1 的默认实现：只记录引用，不下载、不调模型。"""

    def __init__(self, media_dir: str | Path):
        self.media_dir = Path(media_dir)

    async def save(self, att: AttachmentRef, data: bytes) -> str:
        return ""

    async def transcribe(self, att: AttachmentRef, path: str) -> str:
        return ""

    async def describe(self, att: AttachmentRef, path: str) -> dict[str, str]:
        return {}
