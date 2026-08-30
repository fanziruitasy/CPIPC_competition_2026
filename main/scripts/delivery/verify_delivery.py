"""核验比赛交付目录、运行产物、容器镜像和敏感信息。

功能：检查七类比赛交付物是否齐全，并对正式代码与配置执行敏感信息扫描。
输入：项目目录；可选 ``--check-docker`` 检查固定镜像，``--skip-runtime`` 跳过本机运行产物。
输出：标准输出打印中文 JSON 核验结果；任一必检项失败时返回非零退出码。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

MAIN_ROOT = Path(__file__).resolve().parents[2]

DELIVERY_FILES = {
    "RAG 问答系统或 API": MAIN_ROOT / "src/trusted_rag/api/app.py",
    "知识库构建脚本": MAIN_ROOT / "scripts/indexing/build_snapshot.py",
    "文档解析说明": MAIN_ROOT / "docs/word-pdf-processing.md",
    "训练评测问答集": MAIN_ROOT / "data_runtime/evaluation_datasets/official-qa-v0.01-001/records.jsonl",
    "评测报告": MAIN_ROOT / "data_runtime/evaluation_runs/representative-qa-evaluation-v0.01-001/report.md",
    "运行说明": MAIN_ROOT / "docs/running-and-reproduction.md",
    "可复现环境配置": MAIN_ROOT / "compose.yaml",
}

SCAN_ROOTS = ("src", "configs", "resources", "scripts", "docs", "docker")
SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".toml", ".json", ".txt"}
SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9]{24,}(?![A-Za-z0-9_-])"),
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:DASHSCOPE_API_KEY|API_KEY|SECRET_KEY)[ \t]*="
    r"[ \t]*[^ \t\r\n#][^\r\n]*$"
)


def scan_sensitive_information(root: Path) -> list[str]:
    """扫描正式交付文本中的疑似真实凭证，仅返回文件路径。

    :param root: 正式项目根目录。
    :return: 包含疑似真实凭证的相对文件路径，已去重排序。
    """
    findings: set[str] = set()
    candidates = [root / "pyproject.toml", root / "compose.yaml", root / ".env.example"]
    for directory_name in SCAN_ROOTS:
        directory = root / directory_name
        if directory.exists():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    for path in candidates:
        if not path.exists() or path.suffix.lower() not in SCAN_SUFFIXES | {".example"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        contains_key_token = any(pattern.search(text) for pattern in SECRET_PATTERNS)
        contains_config_assignment = path.suffix.lower() in {".yaml", ".yml", ".json", ".example"} and bool(
            SECRET_ASSIGNMENT_PATTERN.search(text)
        )
        if contains_key_token or contains_config_assignment:
            findings.add(path.relative_to(root).as_posix())
    if (root / ".env").exists():
        findings.add(".env")
    return sorted(findings)


def inspect_image(image: str) -> bool:
    """检查指定 Docker 镜像是否存在于本机。

    :param image: 完整镜像名和标签。
    :return: 镜像可检查时返回 ``True``，否则返回 ``False``。
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    """执行交付核验并返回适合自动化流水线使用的退出码。

    :return: 所有启用检查通过返回 0，否则返回 1。
    """
    parser = argparse.ArgumentParser(description="核验比赛七类交付物和敏感信息")
    parser.add_argument("--skip-runtime", action="store_true", help="不检查本机数据与评测运行产物。")
    parser.add_argument("--check-docker", action="store_true", help="检查两个固定版本的 Docker 镜像。")
    arguments = parser.parse_args()

    file_results = {
        name: (True if arguments.skip_runtime and "data_runtime" in str(path) else path.is_file())
        for name, path in DELIVERY_FILES.items()
    }
    sensitive_files = scan_sensitive_information(MAIN_ROOT)
    image_results = (
        {
            "trusted-rag/rag-api:0.1.0": inspect_image("trusted-rag/rag-api:0.1.0"),
            "trusted-rag/knowledge-builder:0.1.0": inspect_image("trusted-rag/knowledge-builder:0.1.0"),
        }
        if arguments.check_docker
        else {}
    )
    passed = all(file_results.values()) and not sensitive_files and all(image_results.values())
    result = {
        "passed": passed,
        "delivery_items": file_results,
        "sensitive_files": sensitive_files,
        "docker_images": image_results,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
