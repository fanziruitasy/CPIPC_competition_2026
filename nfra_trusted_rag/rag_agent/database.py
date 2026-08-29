"""DuckDB 数据访问层。运行时只读取单个 .duckdb 文件。"""
from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any

import duckdb


FACT_FILTERS: dict[str, tuple[str, ...]] = {
    "metric": ("metric_code", "metric_name", "metric_name_raw"),
    "entity": ("entity_code", "entity_name"),
    "region": ("region_code", "region_name"),
    "period_basis": ("period_basis",),
    "scope": ("scope",),
    "product_line": ("product_line", "product_line_name"),
    "measure_type": ("measure_type",),
    "unit": ("unit",),
}


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

    def fetch_all(self, sql: str, parameters: list[Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._connection.execute(sql, parameters or [])
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def fetch_one(self, sql: str, parameters: list[Any] | None = None) -> dict[str, Any] | None:
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
        return {"db_path": str(self.db_path), **(row or {})}

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
        merged: dict[str, dict[str, Any]] = {}
        for row in [*fact_rows, *document_rows]:
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

    def source_candidates(self, filters: dict[str, str]) -> list[str]:
        """Return files that actually contain facts satisfying hard constraints."""
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
        if filters.get("period_end"):
            clauses.append("CAST(f.period_end AS VARCHAR) = ?")
            parameters.append(filters["period_end"])
        if filters.get("start_period"):
            clauses.append("CAST(f.period_end AS VARCHAR) >= ?")
            parameters.append(filters["start_period"])
        if filters.get("end_period"):
            clauses.append("CAST(f.period_end AS VARCHAR) <= ?")
            parameters.append(filters["end_period"])
        for field, value in filters.items():
            if field in {
                "period_end", "from_period", "to_period", "start_period", "end_period"
            }:
                continue
            columns = FACT_FILTERS.get(field)
            if not columns:
                continue
            clauses.append(
                "(" + " OR ".join(
                    f"lower(CAST(f.{column} AS VARCHAR)) = lower(?)" for column in columns
                ) + ")"
            )
            parameters.extend([value] * len(columns))
        if not clauses:
            return []
        having = ""
        if len(point_periods) == 2:
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
        filters = filters or {}
        periods = [filters[key] for key in ("from_period", "to_period") if filters.get(key)]
        if periods:
            clauses.append("CAST(f.period_end AS VARCHAR) IN (" + ",".join("?" for _ in periods) + ")")
            parameters.extend(periods)
        if filters.get("period_end"):
            clauses.append("CAST(f.period_end AS VARCHAR) = ?")
            parameters.append(filters["period_end"])
        if filters.get("start_period"):
            clauses.append("CAST(f.period_end AS VARCHAR) >= ?")
            parameters.append(filters["start_period"])
        if filters.get("end_period"):
            clauses.append("CAST(f.period_end AS VARCHAR) <= ?")
            parameters.append(filters["end_period"])
        for field, value in filters.items():
            if field in {"period_end", "from_period", "to_period", "start_period", "end_period"}:
                continue
            columns = FACT_FILTERS.get(field)
            if not columns:
                raise ValueError(f"不支持的事实过滤字段：{field}")
            clauses.append(
                "(" + " OR ".join(f"lower(CAST(f.{column} AS VARCHAR)) = lower(?)" for column in columns) + ")"
            )
            parameters.extend([value] * len(columns))
        return self.fetch_all(
            """
            SELECT f.*, s.source_title, s.attachment_name,
                   s.quality_status AS source_quality_status,
                   s.quality_detail AS source_quality_detail
            FROM facts f
            JOIN sources s ON s.source_file = f.source_file
            WHERE """ + " AND ".join(clauses) + " ORDER BY f.source_sheet, f.source_cell",
            parameters,
        )

    def dictionary_rows(self, table: str, file_name: str) -> list[dict[str, Any]]:
        if table not in {"metric_definitions", "institution_scopes", "release_schedule"}:
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

    @staticmethod
    def _normalize(value: Any) -> str:
        return "".join(
            char.lower()
            for char in str(value or "")
            if char not in " \t\r\n-_/（）()《》“”‘’：:，,。.;；"
        )

    def metric_definitions(
        self, file_name: str, metric_name: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.dictionary_rows("metric_definitions", file_name)
        if metric_name:
            expected = self._normalize(metric_name)
            rows = [
                row for row in rows
                if expected in self._normalize(row.get("metric_name"))
            ]
        return rows

    def institution_scopes(
        self, file_name: str, institution: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.dictionary_rows("institution_scopes", file_name)
        if institution:
            expected = self._normalize(institution)
            rows = [
                row for row in rows
                if expected in self._normalize(row.get("institution_type"))
            ]
        return rows

    def release_schedules(self, file_name: str) -> list[dict[str, Any]]:
        return self.dictionary_rows("release_schedule", file_name)

    def document_rows(self, file_name: str, sheet_name: str | None = None) -> list[dict[str, Any]]:
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
            WHERE """ + " AND ".join(clauses) + " ORDER BY d.source_sheet, d.row_number",
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
            WHERE """ + " AND ".join(clauses),
            parameters,
        )

    def audit(self, *args: Any, **kwargs: Any) -> None:
        """只读单文件部署不落查询审计。"""
        return None
