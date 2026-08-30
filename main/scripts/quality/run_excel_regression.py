"""执行 Excel 代表样本或全量转换、抽取、分块和质量回归。

功能：登记只读 XLS/XLSX，转换 XLS，生成统一事实、证据、说明分块和质量报告。
输入：版本化 YAML 配置；可通过 ``--mode`` 覆盖代表样本或全量模式。
输出：``data_runtime/ingestion_runs/<run_id>`` 下的转换、规范数据和报告。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml

MAIN_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = MAIN_ROOT.parent
sys.path.insert(0, str(MAIN_ROOT / "src"))

from trusted_rag.infrastructure.artifacts import sha256_file  # noqa: E402
from trusted_rag.ingestion.source_registry import SourceRegistrar  # noqa: E402
from trusted_rag.ingestion.spreadsheets.fact_extractor import (  # noqa: E402
    UnifiedSpreadsheetExtractor,
    write_facts_parquet,
)
from trusted_rag.ingestion.spreadsheets.libreoffice_converter import (  # noqa: E402
    LibreOfficeSpreadsheetConverter,
)
from trusted_rag.ingestion.spreadsheets.spreadsheet_chunker import (  # noqa: E402
    chunk_spreadsheet_facts,
)


def run_regression(
    config_path: Path,
    *,
    mode: Literal["representative", "full"] | None = None,
    run_id: str | None = None,
    check_only: bool = False,
    resume_failed: bool = False,
) -> dict[str, Any]:
    """执行一次隔离的 Excel 回归运行。

    :param config_path: 相对于 `main` 或绝对的 YAML 配置路径。
    :param mode: 可选运行模式覆盖值。
    :param run_id: 可选运行标识覆盖值，用于同配置创建独立运行。
    :param check_only: 只核验配置和输入，不创建运行目录。
    :param resume_failed: 复用既有运行目录，只重跑清单中的失败文件。
    :return: 文件覆盖、事实、证据、分块和失败数量摘要。
    :raises ValueError: 配置路径、运行模式或源目录不合法时抛出。
    :raises FileExistsError: 正式运行目录已经存在时抛出。
    """
    resolved_config = _resolve_under(MAIN_ROOT, config_path)
    config = yaml.safe_load(resolved_config.read_text(encoding="utf-8"))
    selected_mode = mode or str(config["mode"])
    selected_run_id = run_id or str(config["run_id"])
    if selected_mode not in {"representative", "full"}:
        raise ValueError("mode 必须是 representative 或 full。")
    source_root = _resolve_project_path(str(config["source_root"]))
    if not source_root.is_dir():
        raise FileNotFoundError(f"Excel 源目录不存在：{source_root}")
    files = sorted(
        path
        for path in source_root.iterdir()
        if path.is_file()
        and not path.name.startswith("~$")
        and path.suffix.lower() in {".xls", ".xlsx"}
    )
    if selected_mode == "representative":
        wanted = set(str(item) for item in config["representative_file_ids"])
        files = [path for path in files if path.name.partition("_")[0] in wanted]
    if not files:
        raise ValueError("当前模式没有选中 Excel 文件。")
    run_root = _resolve_under(
        MAIN_ROOT,
        Path(str(config["runtime_root"])) / selected_run_id,
    )
    summary: dict[str, Any] = {
        "run_id": selected_run_id,
        "mode": selected_mode,
        "source_count": len(files),
        "source_format_counts": dict(Counter(path.suffix.lower().lstrip(".") for path in files)),
        "run_root": run_root.relative_to(MAIN_ROOT).as_posix(),
    }
    if check_only:
        summary["check_only"] = True
        return summary
    if run_root.exists() and not resume_failed:
        raise FileExistsError(f"运行目录已存在，拒绝覆盖：{run_root}")
    if not run_root.exists() and resume_failed:
        raise FileNotFoundError(f"续跑目录不存在：{run_root}")
    if not resume_failed:
        for directory in (
            run_root / "converted",
            run_root / "work",
            run_root / "profile",
            run_root / "normalized",
            run_root / "quality",
            run_root / "logs",
        ):
            directory.mkdir(parents=True, exist_ok=False)

    converter = LibreOfficeSpreadsheetConverter(
        Path(str(config["soffice_path"])),
        export_filter=str(config["conversion"]["export_filter"]),
        timeout_seconds=config["conversion"]["timeout_seconds"],
    )
    libreoffice_version = converter.probe_version()
    registrar = SourceRegistrar(source_root)
    manifest_path = run_root / "manifest.jsonl"
    prior_manifest = _read_jsonl(manifest_path) if resume_failed else []
    records_by_file_id = {str(record["file_id"]): record for record in prior_manifest}
    if resume_failed:
        failed_ids = {
            file_id
            for file_id, record in records_by_file_id.items()
            if record.get("status") == "failed"
        }
        files_to_process = [
            path for path in files if path.name.partition("_")[0] in failed_ids
        ]
    else:
        files_to_process = files
    processed_source_ids: dict[str, str] = {
        str(record.get("conversion", {}).get("source_id")): str(record["file_id"])
        for record in prior_manifest
        if record.get("status") == "success"
        and record.get("conversion", {}).get("source_id")
    }
    for index, source_path in enumerate(files_to_process, start=1):
        before = sha256_file(source_path)
        record: dict[str, Any] = {
            "file_id": source_path.name.partition("_")[0],
            "original_file_name": source_path.name,
            "source_format": source_path.suffix.lower().lstrip("."),
            "source_sha256_before": before,
            "status": "failed",
            "error": None,
        }
        try:
            source = registrar.register(
                source_path,
                knowledge_base_id=str(config["knowledge_base_id"]),
                run_id=selected_run_id,
            )
            duplicate_file_id = processed_source_ids.get(source.source_id)
            if duplicate_file_id is not None:
                record.update(
                    {
                        "status": "success",
                        "deduplicated": True,
                        "deduplicated_from_file_id": duplicate_file_id,
                        "element_count": 0,
                        "fact_count": 0,
                        "evidence_count": 0,
                        "chunk_count": 0,
                        "requires_manual_review": False,
                    }
                )
                continue
            extraction_root = source_root
            extraction_source = source
            if source_path.suffix.lower() == ".xls":
                conversion = converter.convert(
                    source,
                    source_root=source_root,
                    converted_root=run_root / "converted",
                    work_root=run_root / "work",
                    profile_root=run_root / "profile",
                    run_id=selected_run_id,
                    libreoffice_version=libreoffice_version,
                )
                extraction_source = conversion.converted_source
                extraction_root = run_root
                record["conversion"] = conversion.model_dump(mode="json")
            result = UnifiedSpreadsheetExtractor(extraction_root).extract(
                extraction_source,
                run_id=selected_run_id,
            )
            chunking = chunk_spreadsheet_facts(
                result.document,
                result.elements,
                result.facts,
                result.evidence,
                max_facts_per_chunk=int(config["max_facts_per_chunk"]),
            )
            artifact_root = run_root / "normalized" / record["file_id"]
            if resume_failed and artifact_root.exists():
                resolved_artifact = artifact_root.resolve()
                resolved_artifact.relative_to((run_root / "normalized").resolve())
                shutil.rmtree(resolved_artifact)
            artifact_root.mkdir(parents=True, exist_ok=False)
            _write_json(artifact_root / "document.json", result.document.model_dump(mode="json"))
            _write_jsonl(artifact_root / "elements.jsonl", result.elements)
            _write_jsonl(artifact_root / "evidence.jsonl", result.evidence)
            _write_jsonl(artifact_root / "chunks.jsonl", chunking.chunks)
            write_facts_parquet(result.facts, artifact_root / "facts.parquet")
            record.update(
                {
                    "status": "success",
                    "element_count": len(result.elements),
                    "fact_count": len(result.facts),
                    "evidence_count": len(result.evidence),
                    "chunk_count": len(chunking.chunks),
                    "requires_manual_review": result.document.quality.requires_manual_review,
                }
            )
            processed_source_ids[source.source_id] = record["file_id"]
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            after = sha256_file(source_path)
            record["source_sha256_after"] = after
            record["source_unchanged"] = before == after
            records_by_file_id[record["file_id"]] = record
            manifest = [
                records_by_file_id[path.name.partition("_")[0]]
                for path in files
                if path.name.partition("_")[0] in records_by_file_id
            ]
            _write_jsonl(manifest_path, manifest)
            print(
                f"[{index}/{len(files_to_process)}] {record['file_id']} "
                f"{record['status']} {record['error'] or ''}",
                flush=True,
            )

    manifest = [
        records_by_file_id[path.name.partition("_")[0]]
        for path in files
        if path.name.partition("_")[0] in records_by_file_id
    ]
    _rebuild_all_facts_parquet(run_root / "normalized")
    status_counts = Counter(str(record["status"]) for record in manifest)
    totals: Counter[str] = Counter()
    for record in manifest:
        if record.get("status") == "success":
            totals.update(
                elements=int(record.get("element_count", 0)),
                facts=int(record.get("fact_count", 0)),
                evidence=int(record.get("evidence_count", 0)),
                chunks=int(record.get("chunk_count", 0)),
            )
    summary.update(
        {
            "status_counts": dict(status_counts),
            "all_sources_unchanged": all(bool(record["source_unchanged"]) for record in manifest),
            "element_count": totals["elements"],
            "fact_count": totals["facts"],
            "evidence_count": totals["evidence"],
            "chunk_count": totals["chunks"],
            "manual_review_file_count": sum(bool(record.get("requires_manual_review")) for record in manifest),
            "deduplicated_file_count": sum(bool(record.get("deduplicated")) for record in manifest),
            "failed_file_ids": [record["file_id"] for record in manifest if record["status"] == "failed"],
        }
    )
    _write_json(run_root / "quality" / "summary.json", summary)
    _write_report(run_root / "quality" / "report.md", summary, manifest)
    return summary


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (MAIN_ROOT / path).resolve()


def _resolve_under(root: Path, path: Path) -> Path:
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"路径必须位于 {root} 内：{resolved}") from exc
    return resolved


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, records: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        payload = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
        lines.append(json.dumps(payload, ensure_ascii=False, default=str))
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 UTF-8 JSONL 清单。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _rebuild_all_facts_parquet(normalized_root: Path) -> Path:
    """从每个成功文件的事实分区原子重建全量 Parquet。"""
    output_path = normalized_root / "all_facts.parquet"
    tables = [
        pq.read_table(path)
        for path in sorted(normalized_root.glob("*/facts.parquet"))
        if path.is_file()
    ]
    if not tables:
        return write_facts_parquet([], output_path)
    combined = pa.concat_tables(tables, promote_options="default")
    temporary_path = output_path.with_suffix(".parquet.tmp")
    pq.write_table(combined, temporary_path)
    os.replace(temporary_path, output_path)
    return output_path


def _write_report(path: Path, summary: dict[str, Any], manifest: Sequence[dict[str, Any]]) -> None:
    lines = [
        "# Excel 数据处理质量报告",
        "",
        f"- 运行标识：`{summary['run_id']}`",
        f"- 运行范围：`{summary['mode']}`",
        f"- 来源文件：{summary['source_count']}",
        f"- 成功/失败：{summary['status_counts']}",
        f"- 原文件全部未改变：{summary['all_sources_unchanged']}",
        f"- 标准事实：{summary['fact_count']}",
        f"- 单元格/行证据：{summary['evidence_count']}",
        f"- 检索分块：{summary['chunk_count']}",
        f"- 需人工复核文件：{summary['manual_review_file_count']}",
        f"- 内容去重文件：{summary['deduplicated_file_count']}",
        "",
        "## 文件结果",
        "",
    ]
    lines.extend(
        f"- `{record['file_id']}`：{record['status']}，facts={record.get('fact_count', 0)}，"
        f"chunks={record.get('chunk_count', 0)}，error={record.get('error') or '无'}"
        for record in manifest
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行并执行 Excel 回归。

    :param argv: 可选命令行参数；空值时读取当前进程参数。
    :return: 无失败返回0，否则返回2。
    """
    parser = argparse.ArgumentParser(description="执行 Excel 代表样本或全量质量回归")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/ingestion/spreadsheets/v0.01.yaml"),
    )
    parser.add_argument("--mode", choices=("representative", "full"))
    parser.add_argument("--run-id")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--resume-failed",
        action="store_true",
        help="复用既有运行目录，仅重跑清单中的失败文件。",
    )
    arguments = parser.parse_args(argv)
    summary = run_regression(
        arguments.config,
        mode=arguments.mode,
        run_id=arguments.run_id,
        check_only=arguments.check_only,
        resume_failed=arguments.resume_failed,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary.get("status_counts", {}).get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
