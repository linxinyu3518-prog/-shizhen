# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory session boundaries, using synthetic cookies only."""
from http.cookiejar import CookieJar, DefaultCookiePolicy
import unittest
from unittest.mock import patch
from urllib.request import Request

from framekeep.network import MediaError
from framekeep.sessions import SessionStore


HEADER = "# Netscape HTTP Cookie File\n"


def row(domain=".douyin.com", include="TRUE", path="/", secure="TRUE", expiry="0", name="session", value="synthetic"):
    return "\t".join((domain, include, path, secure, expiry, name, value))


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore()

    def import_rows(self, *rows):
        return self.store.import_text("douyin", HEADER + "\n".join(rows))

    def test_status_clear_and_separate_store_are_memory_only(self):
        empty = {"configured": False, "count": 0, "platform": "douyin", "storage": "memory"}
        self.assertEqual(self.store.status("douyin"), empty)
        self.assertEqual(self.import_rows(row()), {**empty, "configured": True, "count": 1})
        self.assertEqual(SessionStore().status("douyin"), empty)
        self.assertEqual(self.store.clear("douyin"), empty)
        self.assertEqual(self.store.snapshot("douyin"), ())

    def test_session_cookie_and_http_only_records(self):
        self.import_rows(row(domain="#HttpOnly_.douyin.com", value="a=b:c"))
        cookie, = self.store.snapshot("douyin")
        self.assertIsNone(cookie.expires)
        self.assertTrue(cookie.discard)
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.has_nonstandard_attr("HTTPOnly"))
        self.assertEqual(cookie.value, "a=b:c")

    def test_alternative_header_bom_comments_crlf_and_empty_value(self):
        text = "\ufeff# HTTP Cookie File\r\n# comment\r\n\r\n" + row(value="") + "\r\n"
        self.assertEqual(self.store.import_text("douyin", text)["count"], 1)
        self.assertEqual(self.store.snapshot("douyin")[0].value, "")

    def test_exact_and_subdomains_allowed_unrelated_and_spoofed_domains_filtered(self):
        self.import_rows(*(row(domain=domain, name=str(index)) for index, domain in enumerate((
            ".douyin.com", "www.douyin.com", ".iesdouyin.com", ".www.iesdouyin.com",
            ".example.org", ".evildouyin.com", ".douyin.com.example.org",
        ))))
        self.assertEqual(self.store.status("douyin")["count"], 4)
        self.assertEqual({cookie.domain for cookie in self.store.snapshot("douyin")},
                         {".douyin.com", ".www.douyin.com", ".iesdouyin.com", ".www.iesdouyin.com"})

    def test_flags_determine_host_only_and_parent_domain_scope(self):
        self.import_rows(row(domain="douyin.com", name="parent"),
                         row(domain=".www.douyin.com", include="FALSE", name="host"))
        parent, host = self.store.snapshot("douyin")
        self.assertEqual((parent.domain, parent.domain_specified, parent.domain_initial_dot),
                         (".douyin.com", True, True))
        self.assertEqual((host.domain, host.domain_specified, host.domain_initial_dot),
                         ("www.douyin.com", False, False))

    def test_cookiejar_headers_respect_domain_path_secure_and_spoof_hosts(self):
        self.import_rows(row(path="/video", name="parent"),
                         row(domain="www.douyin.com", include="FALSE", path="/video", name="host"))
        # Python's default legacy policy is liberal about host-only cookies;
        # DomainStrictNonDomain gives browser-style matching for this assertion.
        jar = CookieJar(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
        for cookie in self.store.snapshot("douyin"):
            jar.set_cookie(cookie)
        expected = {
            "https://www.douyin.com/video/123": "parent=synthetic; host=synthetic",
            "https://child.www.douyin.com/video/123": "parent=synthetic",
            "https://douyin.com/video/123": "parent=synthetic",
            "https://www.douyin.com/videography": None,
            "https://www.douyin.com/user/self": None,
            "http://www.douyin.com/video/123": None,
            "https://evildouyin.com/video/123": None,
            "https://douyin.com.evil.example/video/123": None,
            "https://www.iesdouyin.com/video/123": None,
        }
        for url, header in expected.items():
            with self.subTest(url=url):
                request = Request(url)
                jar.add_cookie_header(request)
                self.assertEqual(request.get_header("Cookie"), header)

    def test_expired_cookies_ignored_and_status_expires_dynamically(self):
        with patch("framekeep.sessions.time.time", return_value=1000):
            self.import_rows(row(expiry="999", name="old"), row(expiry="1001", name="live"))
            self.assertEqual(self.store.status("douyin")["count"], 1)
        with patch("framekeep.sessions.time.time", return_value=1002):
            self.assertFalse(self.store.status("douyin")["configured"])
            self.assertEqual(self.store.snapshot("douyin"), ())

    def test_duplicate_cookie_last_row_wins(self):
        self.import_rows(row(value="first"), row(value="last"))
        self.assertEqual(self.store.status("douyin")["count"], 1)
        self.assertEqual(self.store.snapshot("douyin")[0].value, "last")
        with patch("framekeep.sessions.time.time", return_value=1000):
            self.import_rows(row(name="other"), row(value="first"), row(expiry="999", value="expired"))
        self.assertEqual([cookie.name for cookie in self.store.snapshot("douyin")], ["other"])

    def test_snapshot_isolation_survives_mutation_reimport_and_clear(self):
        self.import_rows(row(domain="#HttpOnly_.douyin.com", value="first"))
        first = self.store.snapshot("douyin")
        first[0].value = "mutated"
        first[0].set_nonstandard_attr("added", "flag")
        fresh = self.store.snapshot("douyin")
        self.assertEqual(fresh[0].value, "first")
        self.assertFalse(fresh[0].has_nonstandard_attr("added"))
        self.import_rows(row(value="second"))
        self.store.clear("douyin")
        self.assertEqual(fresh[0].value, "first")

    def test_failed_import_preserves_previous_session(self):
        self.import_rows(row())
        with self.assertRaises(MediaError):
            self.store.import_text("douyin", "invalid")
        self.assertTrue(self.store.status("douyin")["configured"])

    def test_headers_sizes_and_record_count_are_bounded(self):
        for text in (None, "", row(), "# Wrong header\n" + row(),
                     HEADER + "#" + "a" * (256 * 1024), HEADER + "#" + "字" * 100000,
                     HEADER + "\n".join(row(name=str(n)) for n in range(3001))):
            with self.subTest(kind=type(text).__name__), self.assertRaises(MediaError):
                self.store.import_text("douyin", text)

    def test_malformed_rows_report_only_row_number(self):
        secret = "private-cookie-value"
        invalid = [
            row(value=secret) + "\textra", row(include="true", value=secret),
            row(secure="false", value=secret), row(expiry="-1", value=secret),
            row(expiry="1.5", value=secret), row(expiry="", value=secret),
            row(expiry="１２３", value=secret), row(expiry=str(2**63), value=secret),
            row(domain=".douyin.com.", value=secret), row(domain="..douyin.com", value=secret),
            row(domain="https://douyin.com", value=secret), row(domain="a_:80.douyin.com", value=secret),
            row(path="relative", value=secret), row(path="/bad\x00path", value=secret),
            row(name="bad;name", value=secret), row(name="", value=secret),
            row(name="x" * 257, value=secret), row(value="x" * 16385),
            row(value=secret + "\rhidden"), row(value=secret + "\x0bhidden"),
            row(value=secret + "\x7fhidden"), row(value=secret + "\thidden"),
        ]
        for text in invalid:
            with self.subTest(index=invalid.index(text)):
                with self.assertRaises(MediaError) as error:
                    self.import_rows(text)
                self.assertIn("第 2 行", str(error.exception))
                self.assertNotIn(secret, str(error.exception))

    def test_no_relevant_live_cookies_is_an_error(self):
        for text in (HEADER + "# comment", HEADER + row(domain=".example.org"), HEADER + row(expiry="1")):
            with self.subTest(text=text), self.assertRaisesRegex(MediaError, "没有可用"):
                self.store.import_text("douyin", text)

    def test_unsupported_platforms_rejected_consistently(self):
        for platform in (None, "", "youtube", "Douyin", "douyin.com"):
            for action in (self.store.status, self.store.snapshot, self.store.clear):
                with self.subTest(platform=platform, action=action.__name__), self.assertRaises(MediaError):
                    action(platform)
            with self.assertRaises(MediaError):
                self.store.import_text(platform, HEADER + row())


if __name__ == "__main__":
    unittest.main()
