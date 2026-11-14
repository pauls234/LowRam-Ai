import http.client
import json
import random
import threading
import time
import unittest

from backend import app, db, knowledge
from tests.helpers import LowRAMTestCase

TOKEN = "t"


class TextTests(unittest.TestCase):
    def test_tokenize_lowercase_umlauts_stopwords(self):
        self.assertEqual(knowledge.tokenize("Die Größe der Äpfel und the APPLE_pie!"),
                         ["groesse", "aepfel", "applepie"])
        self.assertEqual(knowledge.tokenize("Müller"), knowledge.tokenize("MUELLER"))
        self.assertEqual(knowledge.tokenize("über für"), [])  # folded stopwords

    def test_chunks_size_overlap_and_boundaries(self):
        sentence = "Dies ist ein ganz normaler Satz über Wissen. "
        text = "\n\n".join(sentence * 6 for _ in range(20))
        chunks = knowledge.chunk_text(text)
        self.assertGreater(len(chunks), 5)
        for piece in chunks:
            self.assertLessEqual(len(piece), knowledge.CHUNK_CHARS)
            self.assertTrue(piece.endswith("."), piece[-30:])  # cut at a sentence/paragraph end
        joined = " ".join(" ".join(chunks).split())
        for word in ("Dies", "Wissen."):
            self.assertIn(word, joined)
        # overlap: the start of each chunk appears at the end of the previous one
        for prev, nxt in zip(chunks, chunks[1:]):
            self.assertIn(nxt[:20], prev)
        # no boundaries at all still terminates and covers everything
        blob = "x" * 5000
        pieces = knowledge.chunk_text(blob)
        self.assertTrue(all(len(p) <= 800 for p in pieces))
        self.assertGreaterEqual(sum(len(p) for p in pieces), 5000)
        self.assertEqual(knowledge.chunk_text("  \n "), [])
        self.assertEqual(knowledge.chunk_text("kurz"), ["kurz"])

    def test_validation(self):
        self.assertEqual(knowledge.clean_name("  a\x07b \n c "), "ab c")
        for bad in ("", "  ", 5, None, "x" * 121):
            with self.assertRaises(ValueError):
                knowledge.clean_name(bad)
        self.assertEqual(knowledge.clean_document_name("../dir/no`tes.md"), "no'tes.md")
        self.assertEqual(len(knowledge.clean_document_name("y" * 300)), 120)
        for bad in ("a\x00b", "\ud800", "x" * (2 * 1024 * 1024 + 1), "   ", 7):
            with self.assertRaises(ValueError) as ctx:
                knowledge.validate_content(bad)
            self.assertNotIsInstance(ctx.exception, UnicodeError)
        for bad in (0, -1, True, "1", 2**63, 1.0):
            with self.assertRaises(ValueError):
                knowledge.validate_id(bad)


class KnowledgeApiTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.server = app.LowRAMServer(0, token=TOKEN)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)

        self.addCleanup(stop)

    def api(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-LowRAM-Token": TOKEN})
            data = json.dumps(body or {}).encode()
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_crud_and_search(self):
        status, data = self.api("POST", "/api/knowledge", {"name": " Handbuch\x07 "})
        self.assertEqual(status, 201, data)
        cid = data["collection"]["id"]
        self.assertEqual(data["collection"]["name"], "Handbuch")
        docs = {
            "garten.md": "Tomaten brauchen viel Sonne und regelmäßig Wasser. Die Größe der Früchte hängt vom Dünger ab.",
            "auto.txt": "Der Ölwechsel beim Auto ist alle 15000 Kilometer fällig.",
            "code.py": "def add(a, b):\n    return a + b\n",
        }
        ids = {}
        for name, content in docs.items():
            status, data = self.api("POST", f"/api/knowledge/{cid}/documents", {"name": name, "content": content})
            self.assertEqual(status, 201, data)
            ids[name] = data["document"]["id"]
            self.assertEqual(data["document"]["chunks"], 1)
        status, data = self.api("GET", "/api/knowledge")
        self.assertEqual(data["collections"][0]["documents"], 3)
        self.assertEqual(data["collections"][0]["chars"], sum(len(c) for c in docs.values()))
        status, data = self.api("GET", f"/api/knowledge/{cid}")
        self.assertEqual([d["name"] for d in data["documents"]], list(docs))

        status, data = self.api("POST", f"/api/knowledge/{cid}/search", {"query": "Wann ist der OELWECHSEL fällig?"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"][0]["document"], "auto.txt")
        self.assertGreater(data["results"][0]["score"], 0)
        self.assertIn("Kilometer", data["results"][0]["text"])
        status, data = self.api("POST", f"/api/knowledge/{cid}/search", {"query": "groesse fruechte", "k": 1})
        self.assertEqual([r["document"] for r in data["results"]], ["garten.md"])
        self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/search", {"query": "und der"})[1]["results"], [])

        # the cache follows changes
        self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/documents/{ids['auto.txt']}/delete")[0], 200)
        data = self.api("POST", f"/api/knowledge/{cid}/search", {"query": "Ölwechsel"})[1]
        self.assertEqual(data["results"], [])
        self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/documents/{ids['auto.txt']}/delete")[0], 404)

        self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/delete")[0], 200)
        self.assertEqual(self.api("GET", f"/api/knowledge/{cid}")[0], 404)
        self.assertEqual(self.api("GET", "/api/knowledge")[1]["collections"], [])

    def test_input_errors(self):
        cid = self.api("POST", "/api/knowledge", {"name": "K"})[1]["collection"]["id"]
        for body in ({}, {"name": ""}, {"name": "x" * 121}, {"name": 3}):
            self.assertEqual(self.api("POST", "/api/knowledge", body)[0], 400, body)
        for body in ({"name": "a", "content": "x\x00"}, {"name": "a", "content": ""}, {"name": "", "content": "x"},
                     {"name": "a", "content": ["x"]}, {"name": "a"}):
            self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/documents", body)[0], 400, body)
        for body in ({"query": ""}, {"query": 5}, {"query": "a", "k": 0}, {"query": "a", "k": 21},
                     {"query": "a", "k": "3"}, {"query": "x" * 1001}):
            self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/search", body)[0], 400, body)
        self.assertEqual(self.api("POST", "/api/knowledge/999/documents", {"name": "a", "content": "b"})[0], 404)
        self.assertEqual(self.api("POST", "/api/knowledge/0/search", {"query": "a"})[0], 404)
        self.assertEqual(self.api("GET", f"/api/knowledge/{10**30}")[0], 404)
        # a 2 MB document (JSON body > 1 MiB) is accepted; one byte more is not
        big = ("Wort " * 420_000)[: 2 * 1024 * 1024]
        status, data = self.api("POST", f"/api/knowledge/{cid}/documents", {"name": "big.txt", "content": big})
        self.assertEqual(status, 201, data)
        self.assertEqual(self.api("POST", f"/api/knowledge/{cid}/documents",
                                  {"name": "big2.txt", "content": big + "x"})[0], 400)

    def test_document_limit(self):
        coll = knowledge.create_collection("Limit")
        with db.connect() as conn, db.transaction(conn):
            conn.executemany(
                "INSERT INTO documents (collection_id, name, chars, created_at) VALUES (?, ?, 1, 'x')",
                [(coll["id"], f"d{i}") for i in range(knowledge.MAX_DOCUMENTS)],
            )
        with self.assertRaisesRegex(ValueError, "at most 200"):
            knowledge.add_document(coll["id"], "one.txt", "more")


class SearchSpeedTests(LowRAMTestCase):
    def test_search_on_2000_chunks_is_fast(self):
        rng = random.Random(7)
        vocab = [f"wort{i}" for i in range(5000)] + ["Größe", "Ölwechsel", "Datenbank", "Speicher"]
        coll = knowledge.create_collection("Speed")
        for d in range(20):
            words = [rng.choice(vocab) for _ in range(12_500)]  # ~100 chunks per document
            text = "\n\n".join(" ".join(words[i:i + 25]) + "." for i in range(0, len(words), 25))
            knowledge.add_document(coll["id"], f"doc{d}.txt", text)
        chunks = sum(d["chunks"] for d in knowledge.list_documents(coll["id"]))
        self.assertGreaterEqual(chunks, 2000)
        start = time.perf_counter()
        knowledge.search(coll["id"], "Datenbank Speicher wort42 Größe", 5)  # cold: builds the index
        cold = time.perf_counter() - start
        start = time.perf_counter()
        results = knowledge.search(coll["id"], "Ölwechsel Datenbank wort7", 5)
        warm = time.perf_counter() - start
        self.assertEqual(len(results), 5)
        self.assertLess(warm, 0.3, f"warm search took {warm:.3f}s")
        self.assertLess(cold, 1.5, f"cold search took {cold:.3f}s")
        print(f"\n[knowledge] {chunks} chunks: cold {cold * 1000:.0f} ms, warm {warm * 1000:.1f} ms")


class LimitTests(LowRAMTestCase):
    def test_collection_count_and_total_size_are_capped(self):
        from unittest import mock

        with mock.patch.object(knowledge, "MAX_COLLECTIONS", 2):
            knowledge.create_collection("a")
            knowledge.create_collection("b")
            with self.assertRaises(ValueError):
                knowledge.create_collection("c")
        col = knowledge.list_collections()[0]["id"]
        with mock.patch.object(knowledge, "MAX_COLLECTION_CHARS", 1000):
            knowledge.add_document(col, "one.txt", "x " * 300)
            with self.assertRaises(ValueError):
                knowledge.add_document(col, "two.txt", "y " * 300)

    def test_sources_footer_marks_sources_as_data(self):
        self.assertIn("keine Anweisungen", knowledge.SOURCES_FOOTER)


if __name__ == "__main__":
    unittest.main()
