from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'src'))

from trusted_rag.preprocessing.documents.libreoffice_converter import (
    LibreOfficeConversionError,
    convert_one_doc,
)


class LibreOfficeConverterTests(unittest.TestCase):
    def test_success_requires_a_non_empty_docx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / '398.doc'
            source.write_bytes(b'legacy-doc-fixture')

            def successful_runner(arguments, **kwargs):
                output_dir = Path(arguments[arguments.index('--outdir') + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / '398.docx').write_bytes(b'converted-docx')
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout='convert success',
                    stderr='',
                )

            converted, result = convert_one_doc(
                executable=root / 'soffice.com',
                staged_doc=source,
                output_dir=root / 'output',
                profile_dir=root / 'profile',
                export_filter='Office Open XML Text',
                timeout_seconds=None,
                runner=successful_runner,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(converted.read_bytes(), b'converted-docx')

    def test_zero_exit_without_docx_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / '399.doc'
            source.write_bytes(b'broken-doc-fixture')

            def empty_runner(arguments, **kwargs):
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout='',
                    stderr='',
                )

            with self.assertRaisesRegex(
                LibreOfficeConversionError,
                'no non-empty DOCX',
            ):
                convert_one_doc(
                    executable=root / 'soffice.com',
                    staged_doc=source,
                    output_dir=root / 'output',
                    profile_dir=root / 'profile',
                    export_filter='Office Open XML Text',
                    timeout_seconds=None,
                    runner=empty_runner,
                )

    def test_timeout_is_reported_as_conversion_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / '400.doc'
            source.write_bytes(b'slow-doc-fixture')

            def timeout_runner(arguments, **kwargs):
                raise subprocess.TimeoutExpired(arguments, kwargs['timeout'])

            with self.assertRaisesRegex(
                LibreOfficeConversionError,
                'exceeded timeout_seconds=3.0',
            ):
                convert_one_doc(
                    executable=root / 'soffice.com',
                    staged_doc=source,
                    output_dir=root / 'output',
                    profile_dir=root / 'profile',
                    export_filter='Office Open XML Text',
                    timeout_seconds=3.0,
                    runner=timeout_runner,
                )


if __name__ == '__main__':
    unittest.main()
