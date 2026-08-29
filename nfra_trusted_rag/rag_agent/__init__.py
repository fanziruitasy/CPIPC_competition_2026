"""基于单文件 DuckDB 的金融监管 Excel 结构化问答。

  - KnowledgeBase：只读 DuckDB 结构化数据仓库
  - RagAgent：可溯源问答（答案 + 证据 + 引用来源 + 拒答/澄清）
  - evaluate / service / cli：评测、HTTP 服务与命令行入口

运行时唯一数据源为 `nfra.duckdb`。
"""

from .knowledge_base import KnowledgeBase
from .agent import RagAgent

__all__ = ["KnowledgeBase", "RagAgent"]
__version__ = "2.0.0"
