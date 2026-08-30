"""验证 DOCX Docling 配置严格校验和凭证内存注入。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trusted_rag.ingestion.documents.docling_configuration import (
    build_word_docling_options,
    load_docx_parsing_config,
    redacted_docx_options,
)

MAIN_ROOT = Path(__file__).resolve().parents[4]
CONFIG_ROOT = MAIN_ROOT / "configs" / "ingestion" / "documents"


@pytest.mark.parametrize(
    ("profile", "fallback"),
    [("native_docx", "none"), ("converted_docx", "original-doc")],
)
def test_load_adopted_docx_configs(profile: str, fallback: str) -> None:
    """两类 DOCX 配置必须共享解析参数并保留不同兜底策略。"""
    config = load_docx_parsing_config(CONFIG_ROOT / profile / "v0.01.yaml")

    assert config.source_profile == profile.replace("_", "-")
    assert config.outputs == ["json", "md", "html"]
    assert config.enrichment.fallback_mode == fallback
    assert config.docling.pipeline_class == "simple"
    assert config.docling.backend == "msword"


def test_build_options_injects_secret_only_in_memory() -> None:
    """模型 URL、密钥和模型名只进入 Docling 运行时对象。"""
    config = load_docx_parsing_config(CONFIG_ROOT / "native_docx" / "v0.01.yaml")
    pipeline, backend = build_word_docling_options(
        config,
        environ={
            "VL_PROVIDER": "dashscope",
            "VL_MODEL": "qwen-vl-max",
            "DASHSCOPE_BASE_URL": "https://example.test/v1",
            "DASHSCOPE_API_KEY": "test-secret",
            "VL_TIMEOUT_SECONDS": "90",
            "VL_MAX_TOKENS": "1024",
            "VL_TEMPERATURE": "0.1",
            "VL_TOP_P": "0.8",
        },
    )

    description = pipeline.picture_description_options
    assert str(description.url) == "https://example.test/v1/chat/completions"
    assert description.headers["Authorization"] == "Bearer test-secret"
    assert description.params["model"] == "qwen-vl-max"
    assert description.timeout == 90
    assert pipeline.accelerator_options.device == "cuda"
    assert pipeline.accelerator_options.num_threads == 16
    assert backend.render_chart_images is True
    assert "test-secret" not in str(redacted_docx_options(config))
