from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = ROOT / "data"
DEFAULT_OUTPUT_DIR = ROOT / "output_excel_reclassified_clean"

JOBS = [
    ("地区保费", "clean_region_premium.py", "output_region_premium_clean"),
    ("人身险", "clean_life_insurance.py", "output_life_insurance_clean"),
    ("财产险", "clean_property_insurance.py", "output_property_insurance_clean"),
    ("保险业汇总", "clean_insurance_industry.py", "output_insurance_industry_clean"),
    ("监管统计表", "clean_regulatory_tables.py", "output_regulatory_tables_clean"),
    ("模板与参考文档", "clean_excel_documents.py", "output_excel_documents_clean"),
]


def run_job(input_dir: Path, staging_root: Path, job: tuple[str, str, str], check_only: bool) -> None:
    label, script_name, output_name = job
    command = [
        sys.executable,
        str(ROOT / script_name),
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(staging_root / output_name),
    ]
    if check_only:
        command.append("--check-only")
    if script_name == "clean_excel_documents.py":
        command.append("--include-claimed-fallback")
    print(f"\n=== 解析：{label} ===", flush=True)
    subprocess.run(command, check=True)


def validate_output_target(output_dir: Path) -> None:
    try:
        output_dir.relative_to(ROOT)
    except ValueError as exc:
        raise SystemExit(f"输出目录必须位于项目目录内：{output_dir}") from exc
    forbidden = {ROOT, DEFAULT_INPUT_DIR}
    if output_dir in forbidden:
        raise SystemExit(f"拒绝使用危险输出目录：{output_dir}")


def validate_result(candidate: Path, input_dir: Path) -> dict:
    summary_path = candidate / "summary.json"
    catalog_path = candidate / "source_catalog.parquet"
    if not summary_path.exists() or not catalog_path.exists():
        raise RuntimeError("最终结果缺少 summary.json 或 source_catalog.parquet")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    source_count = sum(
        1
        for path in input_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".xls", ".xlsx"}
        and not path.name.startswith("~$")
    )
    if summary.get("classified_source_file_count") != source_count:
        raise RuntimeError(
            f"最终分类数量不一致：{summary.get('classified_source_file_count')} != {source_count}"
        )
    if summary.get("unclassified_source_file_count") or summary.get("duplicate_classification_count"):
        raise RuntimeError("最终结果存在漏分或重复分类")
    return summary


def build(input_dir: Path, output_dir: Path, replace_output: bool, check_only: bool) -> None:
    validate_output_target(output_dir)
    if not input_dir.is_dir():
        raise SystemExit(f"原始 Excel 目录不存在：{input_dir}")
    if output_dir.exists() and any(output_dir.iterdir()) and not replace_output:
        raise SystemExit(f"最新产物已存在；如需重跑请增加 --replace-output：{output_dir}")

    with TemporaryDirectory(prefix=".excel_clean_", dir=ROOT) as temporary:
        staging_root = Path(temporary)
        for job in JOBS:
            run_job(input_dir, staging_root, job, check_only)
        if check_only:
            print("\n全部底层解析检查通过；未生成产物。")
            return

        candidate = staging_root / "latest_output"
        command = [
            sys.executable,
            str(ROOT / "reclassify_excel_clean.py"),
            "--input-dir",
            str(input_dir),
            "--source-root",
            str(staging_root),
            "--output-dir",
            str(candidate),
        ]
        print("\n=== 按实际内容细分并生成最终产物 ===", flush=True)
        subprocess.run(command, check=True)
        summary = validate_result(candidate, input_dir)

        if output_dir.exists():
            shutil.rmtree(output_dir)
        shutil.move(str(candidate), str(output_dir))

    print(
        "\n清洗完成："
        f"{summary['classified_source_file_count']} 个 Excel，"
        f"{summary['dataset_family_count']} 个数据家族，"
        f"最终目录 {output_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从原始 Excel 一键解析、按实际内容细分，并且只保留最终清洗产物"
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--replace-output", action="store_true", help="成功生成新结果后替换旧的最终产物")
    parser.add_argument("--check-only", action="store_true", help="只检查底层解析，不生成产物")
    args = parser.parse_args()
    build(
        args.input_dir.resolve(),
        args.output_dir.resolve(),
        args.replace_output,
        args.check_only,
    )


if __name__ == "__main__":
    main()
