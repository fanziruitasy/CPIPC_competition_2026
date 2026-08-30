"""验证 FastAPI 健康检查、知识库、Chat 和 SSE 契约。"""

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import yaml
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from trusted_rag.api.app import create_app
from trusted_rag.domain.enums import AnswerStatus, RetrievalProfile
from trusted_rag.domain.identity import stable_id
from trusted_rag.domain.query import AnswerRecord, RetrievalSummary


class FakeQueryService:
    """返回可信拒答的测试问答服务。"""

    def ask(self, question: str, *, trace_id: str, **_: object) -> AnswerRecord:
        """生成不调用外部服务的回答。

        :param question: 测试问题。
        :param trace_id: 请求追踪标识。
        :param _: 忽略用户和会话字段。
        :return: 合法的可信拒答记录。
        """
        plan_id = stable_id("query_plan", trace_id, question)
        return AnswerRecord(
            answer_id=stable_id("answer", plan_id),
            trace_id=trace_id,
            query_plan_id=plan_id,
            knowledge_base_id="nfra-regulations",
            question=question,
            status=AnswerStatus.REFUSED,
            answer_text="",
            refusal_reason="测试证据不足",
            retrieval=RetrievalSummary(profile=RetrievalProfile.DENSE_BM25),
            created_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


class FailingQueryService:
    """模拟流式问答内部失败。"""

    def ask(self, _question: str, *, trace_id: str, **_: object) -> AnswerRecord:
        """抛出不应泄漏到前端的内部异常。

        :param _question: 测试问题。
        :param trace_id: 请求追踪标识。
        :param _: 忽略其他请求字段。
        :return: 本方法不返回。
        :raises RuntimeError: 始终模拟内部失败。
        """
        raise RuntimeError(f"敏感内部错误：{trace_id}")


def test_openapi_live_chat_and_sse(tmp_path: Path) -> None:
    """API 应公开稳定模式，非流式与 SSE 最终语义保持一致。"""
    config_path = _config(tmp_path)
    app = create_app(
        main_root=tmp_path,
        config_path=config_path,
        query_service_factory=lambda _trace, _profile: FakeQueryService(),  # type: ignore[arg-type,return-value]
    )
    client = TestClient(app)

    assert client.get("/openapi.json").status_code == 200
    assert client.get("/health/live").json() == {"status": "ok", "checks": {"api": True}}

    response = client.post(
        "/api/v1/chat",
        headers={"X-Trace-ID": "trace-api-test"},
        json={"question": "监管要求是什么？"},
    )
    assert response.status_code == 200
    assert response.json()["answer"]["status"] == "refused"
    assert response.json()["trace_id"] == "trace-api-test"

    stream = client.post(
        "/api/v1/chat/stream",
        headers={"X-Trace-ID": "trace-sse-test"},
        json={"question": "监管要求是什么？"},
    )
    assert stream.status_code == 200
    assert "event: status" in stream.text
    assert "event: citations" in stream.text
    assert "event: usage" in stream.text
    assert "event: completed" in stream.text


def test_knowledge_base_status_and_uniform_error(tmp_path: Path) -> None:
    """知识库状态应读取快照，未知知识库返回统一错误结构。"""
    config_path = _config(tmp_path)
    app = create_app(
        main_root=tmp_path,
        config_path=config_path,
        query_service_factory=lambda _trace, _profile: FakeQueryService(),  # type: ignore[arg-type,return-value]
    )
    client = TestClient(app)

    response = client.get("/api/v1/knowledge-bases/nfra-regulations")
    assert response.status_code == 200
    assert response.json()["snapshot_id"] == "snapshot-test"
    assert response.json()["source_file_count"] == 3
    assert response.json()["unique_document_count"] == 2
    assert response.json()["corpus_chunk_count"] == 7
    assert response.json()["indexed_chunk_count"] == 20
    missing = client.get("/api/v1/knowledge-bases/missing")
    assert missing.status_code == 404
    assert missing.json()["code"] == "http.404"


def test_multi_file_upload_is_idempotent_and_job_is_queryable(tmp_path: Path) -> None:
    """多文件上传应立即返回任务，相同幂等键应复用现有任务。"""
    config_path = _config(tmp_path)
    app = create_app(
        main_root=tmp_path,
        config_path=config_path,
        query_service_factory=lambda _trace, _profile: FakeQueryService(),  # type: ignore[arg-type,return-value]
    )
    client = TestClient(app)
    uploads = [
        ("files", ("regulation.pdf", b"%PDF-test", "application/pdf")),
        (
            "files",
            (
                "notice.docx",
                _office_zip(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
    ]
    first = client.post(
        "/api/v1/knowledge-bases/nfra-regulations/ingestion-jobs",
        headers={"Idempotency-Key": "upload-test-001"},
        files=uploads,
    )
    second = client.post(
        "/api/v1/knowledge-bases/nfra-regulations/ingestion-jobs",
        headers={"Idempotency-Key": "upload-test-001"},
        files=uploads,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["reused_existing_job"] is True
    job_id = first.json()["job"]["job_id"]
    status = client.get(f"/api/v1/ingestion-jobs/{job_id}")
    assert status.status_code == 200
    assert len(status.json()["files"]) == 2


def test_sse_failure_returns_safe_error_event(tmp_path: Path) -> None:
    """流式处理失败应发送带追踪标识的安全错误事件。"""
    config_path = _config(tmp_path)
    app = create_app(
        main_root=tmp_path,
        config_path=config_path,
        query_service_factory=lambda _trace, _profile: FailingQueryService(),  # type: ignore[arg-type,return-value]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/chat/stream",
        headers={"X-Trace-ID": "trace-sse-failure"},
        json={"question": "触发内部失败"},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "stream.processing_failed" in response.text
    assert "trace-sse-failure" in response.text
    assert "敏感内部错误" not in response.text


def test_config_path_can_be_selected_by_environment(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """容器应能用 RAG_CONFIG_PATH 选择项目内版本化配置。"""
    config_path = _config(tmp_path)
    monkeypatch.setenv("RAG_CONFIG_PATH", config_path.name)

    app = create_app(
        main_root=tmp_path,
        query_service_factory=lambda _trace, _profile: FakeQueryService(),  # type: ignore[arg-type,return-value]
    )

    assert TestClient(app).get("/health/live").status_code == 200


def _office_zip() -> bytes:
    """生成可通过现代 Office ZIP 签名检查的最小测试文件。"""
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return stream.getvalue()


def _config(root: Path) -> Path:
    snapshot_root = root / "runtime/snapshot"
    snapshot_root.mkdir(parents=True)
    corpus_root = root / "runtime/corpus"
    corpus_root.mkdir(parents=True)
    (snapshot_root / "snapshot.json").write_text(
        '{"snapshot_id":"snapshot-test","qdrant_point_count":20,'
        '"duckdb_fact_count":100,"smoke_passed":true}',
        encoding="utf-8",
    )
    (corpus_root / "run.json").write_text(
        '{"counts":{"source_aliases.jsonl":3,"documents.jsonl":2,'
        '"chunks.jsonl":7}}',
        encoding="utf-8",
    )
    config = {
        "knowledge_base_id": "nfra-regulations",
        "snapshot": {
            "run_root": "runtime/snapshot",
            "qdrant_url": "http://127.0.0.1:6333",
            "collection_alias": "test-current",
            "duckdb_uri": "facts.duckdb",
            "corpus_root": "runtime/corpus",
        },
        "retrieval": {"default_profile": "dense_bm25"},
        "audit": {"directory": "runtime/audit"},
    }
    path = root / "retrieval.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path
