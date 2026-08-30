"""使用不可变 DuckDB 文件保存并参数化查询统一表格事实。"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from trusted_rag.domain.common import LineageMetadata, QualityMetadata, SourceLocation
from trusted_rag.domain.enums import EvidenceType, QualityStatus, StructuredOperationType
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.knowledge import EvidenceUnit, TableFact
from trusted_rag.domain.ports import SnapshotReference
from trusted_rag.domain.query import QueryPlan, StructuredOperation


class DuckDbFactStore:
    """以白名单字段和参数化 SQL 实现统一事实库存储端口。"""

    def __init__(self, database_root: Path) -> None:
        """初始化 DuckDB 文件根目录。

        :param database_root: 快照 DuckDB 文件允许写入和读取的根目录。
        :return: 无。
        """
        self.database_root = database_root.resolve()

    def replace_snapshot(
        self,
        snapshot: SnapshotReference,
        facts: Sequence[TableFact],
    ) -> None:
        """原子创建不可变事实快照；已经存在时拒绝覆盖。

        :param snapshot: 目标知识库快照及相对 DuckDB URI。
        :param facts: 已通过质量门禁的完整事实集合。
        :return: 无。
        :raises ValueError: URI 逃逸根目录时抛出。
        :raises FileExistsError: 快照数据库已经存在时抛出。
        """
        database_path = self._resolve(snapshot.duckdb_uri)
        if database_path.exists():
            raise FileExistsError(f"DuckDB 快照已存在：{snapshot.duckdb_uri}")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = database_path.with_name(f".{database_path.name}.{uuid.uuid4().hex}.tmp")
        connection = duckdb.connect(str(temporary))
        try:
            _create_schema(connection)
            if facts:
                connection.executemany(_INSERT_SQL, [_fact_row(fact) for fact in facts])
            connection.execute("CHECKPOINT")
            connection.close()
            os.replace(temporary, database_path)
        except Exception:
            connection.close()
            if temporary.exists():
                temporary.unlink()
            raise

    def query(self, snapshot: SnapshotReference, plan: QueryPlan) -> list[EvidenceUnit]:
        """按受控 QueryPlan 执行参数化事实查询。

        :param snapshot: 只读事实库快照。
        :param plan: 已通过 Pydantic 校验的结构化查询计划。
        :return: 保留精确数值、单位和单元格定位的证据。
        :raises FileNotFoundError: 快照数据库不存在时抛出。
        :raises ValueError: 操作不属于当前安全查询子集时抛出。
        """
        database_path = self._resolve(snapshot.duckdb_uri)
        if not database_path.is_file():
            raise FileNotFoundError(f"DuckDB 快照不存在：{snapshot.duckdb_uri}")
        results: list[EvidenceUnit] = []
        connection = duckdb.connect(str(database_path), read_only=True)
        try:
            for operation in plan.structured_operations:
                results.extend(_execute_operation(connection, operation, plan))
        finally:
            connection.close()
        return results

    def _resolve(self, relative_uri: str) -> Path:
        path = (self.database_root / relative_uri).resolve()
        try:
            path.relative_to(self.database_root)
        except ValueError as exc:
            raise ValueError("DuckDB URI 逃逸了配置根目录。") from exc
        return path


def _execute_operation(
    connection: duckdb.DuckDBPyConnection,
    operation: StructuredOperation,
    plan: QueryPlan,
) -> list[EvidenceUnit]:
    where, parameters = _where_clause(operation, plan)
    if operation.operation in {
        StructuredOperationType.LOOKUP,
        StructuredOperationType.DIFFERENCE,
        StructuredOperationType.RATIO,
        StructuredOperationType.TREND,
    }:
        rows = _select_rows(connection, where, parameters)
        row_evidence = [_row_evidence(row) for row in rows]
        if operation.operation is StructuredOperationType.LOOKUP:
            return row_evidence
        calculation = _derived_calculation_evidence(operation, plan, rows)
        return [*row_evidence, calculation] if calculation is not None else row_evidence
    if operation.operation not in {
        StructuredOperationType.SUM,
        StructuredOperationType.AVERAGE,
        StructuredOperationType.MINIMUM,
        StructuredOperationType.MAXIMUM,
        StructuredOperationType.COUNT,
    }:
        raise ValueError(f"当前事实库尚不执行该结构化操作：{operation.operation.value}")
    aggregate = {
        StructuredOperationType.SUM: "sum(CAST(normalized_value AS DECIMAL(38, 12)))",
        StructuredOperationType.AVERAGE: "avg(CAST(normalized_value AS DECIMAL(38, 12)))",
        StructuredOperationType.MINIMUM: "min(CAST(normalized_value AS DECIMAL(38, 12)))",
        StructuredOperationType.MAXIMUM: "max(CAST(normalized_value AS DECIMAL(38, 12)))",
        StructuredOperationType.COUNT: "count(*)",
    }[operation.operation]
    aggregate_row = connection.execute(
        f"SELECT {aggregate} FROM table_facts {where}",
        parameters,
    ).fetchone()
    if aggregate_row is None:
        return []
    value = aggregate_row[0]
    if value is None:
        return []
    return [_calculation_evidence(operation, plan, Decimal(str(value)))]


def _select_rows(
    connection: duckdb.DuckDBPyConnection,
    where: str,
    parameters: list[str],
) -> list[dict[str, object]]:
    """读取派生计算所需的原始事实行。

    :param connection: 只读 DuckDB 连接。
    :param where: 已由白名单构造的 WHERE 子句。
    :param parameters: 与占位符对应的参数。
    :return: 按期间、指标和机构稳定排序的事实行。
    """
    rows = connection.execute(
        f"SELECT * FROM table_facts {where} ORDER BY period_end, metric_name, entity_name LIMIT 200",
        parameters,
    ).fetchall()
    columns = [column[0] for column in connection.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _derived_calculation_evidence(
    operation: StructuredOperation,
    plan: QueryPlan,
    rows: list[dict[str, object]],
) -> EvidenceUnit | None:
    """对两期可比事实执行差额、比率或趋势计算。

    :param operation: 白名单派生操作。
    :param plan: 当前查询计划。
    :param rows: 已按期间排序的原始事实行。
    :return: 可复核计算证据；数据不足或口径不一致时返回空值。
    """
    if len(rows) < 2:
        return None
    units = {str(row["unit"] or "") for row in rows}
    metrics = {str(row["metric_code"] or row["metric_name"] or "") for row in rows}
    entities = {str(row["entity_code"] or row["entity_name"] or "") for row in rows}
    if len(units) != 1 or len(metrics) != 1 or len(entities) != 1:
        return None
    first, last = rows[0], rows[-1]
    first_value = Decimal(str(first["normalized_value"]))
    last_value = Decimal(str(last["normalized_value"]))
    if operation.operation in {StructuredOperationType.DIFFERENCE, StructuredOperationType.TREND}:
        value = last_value - first_value
        expression = f"{last_value} - {first_value}"
        unit = str(last["unit"] or "") or None
    elif operation.operation is StructuredOperationType.RATIO:
        if first_value == 0:
            return None
        value = last_value / first_value
        expression = f"{last_value} / {first_value}"
        unit = None
    else:
        return None
    inputs = [str(first["evidence_id"]), str(last["evidence_id"])]
    created_at = datetime.now(UTC)
    identity = stable_id("evidence", plan.query_plan_id, operation.model_dump(mode="json"), inputs)
    return EvidenceUnit(
        evidence_id=identity,
        source_id=stable_id("source", plan.knowledge_base_id, "duckdb_calculation"),
        document_id=stable_id("document", plan.knowledge_base_id, "duckdb_calculation"),
        evidence_type=EvidenceType.CALCULATION,
        excerpt=f"{operation.operation.value}：{expression} = {format(value, 'f')}{unit or ''}",
        source_value=format(value, "f"),
        unit=unit,
        location=SourceLocation(artifact_uri="duckdb/calculation"),
        lineage=LineageMetadata(
            run_id=plan.trace_id,
            producer="duckdb_fact_store",
            producer_version="0.01",
            input_ids=inputs,
            created_at=created_at,
        ),
    )


def _where_clause(
    operation: StructuredOperation,
    plan: QueryPlan,
) -> tuple[str, list[str]]:
    clauses = ["normalized_value IS NOT NULL"]
    parameters: list[str] = []
    if plan.filters.source_ids:
        clauses.append("source_id IN (" + ",".join("?" for _ in plan.filters.source_ids) + ")")
        parameters.extend(plan.filters.source_ids)
    for values, columns in (
        ([operation.metric] if operation.metric else plan.filters.metrics, ("metric_code", "metric_name")),
        ([operation.entity] if operation.entity else plan.filters.entities, ("entity_code", "entity_name")),
    ):
        if values:
            value_clauses = []
            for value in values:
                value_clauses.append(f"({columns[0]} = ? OR {columns[1]} = ?)")
                parameters.extend([value, value])
            clauses.append("(" + " OR ".join(value_clauses) + ")")
    periods = operation.periods or plan.filters.periods
    if periods:
        clauses.append("CAST(period_end AS VARCHAR) IN (" + ",".join("?" for _ in periods) + ")")
        parameters.extend(periods)
    requested_units = [operation.unit] if operation.unit else plan.filters.units
    unit_aliases = {
        "亿元": ("亿元", "CNY_100M"),
        "万件": ("万件", "TEN_THOUSAND_POLICIES"),
    }
    units = sorted(
        {
            alias
            for unit in requested_units
            for alias in unit_aliases.get(unit, (unit,))
        }
    )
    if units:
        clauses.append("unit IN (" + ",".join("?" for _ in units) + ")")
        parameters.extend(units)
    return "WHERE " + " AND ".join(clauses), parameters


def _row_evidence(row: dict[str, object]) -> EvidenceUnit:
    flags = _list_field(row, "quality_flags", "quality_flags_json")
    reasons = _list_field(row, "review_reasons", "review_reasons_json")
    quality = QualityMetadata(
        status=QualityStatus(str(row["quality_status"])),
        flags=flags,
        requires_manual_review=bool(row["requires_manual_review"]),
        review_reasons=reasons,
    )
    location = SourceLocation(
        sheet_name=str(row["sheet_name"]),
        cell_range=str(row["cell_range"]),
        table_id=str(row["table_id"]),
    )
    return EvidenceUnit(
        evidence_id=str(row["evidence_id"]),
        source_id=str(row["source_id"]),
        document_id=str(row["document_id"]),
        evidence_type=EvidenceType.CELL,
        excerpt=_row_excerpt(row),
        source_value=str(row["raw_value"]) if row["raw_value"] is not None else None,
        unit=str(row["unit"]) if row["unit"] is not None else None,
        location=location,
        quality=quality,
        lineage=LineageMetadata(
            run_id=str(row["run_id"]),
            producer=str(row["producer"]),
            producer_version=str(row["producer_version"]),
            input_ids=[str(row["fact_id"])],
            created_at=_aware_datetime(row["created_at"]),
        ),
    )


def _list_field(
    row: dict[str, object],
    parquet_name: str,
    json_name: str,
) -> list[str]:
    """兼容 Parquet 原生列表与端口建表时的 JSON 字符串字段。

    :param row: 当前事实行。
    :param parquet_name: Parquet 快照中的列表字段名。
    :param json_name: ``replace_snapshot`` 模式中的 JSON 字段名。
    :return: 统一的字符串列表。
    """
    value = row.get(parquet_name, row.get(json_name, []))
    if value is None:
        return []
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise TypeError(f"{parquet_name} 必须是列表。")
        return [str(item) for item in parsed]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if hasattr(value, "tolist"):
        converted = value.tolist()
        if isinstance(converted, list):
            return [str(item) for item in converted]
    raise TypeError(f"{parquet_name} 字段类型不受支持：{type(value).__name__}")


def _calculation_evidence(
    operation: StructuredOperation,
    plan: QueryPlan,
    value: Decimal,
) -> EvidenceUnit:
    created_at = datetime.now(UTC)
    identity = stable_id("evidence", plan.query_plan_id, operation.model_dump(mode="json"))
    return EvidenceUnit(
        evidence_id=identity,
        source_id=stable_id("source", plan.knowledge_base_id, "duckdb_calculation"),
        document_id=stable_id("document", plan.knowledge_base_id, "duckdb_calculation"),
        evidence_type=EvidenceType.CALCULATION,
        excerpt=f"{operation.operation.value} 计算结果：{format(value, 'f')}{operation.unit or ''}",
        source_value=format(value, "f"),
        unit=operation.unit,
        location=SourceLocation(artifact_uri="duckdb/calculation"),
        lineage=LineageMetadata(
            run_id=plan.trace_id,
            producer="duckdb_fact_store",
            producer_version="0.01",
            input_ids=[plan.query_plan_id],
            created_at=created_at,
        ),
    )


def _row_excerpt(row: dict[str, object]) -> str:
    labels = [str(row[key]) for key in ("entity_name", "metric_name", "period_end") if row[key]]
    return "；".join(labels) + f"：{row['raw_value']}{row['unit'] or ''}"


def _create_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(_CREATE_TABLE_SQL)
    connection.execute("CREATE INDEX table_facts_metric_idx ON table_facts(metric_name)")
    connection.execute("CREATE INDEX table_facts_period_idx ON table_facts(period_end)")
    connection.execute("CREATE INDEX table_facts_source_idx ON table_facts(source_id)")


def _fact_row(fact: TableFact) -> tuple[object, ...]:
    return (
        fact.fact_id,
        fact.source_id,
        fact.document_id,
        fact.table_id,
        fact.metric_code,
        fact.metric_name,
        fact.entity_code,
        fact.entity_name,
        fact.period_start,
        fact.period_end,
        fact.period_basis,
        fact.raw_value,
        fact.normalized_value,
        fact.value_type.value,
        fact.unit,
        fact.scale,
        fact.statistical_scope,
        fact.accounting_basis,
        fact.is_formula,
        fact.formula,
        fact.formula_cache_status.value,
        fact.evidence_id,
        fact.location.sheet_name,
        fact.location.cell_range,
        fact.quality.status.value,
        json.dumps(fact.quality.flags, ensure_ascii=False),
        fact.quality.requires_manual_review,
        json.dumps(fact.quality.review_reasons, ensure_ascii=False),
        fact.lineage.run_id,
        fact.lineage.producer,
        fact.lineage.producer_version,
        fact.lineage.created_at.astimezone(UTC).replace(tzinfo=None),
    )


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("DuckDB created_at 必须返回 datetime。")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


_CREATE_TABLE_SQL = """
CREATE TABLE table_facts (
    fact_id VARCHAR PRIMARY KEY, source_id VARCHAR NOT NULL, document_id VARCHAR NOT NULL,
    table_id VARCHAR NOT NULL, metric_code VARCHAR, metric_name VARCHAR,
    entity_code VARCHAR, entity_name VARCHAR, period_start DATE, period_end DATE,
    period_basis VARCHAR, raw_value VARCHAR, normalized_value VARCHAR, value_type VARCHAR NOT NULL,
    unit VARCHAR, scale VARCHAR, statistical_scope VARCHAR, accounting_basis VARCHAR,
    is_formula BOOLEAN NOT NULL, formula VARCHAR, formula_cache_status VARCHAR NOT NULL,
    evidence_id VARCHAR NOT NULL, sheet_name VARCHAR NOT NULL, cell_range VARCHAR NOT NULL,
    quality_status VARCHAR NOT NULL, quality_flags_json VARCHAR NOT NULL,
    requires_manual_review BOOLEAN NOT NULL, review_reasons_json VARCHAR NOT NULL,
    run_id VARCHAR NOT NULL, producer VARCHAR NOT NULL, producer_version VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL
)
"""

_INSERT_SQL = "INSERT INTO table_facts VALUES (" + ",".join("?" for _ in range(32)) + ")"
