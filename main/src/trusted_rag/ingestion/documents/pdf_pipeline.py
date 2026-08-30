"""为 Docling Standard PDF Pipeline 接入版本化公式识别 Prompt。"""

from __future__ import annotations

from docling.models.stages.code_formula.code_formula_vlm_model import CodeFormulaVlmModel
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline


class PromptedCodeFormulaVlmModel(CodeFormulaVlmModel):
    """公式使用项目 Prompt，代码继续使用 Docling 默认 Prompt。"""

    def _get_prompt(self, label: str) -> str:
        """返回当前代码或公式元素的模型提示词。

        :param label: Docling 元素标签。
        :return: 配置中的公式 Prompt 或 Docling 默认代码 Prompt。
        :raises ValueError: 公式 Prompt 为空时抛出。
        """
        if label != "formula":
            return super()._get_prompt(label)
        prompt = str(self.options.model_spec.prompt).strip()
        if not prompt:
            raise ValueError("PDF 公式识别 Prompt 不能为空。")
        return prompt


class TrustedStandardPdfPipeline(StandardPdfPipeline):
    """保留 Standard Pipeline，仅替换公式增强模型的 Prompt 适配器。"""

    def _init_models(self) -> None:
        """初始化原生模型并替换公式增强模型。

        :return: 无。
        :raises RuntimeError: 开启公式增强但未找到对应模型时抛出。
        """
        super()._init_models()
        if not self.pipeline_options.do_formula_enrichment:
            return
        code_formula_options = self.pipeline_options.code_formula_options.model_copy(
            update={
                "extract_code": self.pipeline_options.do_code_enrichment,
                "extract_formulas": self.pipeline_options.do_formula_enrichment,
            }
        )
        for index, model in enumerate(self.enrichment_pipe):
            if not isinstance(model, CodeFormulaVlmModel):
                continue
            if model.engine is not None:
                model.engine.cleanup()
            self.enrichment_pipe[index] = PromptedCodeFormulaVlmModel(
                enabled=True,
                artifacts_path=self.artifacts_path,
                options=code_formula_options,
                accelerator_options=self.pipeline_options.accelerator_options,
                enable_remote_services=self.pipeline_options.enable_remote_services,
            )
            return
        raise RuntimeError("Docling 未初始化公式增强模型。")
