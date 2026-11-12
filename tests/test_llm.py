import os
import unittest
from unittest import mock

from backend import llm
from tests.helpers import Empty, HttpError, LowRAMTestCase, Raw, Text, closed_port_url

MSGS = [{"role": "user", "content": "hi"}]


class ChatTests(LowRAMTestCase):
    def test_success_and_request_shape(self):
        self.llm.push(Text("  hello there \n"))
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_MODEL": "m1"}):
            out = llm.chat(MSGS, max_tokens=123, temperature=0.5)
        self.assertEqual(out, "hello there")
        req = self.llm.requests[0]
        self.assertEqual(req["json"]["max_tokens"], 123)
        self.assertEqual(req["json"]["model"], "m1")
        self.assertEqual(req["json"]["messages"], MSGS)
        self.assertIs(req["json"]["stream"], False)
        self.assertNotIn("Authorization", req["headers"])

    def test_api_key_sent_as_bearer(self):
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "sekret"}):
            llm.chat(MSGS)
        self.assertEqual(self.llm.requests[0]["headers"]["Authorization"], "Bearer sekret")

    def test_http_error(self):
        self.llm.push(HttpError(503, "overloaded"))
        with self.assertRaisesRegex(llm.LLMError, "HTTP 503.*overloaded"):
            llm.chat(MSGS)

    def test_unreachable_endpoint(self):
        with self.assertRaisesRegex(llm.LLMError, "Could not reach"):
            llm.chat(MSGS, url=closed_port_url(), timeout=3)

    def test_malformed_json(self):
        self.llm.push(Raw(b"this is {not json"))
        with self.assertRaisesRegex(llm.LLMError, "not OpenAI"):
            llm.chat(MSGS)

    def test_wrong_shape(self):
        self.llm.push(Raw(b'{"choices": []}'), Raw(b"[1, 2]"), Raw(b"\xff\xfe"))
        for _ in range(3):
            with self.assertRaises(llm.LLMError):
                llm.chat(MSGS)

    def test_empty_content(self):
        self.llm.push(Empty(), Text("   "), Raw(b'{"choices":[{"message":{"content":null}}]}'))
        for _ in range(3):
            with self.assertRaisesRegex(llm.LLMError, "empty"):
                llm.chat(MSGS)

    def test_non_http_url(self):
        for bad in ("file:///etc/passwd", "ftp://x/y", "garbage", ""):
            with self.subTest(url=bad):
                if bad == "":  # empty falls back to the configured URL
                    continue
                with self.assertRaisesRegex(llm.LLMError, "http"):
                    llm.chat(MSGS, url=bad)
        self.assertEqual(self.llm.requests, [])

    def test_loopback_detection(self):
        self.assertTrue(llm._is_loopback("127.0.0.1"))
        self.assertTrue(llm._is_loopback("localhost"))
        self.assertTrue(llm._is_loopback("::1"))
        self.assertFalse(llm._is_loopback("example.com"))
        self.assertFalse(llm._is_loopback(None))


class StripReasoningTests(unittest.TestCase):
    def test_strips_blocks(self):
        self.assertEqual(llm.strip_reasoning("<think>a\nb</think>\nanswer"), "answer")
        self.assertEqual(llm.strip_reasoning("<THINK>x</THINK>ok<think>y</think>"), "ok")

    def test_plain_text_unchanged(self):
        self.assertEqual(llm.strip_reasoning("  plain  "), "plain")
        self.assertEqual(llm.strip_reasoning(""), "")


if __name__ == "__main__":
    unittest.main()
