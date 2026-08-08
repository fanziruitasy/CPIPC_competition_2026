from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'src'))

from trusted_rag.preprocessing.documents.document_quality import (
    _body_reachability_metrics,
    _pointer_metrics,
    _serialized_resource_metrics,
    compare_text,
    extract_ooxml_baseline,
    resolve_json_pointer,
)


class DocumentQualityTests(unittest.TestCase):
    def test_character_and_number_recall_detects_loss(self) -> None:
        metrics = compare_text('资本充足率 10.5%，金额 1,234 元', '资本充足率 10.5%')

        self.assertLess(metrics['character_recall'], 1.0)
        self.assertEqual(metrics['number_recall'], 0.5)

    def test_json_pointer_resolution_and_broken_reference(self) -> None:
        document = {
            'body': {'children': [{'$ref': '#/texts/0'}, {'$ref': '#/texts/2'}]},
            'texts': [{'text': '有效'}],
        }

        self.assertEqual(resolve_json_pointer(document, '#/texts/0')['text'], '有效')
        self.assertEqual(_pointer_metrics(document)['broken_pointer_count'], 1)

    def test_body_reachability_finds_orphan_body_node(self) -> None:
        document = {
            'body': {'children': [{'$ref': '#/texts/0'}]},
            'texts': [
                {'content_layer': 'body', 'text': '可达'},
                {'content_layer': 'body', 'text': '孤儿'},
                {'content_layer': 'furniture', 'text': '页眉'},
            ],
        }

        metrics = _body_reachability_metrics(document)
        self.assertEqual(metrics['body_node_count'], 2)
        self.assertEqual(metrics['unreachable_body_node_count'], 1)

    def test_serialized_resource_integrity_checks_both_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / 'assets' / 'image.png'
            asset.parent.mkdir()
            asset.write_bytes(b'png')
            metrics = _serialized_resource_metrics(
                '![Image](assets\\image.png)',
                '<img src="assets/image.png"><img src="assets/missing.png">',
                root,
            )

        self.assertEqual(metrics['serialized_resource_reference_count'], 3)
        self.assertEqual(metrics['serialized_missing_resource_count'], 1)

    def test_ooxml_baseline_reads_text_and_table_shape(self) -> None:
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>监管数字123</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc/><w:tc/></w:tr><w:tr><w:tc/><w:tc/></w:tr></w:tbl>
          </w:body>
        </w:document>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.docx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', xml)
            baseline = extract_ooxml_baseline(path)

        self.assertEqual(baseline['text'], '监管数字123')
        self.assertEqual(baseline['table_shapes'], [{'rows': 2, 'columns': 2}])


if __name__ == '__main__':
    unittest.main()
