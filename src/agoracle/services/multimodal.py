"""
Multimodal message builder — converts Attachment objects into OpenAI-compatible
multimodal content arrays for vision-capable models.

v3.2: Initial implementation supporting images (base64) and documents (text extraction).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from agoracle.domain.types import Attachment

logger = logging.getLogger(__name__)

# Models known to support vision (image input)
VISION_MODELS = {
    "gpt4o", "gpt4o_mini",
    "claude_sonnet", "claude_opus_thinking",
    "gemini_31_pro_thinking", "gemini_3_flash",
}  # gpt52_thinking removed: model no longer in config (v5.2)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DOC_TYPES = {"application/pdf", "text/plain", "text/markdown",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def build_user_message(
    question: str,
    attachments: list[Attachment],
    model_id: str = "",
) -> dict[str, Any]:
    """
    Build a user message dict, optionally with multimodal content.

    - If no attachments: returns {"role": "user", "content": "question text"}
    - If image attachments + vision model: returns multimodal content array
    - If document attachments: extracts text and prepends to question
    """
    if not attachments:
        return {"role": "user", "content": question}

    # Separate images and documents
    images = [a for a in attachments if a.content_type in IMAGE_TYPES]
    docs = [a for a in attachments if a.content_type in DOC_TYPES]

    # Extract document text and prepend to question
    doc_text = ""
    for doc in docs:
        extracted = _extract_document_text(doc)
        if extracted:
            doc_text += f"\n\n--- 文档: {doc.filename} ---\n{extracted}\n"

    enriched_question = question
    if doc_text:
        enriched_question = f"{question}\n\n以下是用户上传的参考文档内容:{doc_text}"

    # If no images or model doesn't support vision, return text-only
    supports_vision = model_id in VISION_MODELS
    if not images or not supports_vision:
        return {"role": "user", "content": enriched_question}

    # Build multimodal content array (OpenAI format, compatible with Claude/Gemini)
    content: list[dict[str, Any]] = [
        {"type": "text", "text": enriched_question}
    ]

    for img in images[:4]:  # max 4 images
        b64 = _image_to_base64(img)
        if b64:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img.content_type};base64,{b64}",
                    "detail": "auto",
                }
            })

    return {"role": "user", "content": content}


def _image_to_base64(attachment: Attachment) -> str | None:
    """Read image file and return base64 string."""
    try:
        data = Path(attachment.file_path).read_bytes()
        if len(data) > 10 * 1024 * 1024:  # 10MB limit for base64
            logger.warning(f"Image too large for base64: {attachment.filename} ({len(data)} bytes)")
            return None
        return base64.b64encode(data).decode("ascii")
    except Exception as e:
        logger.error(f"Failed to read image {attachment.file_path}: {e}")
        return None


def _extract_document_text(attachment: Attachment) -> str:
    """Extract text content from a document attachment."""
    try:
        fp = Path(attachment.file_path)
        ct = attachment.content_type

        if ct in ("text/plain", "text/markdown"):
            text = fp.read_text(encoding="utf-8", errors="replace")
            return text[:50000]  # limit to ~50k chars

        if ct == "application/pdf":
            return _extract_pdf_text(fp)

        if ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return _extract_docx_text(fp)

        return ""
    except Exception as e:
        logger.error(f"Failed to extract text from {attachment.filename}: {e}")
        return ""


def _extract_pdf_text(path: Path) -> str:
    """Extract text from PDF using PyMuPDF (fitz) if available, else fallback."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        text = ""
        for page in doc:
            text += page.get_text()
            if len(text) > 50000:
                break
        doc.close()
        return text[:50000]
    except ImportError:
        logger.warning("PyMuPDF not installed — PDF text extraction unavailable (pip install PyMuPDF)")
        return "[PDF文件已上传，但服务器未安装PDF解析库。请安装: pip install PyMuPDF]"
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


def _extract_docx_text(path: Path) -> str:
    """Extract text from DOCX using python-docx if available."""
    try:
        import docx
        doc = docx.Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs)
        return text[:50000]
    except ImportError:
        logger.warning("python-docx not installed — DOCX text extraction unavailable (pip install python-docx)")
        return "[DOCX文件已上传，但服务器未安装DOCX解析库。请安装: pip install python-docx]"
    except Exception as e:
        logger.error(f"DOCX extraction failed: {e}")
        return ""
