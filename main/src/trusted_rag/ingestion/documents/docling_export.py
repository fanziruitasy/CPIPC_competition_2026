"""统一导出 DoclingDocument 的 JSON、Markdown、HTML 和引用资产。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from trusted_rag.infrastructure.artifacts import write_json_atomic


def export_docling_document(
    document: Any,
    *,
    document_name: str,
    output_dir: Path,
    image_mode_name: str,
    markdown_options: Mapping[str, Any],
    json_options: Mapping[str, Any] | None,
    html_options: Mapping[str, Any],
) -> None:
    """完整导出 DoclingDocument 并清除宿主机绝对资产路径。

    :param document: Docling 转换产生的 ``DoclingDocument``。
    :param document_name: 稳定文档名称。
    :param output_dir: 单文档临时输出目录。
    :param image_mode_name: Docling 图片引用模式名称。
    :param markdown_options: Markdown 导出附加参数。
    :param json_options: JSON 导出附加参数。
    :param html_options: HTML 导出附加参数。
    :return: 无。
    """
    from docling_core.types.doc.base import ImageRefMode

    document.name = document_name
    assets_dir = output_dir / f"{document_name}_artifacts"
    image_mode = ImageRefMode(image_mode_name)
    document.save_as_markdown(
        filename=output_dir / f"{document_name}.md",
        artifacts_dir=assets_dir,
        image_mode=image_mode,
        **dict(markdown_options),
    )
    json_path = output_dir / f"{document_name}.json"
    document.save_as_json(
        filename=json_path,
        artifacts_dir=assets_dir,
        image_mode=image_mode,
        **dict(json_options or {}),
    )
    document.save_as_html(
        filename=output_dir / f"{document_name}.html",
        artifacts_dir=assets_dir,
        image_mode=image_mode,
        **dict(html_options),
    )
    _normalize_json(json_path, output_dir)
    _normalize_text_references(output_dir / f"{document_name}.md", output_dir)
    _normalize_text_references(output_dir / f"{document_name}.html", output_dir)


def _normalize_json(path: Path, output_dir: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    write_json_atomic(path, _rewrite_absolute_uris(payload, output_dir))


def _normalize_text_references(path: Path, output_dir: Path) -> None:
    text = path.read_text(encoding="utf-8")
    rewritten = text.replace(str(output_dir) + "\\", "").replace(output_dir.as_posix() + "/", "")
    if path.suffix.lower() == ".html":
        pattern = re.compile(r'(?i)(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])')

        def replace_source(match: re.Match[str]) -> str:
            candidate = Path(unquote(match.group(2)))
            value = match.group(2)
            if candidate.is_absolute():
                with suppress(ValueError):
                    value = candidate.resolve().relative_to(output_dir.resolve()).as_posix()
            return match.group(1) + value + match.group(3)

        rewritten = pattern.sub(replace_source, rewritten)
    path.write_text(rewritten, encoding="utf-8", newline="\n")


def _rewrite_absolute_uris(value: Any, output_dir: Path) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if key == "uri" and isinstance(item, str):
                candidate = Path(item)
                if candidate.is_absolute():
                    with suppress(ValueError):
                        item = candidate.resolve().relative_to(output_dir.resolve()).as_posix()
            rewritten[key] = _rewrite_absolute_uris(item, output_dir)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_absolute_uris(item, output_dir) for item in value]
    return value
