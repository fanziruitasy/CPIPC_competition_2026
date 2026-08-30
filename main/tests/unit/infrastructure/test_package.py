"""验证正式 Python 包能够正常导入。"""

import trusted_rag


def test_package_version() -> None:
    """项目包应公开固定的语义化版本。"""
    assert trusted_rag.__version__ == "0.1.0"

