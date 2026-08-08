from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trusted_rag.preprocessing.documents.document_inventory import (
    discover_documents,
)


class DocumentInventoryTests(unittest.TestCase):
    def test_discovers_supported_formats_in_deterministic_size_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '401_small.pdf').write_bytes(b'a')
            (root / '402_middle.docx').write_bytes(b'bbb')
            (root / '403_large.doc').write_bytes(b'ccccc')
            (root / 'ignore.xlsx').write_bytes(b'x')
            (root / '~$404_lock.docx').write_bytes(b'x')

            sources = discover_documents(
                input_root=root,
                recursive=True,
                include_extensions=('doc', 'docx', 'pdf'),
                exclude_name_prefixes=('~$',),
                order_by='size_ascending',
            )

            self.assertEqual(
                [source.doc_id for source in sources],
                ['401', '402', '403'],
            )
            self.assertEqual(
                [source.source_format for source in sources],
                ['pdf', 'docx', 'doc'],
            )

    def test_duplicate_numeric_prefixes_get_collision_safe_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '500_a.docx').write_bytes(b'a')
            (root / '500_b.pdf').write_bytes(b'b')

            sources = discover_documents(
                input_root=root,
                recursive=False,
                include_extensions=('docx', 'pdf'),
                exclude_name_prefixes=(),
                order_by='relative_path',
            )

            self.assertEqual(len({source.doc_id for source in sources}), 2)
            self.assertTrue(all(source.doc_id.startswith('500-') for source in sources))


if __name__ == '__main__':
    unittest.main()
