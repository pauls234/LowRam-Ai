"""internet.py: web search and reading pages – public addresses only, checked redirects, limits, sources."""

import gzip
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import quote

from backend import internet, settings
from tests import doc_samples as ds
from tests.chat_helpers import Stream
from tests.helpers import LowRAMTestCase
from tests.test_chat import ChatApiTestCase

DDG_PAGE = """<html><body>
<div class="result results_links result--ad"><a class="result__a" href="https://ads.example/buy">Anzeige</a>
  <a class="result__snippet">Kaufen!</a></div>
<div class="result results_links web-result"><h2><a rel="nofollow" class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa%3Fx%3D1&amp;rut=abc">Erster &amp; bester Treffer</a></h2>
  <a class="result__snippet" href="#">Ein <b>kurzer</b> Auszug.</a></div>
<div class="result results_links web-result"><a class="result__a" href="https://example.net/b">Zweiter</a>
  <div class="result__snippet">Noch ein Auszug.</div></div>
<div class="result"><a class="result__a" href="javascript:alert(1)">Böse</a></div>
</body></html>"""


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        handler = self.server.routes.get(self.path.split("?")[0])
        if handler is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, headers, body = handler(self)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET  # noqa: N815


class InternetTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        self.site.daemon_threads = True
        self.site.routes = {}
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.port = self.site.server_address[1]
        ports = mock.patch.object(internet, "PORTS", internet.PORTS | {self.port})
        ports.start()
        self.addCleanup(ports.stop)
        # the test site runs on 127.0.0.1: exactly this address gets through the public-address check
        real = internet.public_address
        allow = mock.patch.object(internet, "public_address", side_effect=lambda ip: ip == "127.0.0.1" or real(ip))
        allow.start()
        self.addCleanup(allow.stop)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def page(self, path, body, content_type="text/html; charset=utf-8", status=200, headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.site.routes[path] = lambda handler: (status, {"Content-Type": content_type, **(headers or {})}, data)


class AddressTests(unittest.TestCase):
    def test_only_public_addresses(self):
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.178.1", "172.16.0.1", "169.254.169.254", "100.64.0.1",
                   "0.0.0.0", "224.0.0.251", "::1", "fe80::1", "fc00::1", "::ffff:192.168.0.1", "not-an-ip"):
            self.assertFalse(internet.public_address(ip), ip)
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
            self.assertTrue(internet.public_address(ip), ip)

    def test_only_web_links(self):
        for url in ("ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "https://user:pw@example.com/",
                    "https://example.com:22/", "https:///nohost", "", "x" * 3000, None):
            with self.assertRaises(internet.InternetError, msg=url):
                internet.validate_url(url)
        self.assertEqual(internet.validate_url(" https://example.com/a?b=1 ").hostname, "example.com")

    def test_names_that_point_into_the_home_network_are_refused(self):
        with mock.patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.0.1", 80))]):
            with self.assertRaisesRegex(internet.InternetError, "eigenen Netz"):
                internet.fetch("http://router.example/")


class FetchTests(InternetTestCase):
    def test_html_page(self):
        self.page("/a", "<html><head><title>Test &amp; Seite</title><script>evil()</script></head>"
                        "<body><h1>Überschrift</h1><p>Inhalt der Seite.</p></body></html>")
        page = internet.fetch(self.url("/a"))
        self.assertEqual((page["title"], page["kind"]), ("Test & Seite", "html"))
        self.assertEqual(page["text"], "# Überschrift\n\nInhalt der Seite.")

    def test_redirects_are_checked_again(self):
        self.page("/start", "", status=302, headers={"Location": "/ziel"})
        self.page("/ziel", "<p>Angekommen</p>")
        self.assertEqual(internet.fetch(self.url("/start"))["text"], "Angekommen")
        self.page("/evil", "", status=302, headers={"Location": "http://10.255.255.1/admin"})
        with self.assertRaisesRegex(internet.InternetError, "eigenen Netz"):
            internet.fetch(self.url("/evil"))
        self.page("/loop", "", status=302, headers={"Location": "/loop"})
        with self.assertRaisesRegex(internet.InternetError, "zu oft"):
            internet.fetch(self.url("/loop"))

    def test_limits_and_errors(self):
        bomb = gzip.compress(b"<p>" + b"A" * 5_000_000 + b"</p>")
        self.page("/bomb", bomb, headers={"Content-Encoding": "gzip"})
        with mock.patch.object(internet, "MAX_PDF_BYTES", 100_000):
            page = internet.fetch(self.url("/bomb"))
        self.assertTrue(page["truncated"])
        self.assertLessEqual(len(page["text"]), 100_000)
        self.page("/img", b"\x89PNG....", content_type="image/png")
        with self.assertRaisesRegex(internet.InternetError, "kein Text"):
            internet.fetch(self.url("/img"))
        with self.assertRaisesRegex(internet.InternetError, "404"):
            internet.fetch(self.url("/missing"))

    def test_pdf_and_text(self):
        self.page("/doc.pdf", ds.pdf(["Ein PDF aus dem Internet mit genug Text."]), content_type="application/pdf")
        page = internet.fetch(self.url("/doc.pdf"))
        self.assertEqual((page["kind"], page["text"]), ("pdf", "[Seite 1]\nEin PDF aus dem Internet mit genug Text."))
        self.page("/t.txt", "Nur Text", content_type="text/plain")
        self.assertEqual(internet.fetch(self.url("/t.txt"))["text"], "Nur Text")

    def test_a_slow_page_is_stopped(self):
        def slow(handler):
            time.sleep(1.5)
            return 200, {"Content-Type": "text/html"}, b"<p>zu spaet</p>"

        self.site.routes["/slow"] = slow
        with mock.patch.object(internet, "TIMEOUT_S", 0.5):
            with self.assertRaises(internet.InternetError):
                internet.fetch(self.url("/slow"))


class SearchTests(InternetTestCase):
    def test_duckduckgo_results_without_ads(self):
        self.page("/html/", DDG_PAGE)
        with mock.patch.object(internet, "DDG_URL", self.url("/html/")):
            results = internet.search("  Wetter   Köln ")
        self.assertEqual(results, [
            {"title": "Erster & bester Treffer", "url": "https://example.org/a?x=1", "snippet": "Ein kurzer Auszug."},
            {"title": "Zweiter", "url": "https://example.net/b", "snippet": "Noch ein Auszug."},
        ])

    def test_a_blocked_search_says_so(self):
        self.page("/html/", "<html><body>Unfortunately, bots use DuckDuckGo too. anomaly</body></html>", status=202)
        with mock.patch.object(internet, "DDG_URL", self.url("/html/")):
            with self.assertRaisesRegex(internet.InternetError, "abgelehnt"):
                internet.search("x")

    def test_own_searxng_instance(self):
        body = json.dumps({"results": [{"title": "S", "url": "https://s.example/", "content": "Auszug"},
                                       {"title": "bad", "url": "file:///x"}]})
        self.page("/search", body, content_type="application/json")
        settings.update("internet", {"searx_url": self.url("")})
        with mock.patch.object(internet, "public_address", return_value=False):  # a LAN instance is fine when chosen
            results = internet.search("frage")
        self.assertEqual(results, [{"title": "S", "url": "https://s.example/", "snippet": "Auszug"}])


class ResearchTests(InternetTestCase):
    def test_links_in_the_question_are_read(self):
        self.page("/artikel", "<title>Artikel</title><p>Zeile ohne Bezug, aber lang genug für einen Absatz.</p>"
                              "<p>Die Antwort auf die Frage nach Python ist hier zu finden, ganz sicher.</p>")
        seen = []
        block, sources = internet.research(f"Fasse {self.url('/artikel')}. zusammen – Python?", 4000, seen.append)
        self.assertTrue(block.startswith(internet.WEB_HEADER))
        self.assertIn(f"[1] Artikel – {self.url('/artikel')}", block)
        self.assertEqual([(s["kind"], s["title"], s["url"]) for s in sources], [("web", "Artikel", self.url("/artikel"))])
        self.assertEqual(seen[-1]["percent"], 95)
        self.assertEqual({item["phase"] for item in seen}, {"web"})

    def test_search_then_read_with_fallback_to_the_snippet(self):
        page = DDG_PAGE.replace("https%3A%2F%2Fexample.org%2Fa%3Fx%3D1", quote(self.url("/gut"), safe=""))
        self.page("/html/", page)
        self.page("/gut", "<title>Gut</title><p>Hier steht etwas Nützliches über das Thema, mehr als dreißig Zeichen.</p>")
        with mock.patch.object(internet, "DDG_URL", self.url("/html/")):
            block, sources = internet.research("Thema", 4000)
        self.assertEqual([s["title"] for s in sources], ["Gut", "Zweiter"])  # the 2nd page failed: its snippet
        self.assertIn("Noch ein Auszug.", block)

    def test_nothing_found(self):
        self.page("/html/", "<html><body>keine Treffer</body></html>")
        with mock.patch.object(internet, "DDG_URL", self.url("/html/")):
            block, sources = internet.research("xyz", 4000)
        self.assertEqual(sources, [])
        self.assertIn("nicht geklappt", block)


class ChatWebTests(ChatApiTestCase):
    def test_internet_switch_in_the_chat(self):
        sources = [{"kind": "web", "title": "Quelle", "url": "https://q.example/", "snippet": "s"}]

        def research(query, budget, on_progress=None):
            on_progress({"text": "Lese Seite 1 von 1", "phase": "web", "percent": 50})
            return "Web-Quellen …\n\n[1] Quelle – https://q.example/\nWISSEN AUS DEM NETZ", sources

        self.fake.push(Stream(["Antwort [1]"]))
        cid = self.new_chat()
        with mock.patch.object(internet, "research", side_effect=research) as called:
            events = self.send(cid, "Was gibt es Neues?", web=True).rest()
        self.assertEqual(called.call_args[0][0], "Was gibt es Neues?")
        done = next(e for e in events if e.get("type") == "done")
        self.assertEqual(done["message"]["sources"], sources)  # shown at once, not only after a reload
        status = [e for e in events if e.get("type") == "status" and e.get("phase") == "web"]
        self.assertEqual(status[0]["percent"], 50)
        prompt = json.dumps(self.fake.requests[-1]["json"]["messages"], ensure_ascii=False)
        self.assertIn("WISSEN AUS DEM NETZ", prompt)
        self.assertEqual(self.messages(cid)[-1]["sources"], sources)
        with mock.patch.object(internet, "research") as never:  # without the switch nothing goes online
            self.send(cid, "Und ohne?").rest()
        never.assert_not_called()

    def test_a_failed_search_is_told_not_hidden(self):
        self.fake.push(Stream(["Leider ohne Internet."]))
        cid = self.new_chat()
        with mock.patch.object(internet, "search", side_effect=internet.InternetError("kein Netz")):
            events = self.send(cid, "Wetter?", web=True).rest()
        self.assertTrue(any("kein Netz" in str(e.get("text")) for e in events if e.get("type") == "status"))
        self.assertIn("nicht geklappt (kein Netz)", json.dumps(self.fake.requests[-1]["json"]["messages"],
                                                              ensure_ascii=False))
        self.assertEqual(self.messages(cid)[-1]["status"], "complete")

    def test_switched_off_in_the_settings(self):
        settings.update("internet", {"enabled": False})
        cid = self.new_chat()
        refused = self.send(cid, "x", web=True)
        self.assertEqual(refused.status, 403)
        self.assertEqual(self.api("GET", "/api/internet")[1]["enabled"], False)


if __name__ == "__main__":
    unittest.main()
