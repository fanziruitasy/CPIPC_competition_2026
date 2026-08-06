from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'src'))

from trusted_rag.preprocessing.documents.docling_config import (
    _validate_pdf_pipeline_options,
)


class DoclingPythonConfigTests(unittest.TestCase):
    def test_polymorphic_options_are_not_silently_dropped(self) -> None:
        options = _validate_pdf_pipeline_options(
            {
                'document_timeout': None,
                'do_ocr': False,
                'table_structure_options': {
                    'mode': 'accurate',
                    'do_cell_matching': True,
                },
                'layout_options': {
                    'engine_options': {
                        'engine_type': 'transformers',
                        'compile_model': False,
                    }
                },
                'picture_classification_options': {
                    'engine_options': {
                        'engine_type': 'transformers',
                        'compile_model': False,
                    }
                },
            }
        )

        self.assertEqual(options.table_structure_options.mode.value, 'accurate')
        self.assertTrue(options.table_structure_options.do_cell_matching)
        self.assertFalse(options.layout_options.engine_options.compile_model)
        self.assertFalse(
            options.picture_classification_options.engine_options.compile_model
        )
        self.assertIsNone(options.document_timeout)
        self.assertFalse(options.do_ocr)

    def test_unknown_top_level_option_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, 'unsupported top-level'):
            _validate_pdf_pipeline_options({'not_a_docling_option': True})


if __name__ == '__main__':
    unittest.main()
