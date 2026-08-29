"""把统一 Parquet 清洗产物原子构建为单文件 DuckDB 数据库。"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = ROOT / "output_excel_reclassified_clean"
DEFAULT_DB_PATH = ROOT / "nfra.duckdb"


def _sql_path(path: Path) -> str:
    return "'" + path.resolve().as_posix().replace("'", "''") + "'"


def build(input_dir: Path, db_path: Path, replace: bool = False) -> dict[str, int | str]:
    input_dir = input_dir.resolve()
    db_path = db_path.resolve()
    required = [
        input_dir / "source_catalog.parquet",
        input_dir / "facts",
        input_dir / "dictionaries",
        input_dir / "templates",
        input_dir / "references",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"缺少清洗产物：{missing}")
    if db_path.exists() and not replace:
        raise FileExistsError(f"数据库已存在；确认重建时请增加 --replace：{db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = db_path.with_name(f".{db_path.name}.{uuid.uuid4().hex}.tmp")

    catalog = _sql_path(input_dir / "source_catalog.parquet")
    facts = _sql_path(input_dir / "facts" / "*.parquet")
    documents = ", ".join(
        [_sql_path(input_dir / "templates" / "*.parquet"), _sql_path(input_dir / "references" / "*.parquet")]
    )
    dictionaries = input_dir / "dictionaries"

    try:
        connection = duckdb.connect(str(temporary))
        connection.execute(f"CREATE TABLE source_catalog AS SELECT * FROM read_parquet({catalog})")
        connection.execute(
            """
            CREATE TABLE sources AS
            SELECT *,
                CASE WHEN regexp_matches(source_file, '^[0-9]+_')
                     THEN split_part(source_file, '_', 2)
                     ELSE regexp_replace(source_file, '\\.[^.]+$', '') END AS source_title,
                CASE WHEN regexp_matches(source_file, '^[0-9]+_')
                     THEN regexp_extract(source_file, '^[0-9]+_[^_]+_(.*)$', 1)
                     ELSE NULL END AS attachment_name
            FROM source_catalog
            """
        )
        connection.execute(f"CREATE TABLE facts AS SELECT * FROM read_parquet({facts}, union_by_name=true)")
        connection.execute(
            f"CREATE TABLE documents AS SELECT * FROM read_parquet([{documents}], union_by_name=true)"
        )
        for table in ("metric_definitions", "institution_scopes", "release_schedule"):
            path = _sql_path(dictionaries / f"{table}.parquet")
            connection.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet({path})")

        connection.execute(
            """
            CREATE TABLE source_sheets AS
            WITH profiles AS (
                SELECT source_file, source_sheet, 'fact' AS content_kind,
                       count(*) AS record_count,
                       min(CAST(period_end AS VARCHAR)) AS period_start,
                       max(CAST(period_end AS VARCHAR)) AS period_end,
                       string_agg(DISTINCT coalesce(metric_name, metric_name_raw, ''), ' | ') AS profile_text
                FROM facts GROUP BY source_file, source_sheet
                UNION ALL
                SELECT source_file, source_sheet, 'document' AS content_kind,
                       count(*) AS record_count, NULL, NULL,
                       string_agg(row_text, ' | ' ORDER BY row_number) AS profile_text
                FROM documents GROUP BY source_file, source_sheet
                UNION ALL
                SELECT source_file, source_sheet, 'dictionary' AS content_kind,
                       count(*) AS record_count, NULL, NULL,
                       string_agg(metric_name || ' ' || definition, ' | ') AS profile_text
                FROM metric_definitions GROUP BY source_file, source_sheet
                UNION ALL
                SELECT source_file, source_sheet, 'dictionary' AS content_kind,
                       count(*) AS record_count, NULL, NULL,
                       string_agg(institution_type || ' ' || scope_definition, ' | ') AS profile_text
                FROM institution_scopes GROUP BY source_file, source_sheet
                UNION ALL
                SELECT source_file, source_sheet, 'dictionary' AS content_kind,
                       count(*) AS record_count, NULL, NULL,
                       string_agg(coalesce(indicator_names, '') || ' ' || coalesce(notes, ''), ' | ') AS profile_text
                FROM release_schedule GROUP BY source_file, source_sheet
            )
            SELECT source_file, source_sheet,
                   string_agg(DISTINCT content_kind, '|') AS content_kinds,
                   sum(record_count) AS record_count,
                   min(period_start) AS period_start,
                   max(period_end) AS period_end,
                   left(string_agg(profile_text, ' | '), 20000) AS profile_text
            FROM profiles
            GROUP BY source_file, source_sheet
            """
        )
        connection.execute(
            """
            CREATE TABLE document_chunks AS
            SELECT source_file, source_file_hash, source_format, source_sheet,
                   CAST(floor((row_number - 1) / 20) AS BIGINT) AS chunk_no,
                   min(row_number) AS start_row,
                   max(row_number) AS end_row,
                   arg_min(source_range, row_number) AS first_source_range,
                   arg_max(source_range, row_number) AS last_source_range,
                   string_agg('R' || CAST(row_number AS VARCHAR) || ': ' || row_text,
                              '\n' ORDER BY row_number) AS chunk_text
            FROM documents
            GROUP BY source_file, source_file_hash, source_format, source_sheet,
                     CAST(floor((row_number - 1) / 20) AS BIGINT)
            """
        )

        connection.execute("CREATE INDEX facts_source_idx ON facts(source_file)")
        connection.execute("CREATE INDEX facts_period_idx ON facts(period_end)")
        connection.execute("CREATE INDEX facts_metric_idx ON facts(metric_name)")
        connection.execute("CREATE INDEX source_sheets_source_idx ON source_sheets(source_file)")
        connection.execute("CREATE INDEX document_chunks_source_idx ON document_chunks(source_file)")
        summary_row = connection.execute(
            """
            SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM facts),
                   (SELECT count(*) FROM documents)
            """
        ).fetchone()
        connection.execute("CHECKPOINT")
        connection.close()
        os.replace(temporary, db_path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    summary = {
        "db_path": str(db_path),
        "source_count": int(summary_row[0]),
        "fact_count": int(summary_row[1]),
        "document_count": int(summary_row[2]),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="把清洗产物构建为单文件 DuckDB 数据库")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.input_dir, args.db, args.replace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
