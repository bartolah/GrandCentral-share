"""E267: images pasted into an email (cid:) are addressable parts.

Casey, 2026-10-02: "I'm not seeing inline images in my emails render." The
pane points <img src="cid:…"> at /attachment?idx=, so parse_msg's chips and
fetch_attachment's part walk must count parts the same way, and every
embedded image needs its Content-ID. Offline: a hand-built MIME message.
"""

from __future__ import annotations

import os
import sys
import unittest
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import mail_common as mc  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _message() -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@x.org", "b@y.org", "logo"
    msg.set_content("plain")
    msg.add_alternative('<p>hi</p><img src="cid:logo@x"><img src="cid:shot.png@01DB">', subtype="html")
    html = msg.get_payload()[1]
    html.add_related(PNG, "image", "png", cid="<logo@x>")                       # no filename
    html.add_related(PNG, "image", "png", cid="<shot.png@01DB>", filename="shot.png")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="deck.pdf")
    return msg.as_bytes()


class InlineImages(unittest.TestCase):
    def test_embedded_images_are_attachments_with_their_cid(self):
        m = mc.parse_msg(_message(), want_html=True)
        self.assertEqual([(a["filename"], a["cid"]) for a in m["attachments"]],
                         [("(unnamed)", "logo@x"), ("shot.png", "shot.png@01DB"), ("deck.pdf", "")])
        self.assertIn("cid:logo@x", m["body_html"])

    def test_chips_and_download_walk_agree(self):
        import email
        msg = email.message_from_bytes(_message())
        walked = [p.get_content_type() for p in msg.walk() if mc.is_attachment_part(p)]
        chips = [a["content_type"] for a in mc.parse_msg(_message())["attachments"]]
        self.assertEqual(walked, chips)

    def test_download_uses_the_shared_rule(self):
        src = open(os.path.join(ROOT, "sweeper", "mail_actions.py"), encoding="utf-8").read()
        body = src[src.index("def fetch_attachment"):]
        self.assertIn("mc.is_attachment_part(part)", body[:body.index("\ndef ", 10)])


if __name__ == "__main__":
    unittest.main()
