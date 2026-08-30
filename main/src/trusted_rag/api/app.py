"""创建无需鉴权且面向前端的 FastAPI 应用。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import duckdb
import yaml
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from qdrant_client import QdrantClient

from trusted_rag.api.contracts import (
    ApiError,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    KnowledgeBaseStatus,
)
from trusted_rag.application.ingestion_registration import IngestionRegistrationService
from trusted_rag.application.query_service import TrustedRagQueryService
from trusted_rag.application.runtime import build_query_service
from trusted_rag.ingestion.jobs import JsonIngestionJobRepository

QueryServiceFactory = Callable[[str, str | None], TrustedRagQueryService]


def create_app(
    *,
    main_root: Path | None = None,
    config_path: Path | None = None,
    query_service_factory: QueryServiceFactory | None = None,
) -> FastAPI:
    """创建应用并注入可测试的问答服务工厂。

    :param main_root: `main` 项目根目录。
    :param config_path: 版本化检索配置。
    :param query_service_factory: 测试或部署可覆盖的服务工厂。
    :return: 配置完成的 FastAPI 应用。
    """
    root = (main_root or Path(__file__).resolve().parents[3]).resolve()
    configured_path = os.getenv("RAG_CONFIG_PATH")
    selected_path = config_path or (
        Path(configured_path) if configured_path else root / "configs/retrieval/v0.01.yaml"
    )
    config_file = (
        selected_path if selected_path.is_absolute() else root / selected_path
    ).resolve()
    config: dict[str, Any] = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    @lru_cache(maxsize=8)
    def default_factory(_trace_id: str, profile: str | None) -> TrustedRagQueryService:
        return build_query_service(root, config_file, trace_id=_trace_id, profile=profile)

    factory = query_service_factory or default_factory
    ingestion_root = root / "data_runtime/uploads"
    jobs_root = root / "data_runtime/ingestion_jobs"
    registration = IngestionRegistrationService(
        runs_root=root / "data_runtime/ingestion_runs/api",
        jobs_root=jobs_root,
    )
    job_repository = JsonIngestionJobRepository(jobs_root)
    app = FastAPI(
        title="银行业监管制度与统计报表可信 RAG API",
        version="0.1.0",
        description="提供知识库状态、可信问答、来源引用和流式事件接口。",
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        error = ApiError(code="request.invalid", message="请求字段不符合接口契约。")
        return JSONResponse(status_code=422, content=error.model_dump(mode="json"))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-ID")
        error = ApiError(code=f"http.{exc.status_code}", message=str(exc.detail), trace_id=trace_id)
        return JSONResponse(status_code=exc.status_code, content=error.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _exc: Exception) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-ID")
        error = ApiError(code="internal.error", message="服务处理失败，请使用追踪标识查询日志。", trace_id=trace_id)
        return JSONResponse(status_code=500, content=error.model_dump(mode="json"))

    @app.get("/health/live", response_model=HealthResponse)
    def live() -> HealthResponse:
        return HealthResponse(status="ok", checks={"api": True})

    @app.get("/health/ready", response_model=HealthResponse)
    def ready() -> HealthResponse:
        checks = _readiness(root, config)
        if not all(checks.values()):
            response = HealthResponse(status="not_ready", checks=checks)
            raise HTTPException(status_code=503, detail=response.model_dump())
        return HealthResponse(status="ok", checks=checks)

    @app.get(
        "/api/v1/knowledge-bases/{knowledge_base_id}",
        response_model=KnowledgeBaseStatus,
    )
    def knowledge_base(knowledge_base_id: str) -> KnowledgeBaseStatus:
        if knowledge_base_id != config["knowledge_base_id"]:
            raise HTTPException(status_code=404, detail="知识库不存在。")
        snapshot_root = root / config["snapshot"]["run_root"]
        manifest = json.loads((snapshot_root / "snapshot.json").read_text(encoding="utf-8"))
        corpus_root = root / config["snapshot"]["corpus_root"]
        corpus_manifest = json.loads((corpus_root / "run.json").read_text(encoding="utf-8"))
        counts = corpus_manifest["counts"]
        return KnowledgeBaseStatus(
            knowledge_base_id=knowledge_base_id,
            snapshot_id=manifest["snapshot_id"],
            collection_alias=config["snapshot"]["collection_alias"],
            source_file_count=int(counts["source_aliases.jsonl"]),
            unique_document_count=int(counts["documents.jsonl"]),
            corpus_chunk_count=int(counts["chunks.jsonl"]),
            indexed_chunk_count=int(manifest["qdrant_point_count"]),
            fact_count=int(manifest["duckdb_fact_count"]),
            status="ready" if manifest["smoke_passed"] else "not_ready",
        )

    @app.post("/api/v1/knowledge-bases/{knowledge_base_id}/ingestion-jobs", status_code=202)
    async def create_ingestion_job(
        knowledge_base_id: str,
        files: Annotated[list[UploadFile], File()],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if knowledge_base_id != config["knowledge_base_id"]:
            raise HTTPException(status_code=404, detail="知识库不存在。")
        if not files:
            raise HTTPException(status_code=422, detail="至少上传一个文件。")
        upload_id = hashlib.sha256(
            (idempotency_key or uuid.uuid4().hex).encode("utf-8")
        ).hexdigest()[:24]
        upload_root = ingestion_root / f"upload_{upload_id}"
        upload_root.mkdir(parents=True, exist_ok=True)
        supported = {".doc", ".docx", ".pdf", ".xls", ".xlsx"}
        for upload in files:
            file_name = Path(upload.filename or "").name
            if not file_name or Path(file_name).suffix.casefold() not in supported:
                raise HTTPException(status_code=415, detail="仅支持 DOC、DOCX、PDF、XLS 和 XLSX。")
            target = upload_root / file_name
            with target.open("wb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    stream.write(chunk)
            await upload.close()
        try:
            outcome = registration.register(
                source_root=upload_root,
                knowledge_base_id=knowledge_base_id,
                pipeline="api-ingestion",
                config_version="0.01",
                effective_config={"supported_formats": sorted(supported)},
                code_version="0.1.0",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="上传文件签名或内容无效。") from exc
        return {
            "job": outcome.job.model_dump(mode="json"),
            "reused_existing_job": outcome.reused_existing_job,
        }

    @app.get("/api/v1/ingestion-jobs/{job_id}")
    def ingestion_job(job_id: str) -> dict[str, Any]:
        try:
            job = job_repository.get(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="入库任务不存在。") from exc
        if job is None:
            raise HTTPException(status_code=404, detail="入库任务不存在。")
        return job.model_dump(mode="json")

    @app.post("/api/v1/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, x_trace_id: str | None = Header(default=None)) -> ChatResponse:
        if body.knowledge_base_id != config["knowledge_base_id"]:
            raise HTTPException(status_code=404, detail="知识库不存在。")
        trace_id = x_trace_id or f"trace-{uuid.uuid4().hex}"
        service = factory(trace_id, body.retrieval_profile.value if body.retrieval_profile else None)
        answer = service.ask(
            body.question,
            trace_id=trace_id,
            user_id=body.user_id,
            session_id=body.session_id,
        )
        return ChatResponse(trace_id=trace_id, answer=answer)

    @app.post("/api/v1/chat/stream")
    def chat_stream(body: ChatRequest, x_trace_id: str | None = Header(default=None)) -> StreamingResponse:
        if body.knowledge_base_id != config["knowledge_base_id"]:
            raise HTTPException(status_code=404, detail="知识库不存在。")
        trace_id = x_trace_id or f"trace-{uuid.uuid4().hex}"

        def events() -> Iterator[str]:
            yield _sse("status", {"trace_id": trace_id, "status": "processing"})
            try:
                service = factory(
                    trace_id,
                    body.retrieval_profile.value if body.retrieval_profile else None,
                )
                answer = service.ask(
                    body.question,
                    trace_id=trace_id,
                    user_id=body.user_id,
                    session_id=body.session_id,
                )
                if answer.answer_text:
                    yield _sse("answer_delta", {"text": answer.answer_text})
                yield _sse(
                    "citations",
                    [item.model_dump(mode="json") for item in answer.citations],
                )
                yield _sse("usage", answer.usage.model_dump(mode="json"))
                yield _sse(
                    "completed",
                    {"trace_id": trace_id, "answer": answer.model_dump(mode="json")},
                )
            except Exception:
                error = ApiError(
                    code="stream.processing_failed",
                    message="问答处理失败，请使用追踪标识查询日志。",
                    trace_id=trace_id,
                )
                yield _sse("error", error.model_dump(mode="json"))

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def _readiness(root: Path, config: dict[str, Any]) -> dict[str, bool]:
    snapshot_root = (root / config["snapshot"]["run_root"]).resolve()
    duckdb_path = snapshot_root / config["snapshot"]["duckdb_uri"]
    checks = {
        "snapshot_manifest": (snapshot_root / "snapshot.json").is_file(),
        "bm25_vocabulary": (snapshot_root / "bm25_vocabulary.json").is_file(),
        "duckdb": duckdb_path.is_file(),
        "qdrant": False,
    }
    if checks["duckdb"]:
        connection = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            checks["duckdb"] = bool(connection.execute("SELECT 1").fetchone())
        finally:
            connection.close()
    try:
        qdrant_url = os.getenv("QDRANT_URL", config["snapshot"]["qdrant_url"])
        checks["qdrant"] = QdrantClient(url=qdrant_url).collection_exists(
            config["snapshot"]["collection_alias"]
        )
    except Exception:
        checks["qdrant"] = False
    return checks


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


app = create_app()
