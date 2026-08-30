"""交付核验脚本单元测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    """加载未安装在包内的交付核验脚本。

    :return: 已加载的脚本模块。
    """
    path = Path(__file__).resolve().parents[3] / "scripts/delivery/verify_delivery.py"
    specification = importlib.util.spec_from_file_location("verify_delivery", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_sensitive_scan_ignores_empty_example_and_detects_real_value(tmp_path: Path) -> None:
    """空白环境契约不报警，非空密钥只返回文件名而不泄漏内容。"""
    module = load_module()
    (tmp_path / ".env.example").write_text("DASHSCOPE_API_KEY=\n", encoding="utf-8")
    assert module.scan_sensitive_information(tmp_path) == []

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "unsafe.yaml").write_text("DASHSCOPE_API_KEY=secret-value-123\n", encoding="utf-8")
    assert module.scan_sensitive_information(tmp_path) == ["configs/unsafe.yaml"]
