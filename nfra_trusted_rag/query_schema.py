"""查询字段的单一白名单与事实表列映射。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalize(text: Any) -> str:
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).lower()
    value = value.replace("四季度", "4季度").replace("三季度", "3季度")
    value = value.replace("二季度", "2季度").replace("一季度", "1季度")
    return re.sub(r"[\s\-_/（）()《》“”‘’：:，,。.;；]+", "", value)


FACT_FILTER_FIELDS: dict[str, tuple[str, ...]] = {
    "metric": ("metric_code", "metric_name", "metric_name_raw"),
    "entity": ("entity_code", "entity_name"),
    "entity_type": ("entity_type",),
    "region": ("region_code", "region_name"),
    "region_type": ("region_type",),
    "period_basis": ("period_basis",),
    "scope": ("scope",),
    "product_line": ("product_line", "product_line_name"),
    "measure_type": ("measure_type",),
    "unit": ("unit",),
}

PERIOD_FILTERS = frozenset(
    {"period_end", "from_period", "to_period", "start_period", "end_period"}
)
PLAN_FILTERS = frozenset({*FACT_FILTER_FIELDS, *PERIOD_FILTERS})
SOURCE_FILTERS = frozenset(
    {"domain", "topic", "dataset_family", "content_type", "frequency"}
)
