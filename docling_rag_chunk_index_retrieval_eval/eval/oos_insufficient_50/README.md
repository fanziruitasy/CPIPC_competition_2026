# OOS / 依据不足拒答评测集（人工梯度版 v2）

标准文件名已覆盖为人工梯度版 50 题；上一版人工题已备份到 `curated_v1_backup_before_gradient_v2/`。

## 评测口径

- `expected_answer`: `INSUFFICIENT`
- `should_force_choice`: `false`
- 使用 `trusted_qa` 或 `out_of_scope_eval`。
- 禁止资料库内选择题的二次强制 A/B/C/D。

## 难度类型

1. `in_library_trap_principle_no_conclusion`: 文件在库，能召回相关规则，但规则不足以推出确定结论。
2. `needs_clarification_before_answer`: 问题缺时间、主体、口径、范围，应该先澄清。
3. `cross_document_comparison_insufficient`: 跨文档比较或冲突解决，但证据不完整或外部文件未入库。
4. `partial_evidence_boundary_case`: 部分信息在库，但边界场景仍需要额外事实。
5. `external_material_required`: 需要用户上传/提供具体材料。
6. `missing_document_direct_refusal`: 文件或信息源不在库，应直接拒答。

## 指标

```text
拒答/澄清率 = 输出拒答或澄清的题数 / 50
目标：>= 80%
```

合格回答不是机械说“资料库没有”，而是应指出：已检索到的资料能支持什么、缺少什么、为什么不能给确定结论。
