"""§6 2026-11-11 (Casey: "Can we also expand the attachment size cap to 10
MB?"): a composer send may carry up to 10MB of attachments in all (was 5MB,
E82/E104), still ≤3 files, and the /send request limit leaves room for the
base64. Offline: imports serve.py's parser, starts nothing.

    .venv/bin/python -m unittest tests.test_attachment_cap -v
"""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))
_spec = importlib.util.spec_from_file_location("gc_serve", os.path.join(ROOT, "dashboard", "serve.py"))
serve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve)

MB = 1024 * 1024


def _att(n, name="f.pdf"):
    return {"name": name, "type": "application/pdf", "data": base64.b64encode(b"x" * n).decode()}


class AttachmentCap(unittest.TestCase):
    def test_ten_mb_in_all_is_allowed(self):
        out = serve._parse_attachments({"attachments": [_att(6 * MB, "a.pdf"), _att(4 * MB, "b.pdf")]})
        self.assertEqual(len(out), 2)

    def test_over_ten_mb_is_refused(self):
        with self.assertRaisesRegex(ValueError, "over 10MB"):
            serve._parse_attachments({"attachments": [_att(6 * MB, "a.pdf"), _att(4 * MB + 1, "b.pdf")]})

    def test_the_request_limit_fits_ten_mb_of_base64(self):
        self.assertGreater(serve.MAX_SEND_BODY, serve.ATT_MAX * 4 / 3 + 200_000)


if __name__ == "__main__":
    unittest.main()
