"""FastAPI 服务接口：把结构化问答 Agent 暴露为 HTTP API。

Agent 采用惰性加载，首次请求时才读取知识库，便于 `import` 与启动解耦。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from .agent import RagAgent

DEFAULT_DB_PATH = Path(os.environ.get("RAG_DB_PATH", "nfra.duckdb"))


class AskRequest(BaseModel):
    question: str
    options: Optional[dict[str, str]] = None


class EvaluateRequest(BaseModel):
    qa_file: str = "QA数据.xlsx"


def create_app(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    use_llm_planner: bool | None = None,
) -> FastAPI:
    """创建一个只读取指定 DuckDB 文件的 FastAPI 应用。"""
    app = FastAPI(title="金融监管 Excel DuckDB 结构化问答服务", version="2.0.0")
    agent: RagAgent | None = None

    def get_agent() -> RagAgent:
        nonlocal agent
        if agent is None:
            enabled = use_llm_planner
            if enabled is None:
                enabled = os.environ.get("RAG_USE_LLM_PLANNER", "true").lower() not in {
                    "0", "false", "off", "no"
                }
            agent = RagAgent(db_path, use_llm_planner=enabled)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("rag_agent.service:app", host="127.0.0.1", port=8000)
