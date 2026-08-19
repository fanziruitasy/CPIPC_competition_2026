from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[%‰]+|[^\s]", re.UNICODE)
CJK_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]+|[%‰]+", re.UNICODE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    ensure_dir(path.parent)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_text(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\x00", "")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact_text(text: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(text)).strip()


def token_count(text: str) -> int:
    return len(TOKEN_RE.findall(text or ""))


def tokenize_for_index(text: str) -> list[str]:
    text = clean_text(text).lower()
    toks: list[str] = []
    for match in CJK_WORD_RE.finditer(text):
        value = match.group(0)
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", value):
            toks.extend(value[i : i + 2] for i in range(max(1, len(value) - 1)))
            if len(value) <= 8:
                toks.append(value)
        else:
            toks.append(value)
    return toks


def l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        return values
    return [v / norm for v in values]


def ref_index(ref: str | None) -> tuple[str, int] | None:
    if not ref:
        return None
    m = re.fullmatch(r"#/([A-Za-z_]+)/([0-9]+)", ref)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def safe_rel_uri(root: Path, path: Path, scheme: str) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
        value = rel.as_posix()
    except Exception:
        value = path.as_posix()
    return f"{scheme}://{value}"
