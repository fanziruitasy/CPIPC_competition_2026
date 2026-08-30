"""提供文件摘要、规范摘要和 UTF-8 原子产物写入能力。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from trusted_rag.domain.identity import canonical_sha256


def sha256_file(path: Path, *, block_size_bytes: int = 1024 * 1024) -> str:
    """以固定大小分块计算文件 SHA-256，不把整个文件载入内存。

    :param path: 待读取的普通文件。
    :param block_size_bytes: 每次读取的字节数，默认 1 MiB。
    :return: 六十四位小写十六进制 SHA-256。
    :raises ValueError: 分块大小不是正整数时抛出。
    :raises FileNotFoundError: 文件不存在时抛出。
    """
    if block_size_bytes <= 0:
        raise ValueError("block_size_bytes 必须是正整数。")
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(block_size_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_digest(payload: Any) -> str:
    """计算与映射插入顺序无关的规范数据摘要。

    :param payload: Pydantic 模型或可规范 JSON 化的数据。
    :return: 六十四位小写十六进制 SHA-256。
    """
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return canonical_sha256(payload)


def write_json_atomic(path: Path, payload: Any) -> None:
    """以 UTF-8 中文原样和原子替换方式写入 JSON。

    :param path: 最终 JSON 文件路径。
    :param payload: Pydantic 模型或可 JSON 序列化数据。
    :return: 无。
    """
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    _write_text_atomic(path, text)


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON 产物。

    :param path: JSON 文件路径。
    :return: 反序列化后的普通 Python 数据。
    :raises FileNotFoundError: 文件不存在时抛出。
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
