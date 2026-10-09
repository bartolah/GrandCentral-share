"""Bundle categorisation rules (E205).

categorize() decides which bundle every incoming email lands in, and until now
it had no tests at all. The bug it grew: List-Unsubscribe was tested before
every more specific rule, and since Gmail and Yahoo have required that header
on bulk mail since 2024, it swallowed the inbox — 11,601 of ~19,000 threads
(61%) were filed 'newsletter', including Casey's own product's no-reply alerts
and every store's sale blast.

The fix makes List-Unsubscribe the weakest signal instead of the strongest.
These tests pin the precedence so it cannot silently invert again.

Pure functions over header dicts — no DB, no network, no Gmail.

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

from mail_sync import categorize  # noqa: E402


def meta(frm, subject="", *, list_unsub=False, bulk=False, content_type="",
         feedback_id="", list_unsub_url=""):
    return {"from": frm, "subject": subject, "list_unsub": list_unsub,
            "bulk": bulk, "content_type": content_type,
            "feedback_id": feedback_id, "list_unsub_url": list_unsub_url}


class ListUnsubscribeIsTheWeakestSignal(unittest.TestCase):
    """The regression that started E205: specific rules get first refusal."""

    def test_app_noreply_sender_beats_list_unsubscribe(self):
        # Casey's own product mail: 265 threads that matched the no-reply rule
        # perfectly but were filed 'newsletter' because the header won first.
        self.assertEqual(
            categorize(meta("no-reply@notice.example-app.com",
                            "New partner created: Indivisible", list_unsub=True)),
            "notification")

    def test_promo_blast_beats_list_unsubscribe(self):
        self.assertEqual(
            categorize(meta("shop@email.example-store.com",
                            "Summer Sale: 40-60% off everything", list_unsub=True)),
            "promo")

    def test_bulk_with_nothing_specific_is_still_a_newsletter(self):
        # The header still does its job last: bulk mail from a sender whose
        # address gives nothing away has no better home than 'newsletter'
        # until the steward curates it. (Until E223 this example was a PAC
        # blast; those are promos now — see FundraisingBlastsArePromos.)
        self.assertEqual(
            categorize(meta("info@e.somemagazine.com",
                            "This week in review", list_unsub=True)),
            "newsletter")


class EditorialNewslettersSurvive(unittest.TestCase):
    """_APP_LOCAL_RE also matches the token 'newsletters', so the editorial
    rule has to be tested before it or real newsletters become notifications."""

    def test_newsletters_at_is_a_newsletter_not_a_notification(self):
        self.assertEqual(
            categorize(meta("newsletters@example-paper.com", "Morning Report",
                            list_unsub=True)),
            "newsletter")

    def test_digest_at_is_a_newsletter(self):
        self.assertEqual(
            categorize(meta("digest@example.com", "Weekly digest", list_unsub=True)),
            "newsletter")


class PromoNeedsStructuralBulkness(unittest.TestCase):
    """The one genuinely bad failure mode is burying a real person in a bundle,
    because bundled mail never accrues reply debt. Content sniffing is gated on
    bulk-ness precisely so that cannot happen."""

    def test_a_person_writing_last_chance_is_still_a_person(self):
        self.assertEqual(
            categorize(meta("jane@example.com",
                            "Last chance to join us for dinner tonight!")),
            "people")

    def test_a_person_mentioning_a_sale_is_still_a_person(self):
        self.assertEqual(
            categorize(meta("colleague@partner.org",
                            "Re: the sale closed, 30% off was the final number")),
            "people")

    def test_the_same_subject_from_a_bulk_sender_is_a_promo(self):
        self.assertEqual(
            categorize(meta("marketing@brand.com", "30% off this weekend only",
                            list_unsub=True)),
            "promo")

    def test_promo_sender_local_is_enough_when_bulk(self):
        self.assertEqual(
            categorize(meta("promo@e.example-health.com",
                            "Health insights all weekend", list_unsub=True)),
            "promo")


class StrongerRulesStillWinFirst(unittest.TestCase):

    def test_curated_sender_override_beats_everything(self):
        smap = {"newsletters@example-paper.com": "people"}
        self.assertEqual(
            categorize(meta("newsletters@example-paper.com", "40% off!",
                            list_unsub=True, bulk=True), smap),
            "people")

    def test_calendar_beats_promo(self):
        self.assertEqual(
            categorize(meta("someone@example.com", "Invitation: Sale planning sync",
                            content_type="text/calendar", list_unsub=True)),
            "calendar")

    def test_receipt_beats_promo(self):
        self.assertEqual(
            categorize(meta("orders@shop.com", "Your order has shipped — 20% off next time",
                            list_unsub=True)),
            "receipt")


class FundraisingBlastsArePromos(unittest.TestCase):
    """E223 (Dev #22): campaign and PAC asks go to Promos, told by the sending
    platform or the sender's name, never by their clickbait subjects."""

    def test_actionkit_feedback_id_is_a_promo(self):
        m = meta("Roy Cooper <info@e.roycooper.com>", "Bad news, team", list_unsub=True,
                 feedback_id="7781113a:b:3875.mailingid:actionkit")
        self.assertEqual(categorize(m), "promo")

    def test_ngpvan_unsubscribe_is_a_promo(self):
        m = meta("info@timkaine.com", "Three great candidates", list_unsub=True,
                 list_unsub_url="<https://secure.ngpvan.com/Unsubscribe?x=1>")
        self.assertEqual(categorize(m), "promo")

    def test_pac_and_party_committee_senders_are_promos(self):
        for frm in ("info@progressiveresistancepac.com", "turnoutpac@substack.com",
                    "dccc@ak.dccc.org"):
            self.assertEqual(categorize(meta(frm, "Hi", list_unsub=True)), "promo", frm)

    def test_editorial_newsletters_stay_newsletters(self):
        for frm in ("mike@axios.com", "info@news.dailykos.com",
                    "roycoopernc@substack.com"):
            m = meta(frm, "Today", list_unsub=True, list_unsub_url="<https://link.axios.com/u>")
            self.assertEqual(categorize(m), "newsletter", frm)

    def test_a_person_at_a_campaign_is_still_a_person(self):
        # Not bulk: a staffer writing to Casey is never a fundraising blast.
        m = meta("Jane <jane@victorypac.org>", "Coffee Tuesday?")
        self.assertEqual(categorize(m), "people")


class OwnDomainsAreNeverPromos(unittest.TestCase):
    """Dev #55: "Any email from my official domain shouldn't be a promotion."
    Brightline's sales@ is a Google Group, so its RFPs arrive bulk-flagged."""
    OWN = {"brightline.example", "fieldhouse.example"}

    def test_a_google_group_post_on_the_own_domain_is_people(self):
        m = meta("'Dev Banerjee' via Sales <sales@fieldhouse.example>", "New RFP: Kimmies",
                 list_unsub=True, bulk=True,
                 list_unsub_url="<mailto:googlegroups-manage+1+unsubscribe@googlegroups.com>")
        self.assertEqual(categorize(m, own=self.OWN), "people")
        self.assertEqual(categorize(m), "promo")      # without the rule, what Casey saw

    def test_a_no_reply_on_the_own_domain_is_an_update(self):
        m = meta("no-reply@fieldhouse.example", "Your weekly stats", list_unsub=True, bulk=True)
        self.assertEqual(categorize(m, own=self.OWN), "notification")

    def test_a_subdomain_is_an_update_at_most(self):
        m = meta("offers@news.brightline.example", "50% off sale", list_unsub=True, bulk=True)
        self.assertEqual(categorize(m, own=self.OWN), "notification")

    def test_casey_s_own_override_still_wins(self):
        m = meta("contact@brightline.example", "Form", list_unsub=True)
        self.assertEqual(categorize(m, {"contact@brightline.example": "newsletter"}, own=self.OWN), "newsletter")

    def test_a_lookalike_domain_is_not_own(self):
        m = meta("sales@notbrightline.example", "Sale on now", list_unsub=True)
        self.assertEqual(categorize(m, own=self.OWN), "promo")

    def test_public_mailboxes_are_never_own(self):
        import mail_sync
        env = {"GMAIL_PERSONAL_USER": "casey@gmail.com", "GMAIL_CB_USER": "casey@brightline.example",
               "GMAIL_CB_APP_PASSWORD": "x"}
        self.assertEqual(mail_sync.own_domains(env), {"brightline.example"})


class PlainMailIsUntouched(unittest.TestCase):

    def test_ordinary_person_is_people(self):
        self.assertEqual(categorize(meta("roman@example.com", "Re: lunch on Thursday")),
                         "people")

    def test_unlabelled_bulk_is_a_notification(self):
        self.assertEqual(categorize(meta("x@example.com", "something", bulk=True)),
                         "notification")


if __name__ == "__main__":
    unittest.main()
