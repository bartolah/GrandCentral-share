"""Your own services get their own bundles (2026-09-30).

Bart: "Let's make Jev smarter about email notifications from our own services
and properties and websites … ConnectBlue Notifications should be a bundle,
ConnectBlue Sales inquiries (RFPs) from sales@connectblue, etc." Design agreed
in chat: automatic and editable, one bundle per service (subdomain), website
RFPs kept prominent. Pure functions over header dicts; fictional domains.

    .venv/bin/python -m unittest tests.test_services -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import services as S  # noqa: E402

OWN = {"bluenet.org", "bluenet.net", "redco.com"}
GROUPS = {"bluenet.org": "BlueNet", "bluenet.net": "BlueNet", "redco.com": "RedCo"}
PEOPLE = {"gina@bluenet.org"}


def det(frm, subject="", bulk=False, list_unsub=False):
    meta = {"from": frm, "subject": subject, "bulk": bulk, "list_unsub": list_unsub}
    return S.detect(meta, OWN, PEOPLE, GROUPS)


class Detect(unittest.TestCase):
    def test_a_system_on_a_subdomain_is_a_service_keyed_by_that_subdomain(self):
        d = det("BlueNet <no-reply@salesdb.bluenet.org>")
        self.assertEqual((d["key"], d["kind"], d["name"]), ("salesdb.bluenet.org", "service", "BlueNet"))
        # every address on that subdomain is the same service
        self.assertEqual(det("BlueNet Events <events@salesdb.bluenet.org>")["key"], "salesdb.bluenet.org")

    def test_an_automated_address_on_the_bare_domain_is_keyed_by_address(self):
        d = det("BlueNet Notifications <no-reply@bluenet.org>", bulk=True)
        self.assertEqual((d["key"], d["name"]), ("no-reply@bluenet.org", "BlueNet Notifications"))

    def test_people_are_never_services(self):
        self.assertIsNone(det("Gina <gina@bluenet.org>", bulk=True))          # on the roster
        self.assertIsNone(det("Sam <sam@bluenet.org>"))                        # a person writing
        self.assertIsNone(det("Pat <pat@elsewhere.com>", bulk=True))           # not our domain
        self.assertIsNone(det("No Reply <no-reply@bluenetx.org>", bulk=True))  # a lookalike

    def test_website_rfps_through_sales_are_sales_inquiries(self):
        d = det("'BlueNet Site' via Sales <sales@bluenet.org>", "New RFP: Kimmies", bulk=True)
        self.assertEqual((d["kind"], d["name"], d["key"]), ("sales", "Sales inquiries", "sales@bluenet.org"))
        # the subject alone is enough
        self.assertEqual(det("'Web Form' via Sales <sales@bluenet.org>", "New RFP: x", bulk=True)["kind"], "sales")

    def test_a_person_posting_through_a_group_stays_a_person(self):
        self.assertIsNone(det("'Dev Banerjee' via Sales <sales@bluenet.org>", "Re: pricing", bulk=True))
        self.assertIsNone(det("'Ro Khanna' via support <support@redco.com>", "Thank you", bulk=True))

    def test_a_tool_posting_through_a_group_is_that_group_s_service(self):
        d = det("'No Reply - Mailchimp' via Admin <admin@redco.com>", "Your campaign", bulk=True)
        self.assertEqual((d["key"], d["kind"], d["name"]), ("admin@redco.com", "service", "RedCo Admin"))
        self.assertEqual(det("'SocialBee Support' via content <content@redco.com>", bulk=True)["name"], "RedCo content")

    def test_group_names_lose_their_quotes_and_a_doubled_company(self):
        d = det('"\'Instagram\' via RedCo Campaigns" <campaigns@redco.com>', bulk=True)
        self.assertEqual(d["name"], "RedCo Campaigns")
        d = det('"noreply-spamdigest via accounts" <accounts@redco.com>', bulk=True)
        self.assertEqual(d["name"], "RedCo accounts")

    def test_bart_s_own_mailings_are_a_service_named_for_the_company(self):
        a, b = det("Your Name <bart@bluenet.net>", bulk=True), det("Bart from BlueNet <partners@bluenet.net>", bulk=True)
        self.assertEqual((a["name"], a["key"]), ("BlueNet mailings", "mailings@bluenet.net"))
        self.assertEqual(b["key"], a["key"])                  # one bundle, whichever address

    def test_outbound_mail_is_never_a_service(self):
        self.assertIsNone(S.detect({"from": "Bart <bart@bluenet.org>", "subject": "", "bulk": True},
                                   OWN, PEOPLE, GROUPS, own_addresses={"bart@bluenet.org"}))


class Category(unittest.TestCase):
    def test_service_mail_leaves_people_so_jev_never_sees_it(self):
        svc = {"kind": "service"}
        self.assertEqual(S.category_for(svc, "people"), "notification")
        self.assertEqual(S.category_for(svc, "other"), "notification")
        self.assertEqual(S.category_for(svc, "newsletter"), "newsletter")   # already bulk: unchanged

    def test_sales_inquiries_are_people_mail_jev_reads(self):
        self.assertEqual(S.category_for({"kind": "sales"}, "promo"), "people")
        self.assertEqual(S.category_for(None, "promo"), "promo")


class Store(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_services_register_once_and_keep_bart_s_edits(self):
        S.register(self.conn, [{"key": "salesdb.bluenet.org", "name": "BlueNet", "kind": "service"}])
        S.edit(self.conn, "salesdb.bluenet.org", name="SalesDB")
        S.register(self.conn, [{"key": "salesdb.bluenet.org", "name": "BlueNet", "kind": "service"}])
        got = S.table(self.conn)["salesdb.bluenet.org"]
        self.assertEqual((got["name"], got["auto_name"]), ("SalesDB", "BlueNet"))

    def test_merge_and_switch_off_resolve(self):
        S.register(self.conn, [{"key": "a.bluenet.org", "name": "A", "kind": "service"},
                               {"key": "b.bluenet.org", "name": "B", "kind": "service"}])
        S.edit(self.conn, "b.bluenet.org", merged_into="a.bluenet.org")
        t = S.table(self.conn)
        self.assertEqual(S.resolve("b.bluenet.org", t), "a.bluenet.org")
        S.edit(self.conn, "a.bluenet.org", off=True)
        self.assertIsNone(S.resolve("b.bluenet.org", S.table(self.conn)))    # off follows the merge
        with self.assertRaises(ValueError):
            S.edit(self.conn, "a.bluenet.org", merged_into="a.bluenet.org")

    def test_duplicate_names_get_their_subdomain(self):
        S.register(self.conn, [{"key": "salesdb.bluenet.org", "name": "BlueNet", "kind": "service"},
                               {"key": "partners@bluenet.net", "name": "BlueNet", "kind": "service"}])
        names = {k: v["label"] for k, v in S.table(self.conn).items()}
        self.assertEqual(names["salesdb.bluenet.org"], "BlueNet · salesdb")
        self.assertEqual(names["partners@bluenet.net"], "BlueNet · partners")

    def test_settings_lists_each_account_s_services_with_counts(self):
        S.register(self.conn, [{"key": "sales@bluenet.org", "name": "Sales inquiries", "kind": "sales"},
                               {"key": "a.bluenet.org", "name": "A", "kind": "service"}])
        for acct, tid, svc in (("acctone", "1", "a.bluenet.org"), ("acctone", "2", "a.bluenet.org"),
                               ("acctone", "3", "sales@bluenet.org"), ("accttwo", "1", "a.bluenet.org")):
            self.conn.execute("INSERT INTO mail_threads (thread_id, account, subject, service) "
                              "VALUES (?, ?, 's', ?)", (tid, acct, svc))
        got = S.listing(self.conn)
        self.assertEqual([(r["key"], r["count"]) for r in got["acctone"]],
                         [("sales@bluenet.org", 1), ("a.bluenet.org", 2)])     # sales first
        self.assertEqual([r["count"] for r in got["accttwo"]], [1])


if __name__ == "__main__":
    unittest.main()
