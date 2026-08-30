"""提供面向前端的可信 RAG HTTP 与 SSE 接口。"""

from trusted_rag.api.app import create_app

__all__ = ["create_app"]
