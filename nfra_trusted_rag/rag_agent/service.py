"""FastAPI 服务接口：把结构化问答 Agent 暴露为 HTTP API。

Agent 采用惰性加载，首次请求时才读取知识库，便于 `import` 与启动解耦。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .agent import RagAgent
from .config import DEFAULT_DB_PATH, env_flag


class AskRequest(BaseModel):
    question: str
    options: dict[str, str] | None = None


class EvaluateRequest(BaseModel):
    qa_file: str = "QA数据.xlsx"


def create_app(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    strict: bool = True,
    use_llm_planner: bool | None = None,
    use_llm_answerer: bool | None = None,
) -> FastAPI:
    """创建一个只读取指定 DuckDB 文件的 FastAPI 应用。"""
    agent: RagAgent | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            if agent is not None:
                agent.close()

    app = FastAPI(
        title="金融监管 Excel DuckDB 结构化问答服务",
        version="2.0.0",
        lifespan=lifespan,
    )

    def get_agent() -> RagAgent:
        nonlocal agent
        if agent is None:
            enabled = use_llm_planner
            if enabled is None:
                enabled = env_flag("RAG_USE_LLM_PLANNER")
            answerer_enabled = use_llm_answerer
            if answerer_enabled is None:
                answerer_enabled = env_flag("RAG_USE_LLM_ANSWERER")
            agent = RagAgent(
                db_path,
                strict=strict,
                use_llm_planner=enabled,
                use_llm_answerer=answerer_enabled,
            )
        return agent

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", **get_agent().kb.summarize()}

    @app.post("/ask")
    def ask(req: AskRequest) -> dict[str, Any]:
        return get_agent().ask(req.question, req.options)

    @app.post("/evaluate")
    def evaluate(req: EvaluateRequest) -> dict[str, Any]:
        from .evaluate import evaluate as run_eval

        return run_eval(get_agent(), Path(req.qa_file))

    return app


app = create_app()
