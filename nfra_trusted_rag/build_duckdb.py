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
        input_dir / "ingestion_manifest.parquet",
        input_dir / "facts",
        input_dir / "dictionaries",
        input_dir / "references",
        input_dir / "template_schema",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"缺少清洗产物：{missing}")
    if db_path.exists() and not replace:
        raise FileExistsError(f"数据库已存在；确认重建时请增加 --replace：{db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = db_path.with_name(f".{db_path.name}.{uuid.uuid4().hex}.tmp")

    catalog = _sql_path(input_dir / "source_catalog.parquet")
    manifest = _sql_path(input_dir / "ingestion_manifest.parquet")
    facts = _sql_path(input_dir / "facts" / "*.parquet")
    documents = _sql_path(input_dir / "references" / "*.parquet")
    dictionaries = input_dir / "dictionaries"

    try:
        connection = duckdb.connect(str(temporary))
        connection.execute(f"CREATE TABLE source_catalog AS SELECT * FROM read_parquet({catalog})")
        connection.execute(
            f"CREATE TABLE ingestion_manifest AS SELECT * FROM read_parquet({manifest})"
        )
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
        connection.execute(
            f"CREATE TABLE raw_facts AS SELECT * FROM read_parquet({facts}, union_by_name=true)"
        )
        connection.execute(
            """
            CREATE TABLE facts AS
            SELECT * EXCLUDE(metric_code, metric_name, product_line,
                             product_line_name, region_type),
                   metric_code AS metric_code_source,
                   product_line AS product_line_source,
                   CASE
                       WHEN metric_code IN ('premium_total', 'premium_property',
                                            'premium_life', 'premium_accident',
                                            'premium_health')
                           THEN 'original_premium_income'
                       ELSE metric_code
                   END AS metric_code,
                   CASE
                       WHEN metric_code IN ('premium_total', 'premium_property',
                                            'premium_life', 'premium_accident',
                                            'premium_health')
                           THEN '原保险保费收入'
                       ELSE metric_name
                   END AS metric_name,
                   CASE
                       WHEN metric_code = 'premium_total' THEN 'all'
                       WHEN metric_code = 'premium_property' THEN 'property_insurance'
                       WHEN metric_code = 'premium_life' THEN 'life_insurance'
                       WHEN metric_code = 'premium_accident' THEN 'accident_insurance'
                       WHEN metric_code = 'premium_health' THEN 'health_insurance'
                       WHEN product_line = 'life' THEN 'life_insurance'
                       WHEN product_line = 'health' THEN 'health_insurance'
                       WHEN product_line = 'accident' THEN 'accident_insurance'
                       ELSE product_line
                   END AS product_line,
                   CASE
                       WHEN metric_code = 'premium_total' THEN '合计'
                       WHEN metric_code = 'premium_property' THEN '财产险'
                       WHEN metric_code = 'premium_life' OR product_line = 'life' THEN '寿险'
                       WHEN metric_code = 'premium_accident' OR product_line = 'accident'
                           THEN '意外险'
                       WHEN metric_code = 'premium_health' OR product_line = 'health'
                           THEN '健康险'
                       ELSE product_line_name
                   END AS product_line_name,
                   CASE
                       WHEN lower(coalesce(region_type, '')) IN
                            ('planned_city', 'city', '计划单列市') THEN 'planned_city'
                       WHEN lower(coalesce(region_type, '')) IN
                            ('province', '省级', '省') THEN 'province'
                       WHEN lower(coalesce(region_type, '')) IN
                            ('national', '全国') THEN 'national'
                       WHEN lower(coalesce(region_type, '')) IN
                            ('company_head_office', '公司本级') THEN 'company_head_office'
                       ELSE region_type
                   END AS region_type
            FROM raw_facts
            """
        )
        connection.execute("DROP TABLE raw_facts")
        connection.execute(
            f"CREATE TABLE documents AS SELECT * FROM read_parquet({documents}, union_by_name=true)"
        )
        for table in ("metric_definitions", "institution_scopes", "release_schedule"):
            path = _sql_path(dictionaries / f"{table}.parquet")
            connection.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet({path})")
        for table in (
            "template_catalog",
            "template_fields",
            "template_formulas",
            "template_validations",
        ):
            path = _sql_path(input_dir / "template_schema" / f"{table}.parquet")
            connection.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet({path})")

        connection.execute(
            """
            CREATE TABLE quality_events AS
            SELECT md5(source_file || '|' || coalesce(quality_status, '') || '|' ||
                       coalesce(quality_detail, '')) AS event_id,
                   source_file, dataset_family,
                   quality_severity AS severity,
                   quality_status AS event_type,
                   quality_detail AS detail,
                   quality_status = 'COMPARABILITY_CAVEAT' AS impacts_comparability,
                   quality_status = 'CUMULATIVE_DECREASE' AS requires_manual_review
            FROM source_catalog
            WHERE quality_severity <> 'PASS'
            """
        )

        connection.execute(
            """
            CREATE TABLE dataset_coverage AS
            WITH stats AS (
                SELECT dataset_family, frequency,
                       min(period_end) AS period_start,
                       max(period_end) AS period_end,
                       count(DISTINCT period_end) AS actual_period_count,
                       count(*) AS fact_count
                FROM facts
                WHERE frequency IN ('month', 'quarter')
                GROUP BY dataset_family, frequency
            )
            SELECT *,
                   CASE frequency
                       WHEN 'month' THEN date_diff('month', period_start, period_end) + 1
                       WHEN 'quarter' THEN date_diff('quarter', period_start, period_end) + 1
                   END AS expected_period_count,
                   expected_period_count - actual_period_count AS missing_period_count
            FROM stats
            """
        )
        connection.execute(
            """
            CREATE TABLE coverage_gaps AS
            WITH monthly_bounds AS (
                SELECT dataset_family,
                       date_trunc('month', min(period_end)) AS first_period,
                       date_trunc('month', max(period_end)) AS last_period
                FROM facts WHERE frequency = 'month' GROUP BY dataset_family
            ),
            monthly_expected AS (
                SELECT dataset_family, 'month' AS frequency,
                       last_day(CAST(period AS DATE)) AS period_end
                FROM monthly_bounds,
                     LATERAL generate_series(first_period, last_period, INTERVAL 1 MONTH) AS t(period)
            ),
            quarterly_bounds AS (
                SELECT dataset_family,
                       date_trunc('quarter', min(period_end)) AS first_period,
                       date_trunc('quarter', max(period_end)) AS last_period
                FROM facts WHERE frequency = 'quarter' GROUP BY dataset_family
            ),
            quarterly_expected AS (
                SELECT dataset_family, 'quarter' AS frequency,
                       last_day(CAST(period AS DATE) + INTERVAL 2 MONTH) AS period_end
                FROM quarterly_bounds,
                     LATERAL generate_series(first_period, last_period, INTERVAL 3 MONTH) AS t(period)
            ),
            expected AS (
                SELECT * FROM monthly_expected
                UNION ALL
                SELECT * FROM quarterly_expected
            ),
            actual AS (
                SELECT DISTINCT dataset_family, period_end FROM facts
            )
            SELECT expected.dataset_family, expected.frequency,
                   expected.period_end AS missing_period_end,
                   'missing_inside_available_range' AS gap_type
            FROM expected
            LEFT JOIN actual USING (dataset_family, period_end)
            WHERE actual.period_end IS NULL
            """
        )

        connection.execute(
            """
            CREATE TABLE source_sheets AS
            WITH profiles AS (
                SELECT source_file, source_sheet, 'fact' AS content_kind,
                       count(*) AS record_count,
                       min(CAST(period_end AS VARCHAR)) AS period_start,
                       max(CAST(period_end AS VARCHAR)) AS period_end,
                       string_agg(DISTINCT
                           coalesce(metric_name, metric_name_raw, '') || ' ' ||
                           coalesce(product_line_name, '') || ' ' ||
                           coalesce(region_type, ''), ' | ') AS profile_text
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
        connection.execute("CREATE INDEX facts_product_idx ON facts(product_line)")
        connection.execute("CREATE INDEX facts_region_type_idx ON facts(region_type)")
        connection.execute("CREATE INDEX source_sheets_source_idx ON source_sheets(source_file)")
        connection.execute("CREATE INDEX document_chunks_source_idx ON document_chunks(source_file)")
        connection.execute("CREATE INDEX manifest_source_idx ON ingestion_manifest(source_file)")
        connection.execute("CREATE INDEX quality_events_source_idx ON quality_events(source_file)")
        connection.execute("CREATE INDEX template_fields_source_idx ON template_fields(source_file)")
        summary_row = connection.execute(
            """
            SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM facts),
                   (SELECT count(*) FROM documents),
                   (SELECT count(*) FROM ingestion_manifest),
                   (SELECT count(*) FROM quality_events),
                   (SELECT count(*) FROM template_catalog),
                   (SELECT count(*) FROM coverage_gaps)
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
        "manifest_count": int(summary_row[3]),
        "quality_event_count": int(summary_row[4]),
        "template_count": int(summary_row[5]),
        "coverage_gap_count": int(summary_row[6]),
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
