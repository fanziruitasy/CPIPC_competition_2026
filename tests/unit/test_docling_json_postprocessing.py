from __future__ import annotations

import unittest
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'src'))

from trusted_rag.preprocessing.documents.docling_runner import (
    _normalize_text_artifact_references,
    _rewrite_local_artifact_uris,
)


class DoclingJsonPostprocessingTests(unittest.TestCase):
    def test_absolute_temporary_artifact_uri_becomes_relative(self) -> None:
        temporary = Path('E:/workspace/run/candidate/.398.tmp')
        value = {
            'pages': {
                '1': {
                    'image': {
                        'uri': (
                            'E:/workspace/run/candidate/.398.tmp/'
                            '398_artifacts/page_000001.png'
                        )
                    }
                }
            }
        }

        rewritten = _rewrite_local_artifact_uris(value, temporary)

        self.assertEqual(
            rewritten['pages']['1']['image']['uri'],
            '398_artifacts/page_000001.png',
        )

    def test_remote_uri_is_unchanged(self) -> None:
        value = {'image': {'uri': 'https://example.com/image.png'}}
        rewritten = _rewrite_local_artifact_uris(
            value,
            Path('E:/workspace/run/candidate/.398.tmp'),
        )
        self.assertEqual(rewritten, value)

    def test_markdown_temporary_path_becomes_relative(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / '.398.tmp'
            temporary.mkdir()
            markdown = temporary / '398.md'
            markdown.write_text(
                f'![Image]({temporary}\\398_artifacts\\image.png)',
                encoding='utf-8',
            )
            _normalize_text_artifact_references(markdown, temporary)
            self.assertEqual(
                markdown.read_text(encoding='utf-8'),
                '![Image](398_artifacts\\image.png)',
            )

    def test_html_percent_encoded_temporary_path_becomes_relative(self) -> None:
        import tempfile
        from urllib.parse import quote

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / '.398.tmp'
            temporary.mkdir()
            html_path = temporary / '398.html'
            source = str(temporary / '398_artifacts' / 'image.png')
            html_path.write_text(
                f'<img src="{quote(source, safe="")}">',
                encoding='utf-8',
            )
            _normalize_text_artifact_references(html_path, temporary)
            self.assertEqual(
                html_path.read_text(encoding='utf-8'),
                '<img src="398_artifacts/image.png">',
            )
