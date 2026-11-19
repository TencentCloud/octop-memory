"""Project archived conversation content into text for raw-event search."""

from __future__ import annotations

import json

IMAGE_BLOCK_TYPES = ("image", "image_url", "input_image")
IMAGE_PAYLOAD_FIELDS = ("base64", "data", "file_id", "image_url", "source", "url", "workspace_path")
IMAGE_PLACEHOLDERS = ("User sent an image.", "用户发送了图片。", "[image]")


def _is_image_block(line: str) -> bool:
    if not (line.startswith("{") and line.endswith("}")):
        return False
    try:
        block = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return (
        isinstance(block, dict)
        and block.get("type") in IMAGE_BLOCK_TYPES
        and any(field in block for field in IMAGE_PAYLOAD_FIELDS)
    )


def raw_search_text(content: str) -> str:
    """Exclude serialized image blocks and image-only placeholders from search."""
    if "{" not in content and "\n" not in content:
        text = content.strip()
        return "" if text in IMAGE_PLACEHOLDERS else text

    lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped not in IMAGE_PLACEHOLDERS and not _is_image_block(stripped):
            lines.append(line)
    return "\n".join(lines).strip()
