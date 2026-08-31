"""DuckDB 数据访问层。运行时只读取单个 .duckdb 文件。"""

from __future__ import annotations

import re
from pathlib import Path
from threading import RLock
from typing import Any

import duckdb

from query_schema import FACT_FILTER_FIELDS, PERIOD_FILTERS, normalize


def infer_entity_level(row: dict[str, Any]) -> str:
    """Infer whether an entity row is an aggregate, category, or institution.

    Newer cleaners can provide an explicit ``entity_type``.  Older canonical
    rows still carry stable semantic entity codes, whose plural/total form lets
    us recover the level without depending on a particular workbook title.
    Unknown values stay unknown so they can never be used as hard negative
    evidence.
    """

    entity_type = normalize(row.get("entity_type"))
    entity_name = normalize(row.get("entity_name"))
    entity_code = str(row.get("entity_code") or "").strip().lower()
    if "aggregate" in entity_type or entity_code.endswith("_total"):
        return "industry_aggregate"
    if any(marker in entity_name for marker in (normalize("汇总"), normalize("合计"))):
        return "industry_aggregate"
    if entity_type in {"institution", "company", "bank", "insurer"}:
        return "institution"
    if re.search(
        r"(?:^|_)(?:banks|insurers|institutions|companies)(?:_|$)", entity_code
    ):
        return "category"
    if entity_code:
        return "institution"
    return "unknown"


class DuckDBRepository:
    """线程安全的只读 DuckDB repository。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"DuckDB 数据库不存在：{self.db_path}；请先运行 "
                "python build_duckdb.py --replace"
            )
        self._connection = duckdb.connect(str(self.db_path), read_only=True)
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def fetch_all(
        self, sql: str, parameters: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._connection.execute(sql, parameters or [])
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def fetch_one(
        self, sql: str, parameters: list[Any] | None = None
    ) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, parameters)
        return rows[0] if rows else None

    def summarize(self) -> dict[str, Any]:
        row = self.fetch_one(
            """
            SELECT
                (SELECT count(*) FROM source_catalog) AS source_count,
                (SELECT count(*) FROM facts) AS fact_count,
                (SELECT count(*) FROM documents) AS document_count
            """
        )
        summary = {"db_path": str(self.db_path), **(row or {})}
        optional_counts = {
            "manifest_count": "ingestion_manifest",
            "quality_event_count": "quality_events",
            "template_count": "template_catalog",
            "template_field_count": "template_fields",
            "template_formula_pattern_count": "template_formulas",
            "coverage_gap_count": "coverage_gaps",
        }
        for output_name, table_name in optional_counts.items():
            exists = self.fetch_one(
                """
                SELECT count(*) AS n FROM information_schema.tables
                WHERE table_schema = 'main' AND table_name = ?
                """,
                [table_name],
            )
            if exists and exists["n"]:
                count = self.fetch_one(f'SELECT count(*) AS n FROM "{table_name}"')
                summary[output_name] = count["n"] if count else 0
        return summary

    def list_sources(self) -> list[dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT source_file AS file_name, source_title, attachment_name,
                   dataset_family, domain_name, topic_name, content_type_code,
                   document_function, frequency, evidence_summary,
                   source_sheet_names, period_start, period_end,
                   quality_severity, quality_status, quality_detail
            FROM sources
            ORDER BY source_file
            """
        )

    def sheet_profiles(self, file_name: str) -> list[dict[str, Any]]:
        """Build lightweight per-sheet profiles from the current database schema.

        New databases may persist the same information in ``source_sheets``;
        deriving it here keeps older single-file deployments compatible.
        """
        persisted = self.fetch_one(
            """
            SELECT count(*) AS n
            FROM information_schema.tables
            WHERE table_name = 'source_sheets'
            """
        )
        if persisted and int(persisted.get("n") or 0):
            return self.fetch_all(
                """
                SELECT source_sheet, content_kinds AS content_kind,
                       profile_text AS content, period_start, period_end, record_count
                FROM source_sheets
                WHERE source_file = ?
                ORDER BY source_sheet
                """,
                [file_name],
            )

        fact_rows = self.fetch_all(
            """
            SELECT source_sheet,
                   'fact' AS content_kind,
                   string_agg(DISTINCT coalesce(metric_name, metric_name_raw, ''), ' | ') AS content,
                   min(CAST(period_end AS VARCHAR)) AS period_start,
                   max(CAST(period_end AS VARCHAR)) AS period_end,
                   count(*) AS record_count
            FROM facts
            WHERE source_file = ?
            GROUP BY source_sheet
            """,
            [file_name],
        )
        document_rows = self.fetch_all(
            """
            SELECT source_sheet,
                   'document' AS content_kind,
                   string_agg(row_text, ' | ' ORDER BY row_number) AS content,
                   NULL AS period_start,
                   NULL AS period_end,
                   count(*) AS record_count
            FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY source_file, source_sheet ORDER BY row_number
                ) AS profile_row
                FROM documents
                WHERE source_file = ?
            ) d
            WHERE profile_row <= 40
            GROUP BY source_sheet
            """,
            [file_name],
        )
        dictionary_rows = self.fetch_all(
            """
            SELECT source_sheet, 'dictionary' AS content_kind,
                   string_agg(profile_text, ' | ' ORDER BY row_order) AS content,
                   NULL AS period_start, NULL AS period_end,
                   count(*) AS record_count
            FROM (
                SELECT source_sheet, row_number() OVER () AS row_order,
                       coalesce(metric_name, '') || ' ' || coalesce(definition, '') AS profile_text
                FROM metric_definitions WHERE source_file = ?
                UNION ALL
                SELECT source_sheet, row_number() OVER () AS row_order,
                       coalesce(institution_type, '') || ' ' || coalesce(scope_definition, '') AS profile_text
                FROM institution_scopes WHERE source_file = ?
                UNION ALL
                SELECT source_sheet, row_order,
                       coalesce(indicator_names, '') || ' ' ||
                       coalesce(release_timing, '') || ' ' || coalesce(notes, '') AS profile_text
                FROM release_schedule WHERE source_file = ?
            ) dictionary_content
            GROUP BY source_sheet
            """,
            [file_name, file_name, file_name],
        )
        merged: dict[str, dict[str, Any]] = {}
        for row in [*fact_rows, *document_rows, *dictionary_rows]:
            key = str(row.get("source_sheet") or "")
            item = merged.setdefault(
                key,
                {
                    "source_sheet": key,
                    "content_kind": set(),
                    "content": [],
                    "period_start": None,
                    "period_end": None,
                    "record_count": 0,
                },
            )
            item["content_kind"].add(str(row.get("content_kind") or ""))
            if row.get("content"):
                item["content"].append(str(row["content"]))
            item["period_start"] = item["period_start"] or row.get("period_start")
            item["period_end"] = item["period_end"] or row.get("period_end")
            item["record_count"] += int(row.get("record_count") or 0)
        return [
            {
                **item,
                "content_kind": "|".join(sorted(item["content_kind"])),
                "content": " | ".join(item["content"])[:12000],
            }
            for item in merged.values()
        ]

    def source_capabilities(
        self, file_name: str, sheet_name: str | None = None
    ) -> list[dict[str, Any]]:
        """Return exhaustive structured coverage, not merely top-k matches."""
        clauses = ["f.source_file = ?"]
        parameters: list[Any] = [file_name]
        if sheet_name:
            clauses.append(
                "(lower(f.source_sheet) = lower(?) "
                "OR contains(lower(f.source_sheet), lower(?)) "
                "OR contains(lower(?), lower(f.source_sheet)))"
            )
            parameters.extend([sheet_name, sheet_name, sheet_name])
        fact_columns = {
            str(row.get("column_name") or "")
            for row in self.fetch_all("DESCRIBE facts")
        }
        entity_code_sql = (
            "f.entity_code" if "entity_code" in fact_columns else "NULL AS entity_code"
        )
        footnotes_sql = (
            "f.footnotes" if "footnotes" in fact_columns else "NULL AS footnotes"
        )
        rows = self.fetch_all(
            f"""
            SELECT f.source_sheet, f.metric_name, f.metric_name_raw,
                   f.entity_name, f.entity_type, {entity_code_sql},
                   f.region_name, f.region_type, f.product_line_name,
                   f.measure_type, f.period_basis, f.period_end, f.unit,
                   {footnotes_sql}
            FROM facts f
            WHERE """
            + " AND ".join(clauses),
            parameters,
        )
        if not rows:
            return []
        source = self.fetch_one(
            "SELECT source_title FROM sources WHERE source_file = ?", [file_name]
        ) or {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("source_sheet") or ""), []).append(row)

        def distinct_values(
            items: list[dict[str, Any]], *fields: str, limit: int = 120
        ) -> tuple[list[str], int, bool]:
            values = sorted(
                {
                    str(item.get(field)).strip()
                    for item in items
                    for field in fields
                    if item.get(field) not in (None, "")
                    and str(item.get(field)).strip()
                }
            )
            return values[:limit], len(values), len(values) <= limit

        profiles: list[dict[str, Any]] = []
        for source_sheet, items in sorted(grouped.items()):
            metrics, metric_count, metrics_complete = distinct_values(
                items, "metric_name", "metric_name_raw"
            )
            entities, entity_count, entities_complete = distinct_values(
                items, "entity_name"
            )
            entity_types, entity_type_count, entity_types_complete = distinct_values(
                items, "entity_type"
            )
            entity_levels = sorted(
                {
                    infer_entity_level(item)
                    for item in items
                    if infer_entity_level(item) != "unknown"
                }
            )
            entity_levels_complete = all(
                infer_entity_level(item) != "unknown"
                for item in items
                if item.get("entity_name") not in (None, "")
            )
            regions, region_count, regions_complete = distinct_values(
                items, "region_name"
            )
            region_types, region_type_count, region_types_complete = distinct_values(
                items, "region_type"
            )
            products, product_count, products_complete = distinct_values(
                items, "product_line_name"
            )
            measure_types, _, _ = distinct_values(items, "measure_type")
            period_bases, _, _ = distinct_values(items, "period_basis")
            units, _, _ = distinct_values(items, "unit")
            footnotes, _, _ = distinct_values(items, "footnotes", limit=20)
            periods = sorted(
                {
                    str(item.get("period_end"))
                    for item in items
                    if item.get("period_end") not in (None, "")
                }
            )
            profiles.append(
                {
                    "evidence_kind": "source_capability_profile",
                    "coverage_scope": "all_structured_facts_in_sheet",
                    "source_title": source.get("source_title"),
                    "source_sheet": source_sheet,
                    "record_count": len(items),
                    "period_start": periods[0] if periods else None,
                    "period_end": periods[-1] if periods else None,
                    "available_periods": periods,
                    "available_metrics": metrics,
                    "available_entities": entities,
                    "available_entity_types": entity_types,
                    "available_entity_levels": entity_levels,
                    "available_regions": regions,
                    "available_region_types": region_types,
                    "available_product_lines": products,
                    "available_measure_types": measure_types,
                    "available_period_bases": period_bases,
                    "available_units": units,
                    "footnotes": footnotes,
                    "dimension_cardinality": {
                        "metrics": metric_count,
                        "entities": entity_count,
                        "entity_types": entity_type_count,
                        "entity_levels": len(entity_levels),
                        "regions": region_count,
                        "region_types": region_type_count,
                        "product_lines": product_count,
                    },
                    "listed_values_complete": {
                        "metrics": metrics_complete,
                        "entities": entities_complete,
                        "entity_types": entity_types_complete,
                        "entity_levels": entity_levels_complete,
                        "regions": regions_complete,
                        "region_types": region_types_complete,
                        "product_lines": products_complete,
                    },
                }
            )
        return profiles

    @staticmethod
    def _fact_filters(filters: dict[str, str]) -> tuple[list[str], list[Any], int]:
        clauses: list[str] = []
        parameters: list[Any] = []
        point_periods = [
            filters[key] for key in ("from_period", "to_period") if filters.get(key)
        ]
        if point_periods:
            clauses.append(
                "CAST(f.period_end AS VARCHAR) IN ("
                + ",".join("?" for _ in point_periods)
                + ")"
            )
            parameters.extend(point_periods)
        for field, operator in (
            ("period_end", "="),
            ("start_period", ">="),
            ("end_period", "<="),
        ):
            if filters.get(field):
                clauses.append(f"CAST(f.period_end AS VARCHAR) {operator} ?")
                parameters.append(filters[field])
        for field, value in filters.items():
            if field in PERIOD_FILTERS:
                continue
            columns = FACT_FILTER_FIELDS.get(field)
            if not columns:
                raise ValueError(f"不支持的事实过滤字段：{field}")
            clauses.append(
                "("
                + " OR ".join(
                    f"lower(CAST(f.{column} AS VARCHAR)) = lower(?)"
                    for column in columns
                )
                + ")"
            )
            parameters.extend([value] * len(columns))
        return clauses, parameters, len(point_periods)

    def source_candidates(self, filters: dict[str, str]) -> list[str]:
        """Return files that actually contain facts satisfying hard constraints."""
        clauses, parameters, point_period_count = self._fact_filters(filters)
        if not clauses:
            return []
        having = ""
        if point_period_count == 2:
            having = " HAVING count(DISTINCT CAST(f.period_end AS VARCHAR)) = 2"
        rows = self.fetch_all(
            "SELECT f.source_file AS file_name FROM facts f WHERE "
            + " AND ".join(clauses)
            + " GROUP BY f.source_file"
            + having,
            parameters,
        )
        return [str(row["file_name"]) for row in rows]

    def facts_for_source(
        self,
        file_name: str,
        sheet_name: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["f.source_file = ?"]
        parameters: list[Any] = [file_name]
        if sheet_name:
            clauses.append(
                "(lower(f.source_sheet) = lower(?) "
                "OR contains(lower(f.source_sheet), lower(?)) "
                "OR contains(lower(?), lower(f.source_sheet)))"
            )
            parameters.extend([sheet_name, sheet_name, sheet_name])
        filter_clauses, filter_parameters, _ = self._fact_filters(filters or {})
        clauses.extend(filter_clauses)
        parameters.extend(filter_parameters)
        return self.fetch_all(
            """
            SELECT f.*, s.source_title, s.attachment_name,
                   s.quality_status AS source_quality_status,
                   s.quality_detail AS source_quality_detail
            FROM facts f
            JOIN sources s ON s.source_file = f.source_file
            WHERE """
            + " AND ".join(clauses)
            + " ORDER BY f.source_sheet, f.source_cell",
            parameters,
        )

    def dictionary_rows(self, table: str, file_name: str) -> list[dict[str, Any]]:
        if table not in {
            "metric_definitions",
            "institution_scopes",
            "release_schedule",
        }:
            raise ValueError(f"未知字典表：{table}")
        return self.fetch_all(
            f"""
            SELECT d.*, s.source_title, s.attachment_name
            FROM {table} d
            JOIN sources s ON s.source_file = d.source_file
            WHERE d.source_file = ?
            """,
            [file_name],
        )

    def metric_definitions(
        self, file_name: str, metric_name: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.dictionary_rows("metric_definitions", file_name)
        if metric_name:
            expected = normalize(metric_name)
            rows = [
                row for row in rows if expected in normalize(row.get("metric_name"))
            ]
        return rows

    def institution_scopes(
        self, file_name: str, institution: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.dictionary_rows("institution_scopes", file_name)
        if institution:
            expected = normalize(institution)
            rows = [
                row
                for row in rows
                if expected in normalize(row.get("institution_type"))
            ]
        return rows

    def release_schedules(self, file_name: str) -> list[dict[str, Any]]:
        return self.dictionary_rows("release_schedule", file_name)

    def document_rows(
        self, file_name: str, sheet_name: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["d.source_file = ?"]
        parameters: list[Any] = [file_name]
        if sheet_name:
            clauses.append("contains(lower(d.source_sheet), lower(?))")
            parameters.append(sheet_name)
        return self.fetch_all(
            """
            SELECT d.*, s.source_title, s.attachment_name
            FROM documents d
            JOIN sources s ON s.source_file = d.source_file
            WHERE """
            + " AND ".join(clauses)
            + " ORDER BY d.source_sheet, d.row_number",
            parameters,
        )

    def metadata_facts(
        self,
        domain_name: str,
        topic_name: str,
        period_end: str,
        metric_name: str,
        dimension: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "lower(f.domain_name) = lower(?)",
            "lower(f.topic_name) = lower(?)",
            "CAST(f.period_end AS VARCHAR) = ?",
            "lower(f.metric_name) = lower(?)",
        ]
        parameters: list[Any] = [domain_name, topic_name, period_end, metric_name]
        if dimension:
            clauses.append(
                "(lower(f.entity_name) = lower(?) OR lower(f.region_name) = lower(?))"
            )
            parameters.extend([dimension, dimension])
        return self.fetch_all(
            """
            SELECT f.*, s.source_title, s.attachment_name,
                   s.quality_status AS source_quality_status,
                   s.quality_detail AS source_quality_detail
            FROM facts f
            JOIN sources s ON s.source_file = f.source_file
            WHERE """
            + " AND ".join(clauses),
            parameters,
        )

    def audit(self, *args: Any, **kwargs: Any) -> None:
        """只读单文件部署不落查询审计。"""
        return None
