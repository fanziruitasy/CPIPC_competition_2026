"""结构化 Excel 问答命令行入口。

用法示例：
  python -m rag_agent.cli info
  python -m rag_agent.cli ask "根据 Excel 附件《2023年10月人身险公司经营情况表》…"
  python -m rag_agent.cli ask "…" --options 31739.18 6428.56 24912.73 397.89
  python -m rag_agent.cli evaluate --qa-file QA数据.xlsx
  python -m rag_agent.cli serve --port 8000
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import RagAgent
from .config import DEFAULT_DB_PATH


def cmd_info(args: argparse.Namespace) -> None:
    with RagAgent(
        args.db, use_llm_planner=False, use_bailian_embeddings=False
    ) as agent:
        print(json.dumps(agent.kb.summarize(), ensure_ascii=False, indent=2))


def cmd_ask(args: argparse.Namespace) -> None:
    options = None
    if args.options:
        letters = ("A", "B", "C", "D")
        options = {letters[i]: value for i, value in enumerate(args.options) if value}
    with _configured_agent(args) as agent:
        result = agent.ask(args.question, options)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_evaluate(args: argparse.Namespace) -> None:
    from .evaluate import evaluate, render_markdown, write_validated_qa

    with _configured_agent(args) as agent:
        summary = evaluate(agent, args.qa_file)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "eval_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.report_dir / "eval_report.md").write_text(
        render_markdown(summary), encoding="utf-8"
    )
    validated_output = (
        args.validated_qa_output
        or args.report_dir / f"{args.qa_file.stem}.validated.xlsx"
    )
    write_validated_qa(args.qa_file, summary, validated_output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        f"\n报告已写入：{args.report_dir / 'eval_report.md'} 与 {args.report_dir / 'eval_report.json'}"
    )
    print(f"校验后的评测集：{validated_output}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn
    from .service import create_app

    app = create_app(
        args.db,
        strict=not args.allow_first_match,
        use_llm_planner=not args.no_llm_planner,
        use_llm_answerer=not args.no_llm_answerer,
    )
    uvicorn.run(app, host=args.host, port=args.port)


def _configured_agent(args: argparse.Namespace) -> RagAgent:
    return RagAgent(
        args.db,
        strict=not args.allow_first_match,
        use_llm_planner=not args.no_llm_planner,
        use_llm_answerer=not args.no_llm_answerer,
    )


def _add_agent_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--allow-first-match", action="store_true")
    parser.add_argument("--no-llm-planner", action="store_true")
    parser.add_argument("--no-llm-answerer", action="store_true")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="基于 DuckDB 的金融监管 Excel 结构化问答"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="知识库概况")
    p_info.set_defaults(handler=cmd_info)

    p_ask = sub.add_parser("ask", help="提问")
    p_ask.add_argument("question")
    p_ask.add_argument("--options", nargs="*", default=None, help="选项 A B C D")
    _add_agent_options(p_ask)
    p_ask.set_defaults(handler=cmd_ask)

    p_eval = sub.add_parser("evaluate", help="评测")
    p_eval.add_argument("--qa-file", type=Path, default=Path("QA数据.xlsx"))
    p_eval.add_argument("--report-dir", type=Path, default=Path("reports"))
    p_eval.add_argument("--validated-qa-output", type=Path, default=None)
    _add_agent_options(p_eval)
    p_eval.set_defaults(handler=cmd_evaluate)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    _add_agent_options(p_serve)
    p_serve.set_defaults(handler=cmd_serve)

    args = parser.parse_args()

    args.handler(args)


if __name__ == "__main__":
    main()
