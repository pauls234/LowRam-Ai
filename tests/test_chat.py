import gc
import http.client
import json
import os
import threading
import time
import types
import unittest
from unittest import mock

from backend import app, chat, db, llm
from tests.chat_helpers import (
    Disconnect, FakeStreamLLM, Hold, HttpError, JsonAnswer, RawBody, Redirect, SSEClient, Stream,
)
from tests.helpers import LowRAMTestCase, closed_port_url, wait_until

TOKEN = "t"
MSGS = [{"role": "user", "content": "hi"}]


class StreamLLMTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeStreamLLM()
        self.addCleanup(self.fake.close)
        env = mock.patch.dict(os.environ, {"LOWRAM_LLM_URL": self.fake.url})
        env.start()
        self.addCleanup(env.stop)


# --------------------------------------------------------------------------- llm.stream_chat


class StreamChatTests(StreamLLMTestCase):
    def collect(self, **kw):
        return list(llm.stream_chat(MSGS, max_tokens=kw.pop("max_tokens", 64), **kw))

    def test_tokens_in_order_and_request_shape(self):
        self.fake.push(Stream(["Hel", "lo", " wörld"], delay=0.01))
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_MODEL": "m1"}):
            self.assertEqual(self.collect(max_tokens=99, temperature=0.3), ["Hel", "lo", " wörld"])
        req = self.fake.requests[0]
        self.assertIs(req["json"]["stream"], True)
        self.assertEqual((req["json"]["max_tokens"], req["json"]["temperature"]), (99, 0.3))
        self.assertEqual(req["json"]["model"], "m1")
        self.assertEqual(req["json"]["messages"], MSGS)
        self.assertIn("text/event-stream", req["headers"]["Accept"])
        self.assertNotIn("Authorization", req["headers"])

    def test_close_delimited_crlf_and_comments(self):
        self.fake.push(Stream(["a", "b"], chunked=False, crlf=True, comments=True))
        self.assertEqual(self.collect(), ["a", "b"])

    def test_think_tags_pass_through_and_reasoning_content_is_wrapped(self):
        self.fake.push(Stream(["<thi", "nk>hmm</th", "ink>answer"]))
        self.assertEqual("".join(self.collect()), "<think>hmm</think>answer")
        self.fake.push(Stream(["Answer"], reasoning=["let me ", "think"]))
        self.assertEqual(self.collect(), ["<think>", "let me ", "think", "</think>", "Answer"])

    def test_http_error_scrubs_api_key(self):
        self.fake.push(HttpError(500, "bad key sekret123"))
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "sekret123"}):
            with self.assertRaisesRegex(llm.LLMError, "HTTP 500") as ctx:
                self.collect()
        self.assertNotIn("sekret123", str(ctx.exception))
        self.assertEqual(self.fake.requests[0]["headers"]["Authorization"], "Bearer sekret123")

    def test_empty_streams(self):
        self.fake.push(Stream([]), Stream(["  ", "\n"]), Stream([], reasoning=["only thinking"]),
                       Stream(["<think>unclosed"]))
        for _ in range(4):
            with self.assertRaisesRegex(llm.LLMError, "empty"):
                self.collect()

    def test_malformed_data(self):
        bad = [b"data: {not json\n\n", b'data: {"choices": "x"}\n\n', b"data: [1]\n\n",
               b'data: {"choices": [{"delta": {"content": 5}}]}\n\n', b"data: \xff\xfe\n\n"]
        for data in bad:
            self.fake.push(RawBody(data))
            with self.subTest(data=data), self.assertRaisesRegex(llm.LLMError, "not OpenAI"):
                self.collect()

    def test_error_events(self):
        self.fake.push(RawBody(b'error: {"code": 500, "message": "context too long", "type": "server_error"}\n\n'))
        with self.assertRaisesRegex(llm.LLMError, "context too long"):
            self.collect()
        self.fake.push(RawBody(b'data: {"error": {"message": "model crashed"}}\n\n'))
        with self.assertRaisesRegex(llm.LLMError, "model crashed"):
            self.collect()

    def test_mid_stream_disconnect(self):
        for chunked in (True, False):
            self.fake.push(Disconnect(["partial ", "text"], chunked=chunked))
            got = []
            with self.subTest(chunked=chunked), self.assertRaisesRegex(llm.LLMError, "interrupted|before the answer"):
                for piece in llm.stream_chat(MSGS, max_tokens=8):
                    got.append(piece)
            self.assertEqual(got, ["partial ", "text"])

    def test_stop_event_closes_promptly(self):
        release, stop = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["one", "two"]))
        got = []
        started = time.monotonic()
        for piece in llm.stream_chat(MSGS, max_tokens=8, stop_event=stop):
            got.append(piece)
            if len(got) == 2:
                stop.set()
        self.assertEqual(got, ["one", "two"])
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(wait_until(lambda: self.fake.disconnects == 1, 5))

    def test_stop_before_start_sends_nothing(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.collect(stop_event=stop), [])
        self.assertEqual(self.fake.requests, [])

    def test_endpoint_rules(self):
        for bad in ("file:///etc/passwd", "ftp://x/y", "garbage"):
            with self.subTest(url=bad), self.assertRaisesRegex(llm.LLMError, "http"):
                self.collect(url=bad)
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "k"}):
            with self.assertRaisesRegex(llm.LLMError, "plain http"):
                self.collect(url="http://example.com/v1/chat/completions")
        self.assertEqual(self.fake.requests, [])
        self.fake.push(Redirect("http://example.com/"))
        with self.assertRaisesRegex(llm.LLMError, "redirect"):
            self.collect()
        with self.assertRaisesRegex(llm.LLMError, "Could not reach"):
            self.collect(url=closed_port_url(), timeout=3)

    def test_plain_json_fallback_and_limits(self):
        self.fake.push(JsonAnswer("whole answer"))
        self.assertEqual(self.collect(), ["whole answer"])
        with mock.patch.object(llm, "MAX_STREAM_LINE_BYTES", 64):
            self.fake.push(Stream(["x" * 200]))
            with self.assertRaisesRegex(llm.LLMError, "oversized"):
                self.collect()


# --------------------------------------------------------------------------- pure helpers


class ThinkSplitterTests(unittest.TestCase):
    def run_split(self, chunks):
        splitter = chat.ThinkSplitter()
        out = []
        for chunk in chunks:
            out.extend(splitter.feed(chunk))
        out.extend(splitter.flush())
        merged = []
        for kind, text in out:
            if merged and merged[-1][0] == kind:
                merged[-1] = (kind, merged[-1][1] + text)
            else:
                merged.append((kind, text))
        return merged

    def test_tags_split_at_every_position(self):
        text = "<think>plan it</think>The answer <b>bold</b>"
        expected = [("reasoning", "plan it"), ("token", "The answer <b>bold</b>")]
        for cut in range(1, len(text)):
            for cut2 in range(cut, len(text)):
                with self.subTest(cut=cut, cut2=cut2):
                    self.assertEqual(self.run_split([text[:cut], text[cut:cut2], text[cut2:]]), expected)

    def test_case_multiple_blocks_stray_and_partial_end(self):
        self.assertEqual(
            self.run_split(["a<THINK>x</Think>b<think>y</think>c"]),
            [("token", "a"), ("reasoning", "x"), ("token", "b"), ("reasoning", "y"), ("token", "c")],
        )
        self.assertEqual(self.run_split(["before</think>after"]), [("token", "beforeafter")])
        self.assertEqual(self.run_split(["ends with <thi"]), [("token", "ends with <thi")])
        self.assertEqual(self.run_split(["a < b and <tag>"]), [("token", "a < b and <tag>")])


def turn(q, a, status="complete"):
    return [
        {"role": "user", "content": q, "status": "complete"},
        {"role": "assistant", "content": a, "status": status},
    ]


class BuildMessagesTests(unittest.TestCase):
    def test_system_and_new_message_always_present(self):
        msgs = chat.build_messages("chat", [], "hello", 4000)
        self.assertEqual(msgs, [{"role": "system", "content": chat.CHAT_SYSTEM_PROMPT}, {"role": "user", "content": "hello"}])
        self.assertEqual(chat.build_messages("code", [], "x", 4000)[0]["content"], chat.CODE_SYSTEM_PROMPT)
        self.assertIn("code fence", chat.CODE_SYSTEM_PROMPT)
        self.assertIn("language of the user", chat.CHAT_SYSTEM_PROMPT)

    def test_drops_oldest_first_without_splitting(self):
        history = turn("q1", "a1" * 300) + turn("q2", "a2" * 300) + turn("q3", "a3")
        budget = len(chat.CHAT_SYSTEM_PROMPT) + len(chat.OMITTED_NOTE) + 700 + 10 * chat.MESSAGE_OVERHEAD_CHARS
        msgs = chat.build_messages("chat", history, "new", budget)
        contents = [m["content"] for m in msgs[1:]]
        self.assertEqual(contents, ["q2", "a2" * 300, "q3", "a3", "new"])
        self.assertEqual([m["role"] for m in msgs[1:]], ["user", "assistant", "user", "assistant", "user"])

    def test_keeps_answer_with_note_when_question_does_not_fit(self):
        history = turn("q" * 500, "answer " * 50)
        budget = len(chat.CHAT_SYSTEM_PROMPT) + len(chat.OMITTED_NOTE) + 400 + 6 * chat.MESSAGE_OVERHEAD_CHARS
        msgs = chat.build_messages("chat", history, "new", budget)
        self.assertEqual([m["content"] for m in msgs[1:]], [chat.OMITTED_NOTE, "answer " * 50, "new"])

    def test_truncates_single_huge_messages_with_marker(self):
        huge = "A" * 5000 + "MIDDLE" + "Z" * 5000
        msgs = chat.build_messages("chat", turn("q", huge), "new", 4000)
        last_answer = msgs[-2]["content"]
        self.assertEqual(msgs[-2]["role"], "assistant")
        self.assertIn("characters left out", last_answer)
        self.assertTrue(last_answer.startswith("AAA") and last_answer.endswith("ZZZ"))
        self.assertNotIn("MIDDLE", last_answer)
        self.assertLess(sum(len(m["content"]) for m in msgs), 4000)
        msgs = chat.build_messages("chat", [], huge, 4000)
        self.assertIn("characters left out", msgs[-1]["content"])
        self.assertLess(len(msgs[-1]["content"]), 4000)

    def test_skips_failed_and_empty_answers(self):
        history = turn("q1", "", "error") + turn("q2", "partial", "stopped") + turn("q3", "", "stopped") + [
            {"role": "user", "content": "orphan", "status": "complete"}
        ]
        msgs = chat.build_messages("chat", history, "new", 10000)
        self.assertEqual([m["content"] for m in msgs[1:]], ["q2", "partial", "new"])


# --------------------------------------------------------------------------- HTTP API


class ChatApiTestCase(StreamLLMTestCase):
    def setUp(self):
        super().setUp()
        self.server = app.LowRAMServer(0, token=TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.assertTrue(wait_until(lambda: not chat._ACTIVE, 10), "an answer is still active")
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def api(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-LowRAM-Token": token})
            data = json.dumps(body or {}).encode()
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def new_chat(self, **body):
        status, data = self.api("POST", "/api/chats", body)
        self.assertEqual(status, 201, data)
        return data["chat"]["id"]

    def send(self, chat_id, content="Hello?", **body):
        return SSEClient(self.port, f"/api/chats/{chat_id}/messages", {"content": content, **body})

    def messages(self, chat_id):
        return self.api("GET", f"/api/chats/{chat_id}")[1]["messages"]


class ConversationApiTests(ChatApiTestCase):
    def test_crud_and_validation(self):
        first = self.new_chat()
        second = self.new_chat(title="  My\n  code  chat ", mode="code")
        status, data = self.api("GET", "/api/chats")
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in data["chats"]], [second, first])
        chat_two = data["chats"][0]
        self.assertEqual((chat_two["title"], chat_two["mode"], chat_two["message_count"]), ("My code chat", "code", 0))
        self.assertEqual(data["chats"][1]["mode"], "chat")
        for body in ({"mode": "image"}, {"title": 5}, {"title": "x" * 121}):
            with self.subTest(body=body):
                self.assertEqual(self.api("POST", "/api/chats", body)[0], 400)
        status, data = self.api("POST", f"/api/chats/{first}/rename", {"title": " Renamed "})
        self.assertEqual((status, data["chat"]["title"]), (200, "Renamed"))
        self.assertEqual(self.api("POST", f"/api/chats/{first}/rename", {"title": "  "})[0], 400)
        self.assertEqual(self.api("POST", "/api/chats/999/rename", {"title": "x"})[0], 404)
        self.assertEqual(self.api("GET", "/api/chats/999")[0], 404)
        self.assertEqual(self.api("POST", "/api/chats/999/delete")[0], 404)
        self.assertEqual(self.api("POST", "/api/chats/999/stop")[0], 404)
        self.assertEqual(self.api("POST", "/api/chats", {}, token="wrong")[0], 403)
        self.assertEqual(self.api("POST", f"/api/chats/{first}/delete"), (200, {"deleted": True}))
        self.assertEqual([c["id"] for c in self.api("GET", "/api/chats")[1]["chats"]], [second])

    def test_delete_cascades_to_messages(self):
        cid = self.new_chat()
        self.send(cid).rest()
        self.assertEqual(len(self.messages(cid)), 2)
        self.api("POST", f"/api/chats/{cid}/delete")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_startup_hook_marks_leftover_streaming_as_stopped(self):
        cid = self.new_chat()
        now = db.utcnow()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, status, created_at) "
                "VALUES (?, 'assistant', 'half', 'streaming', ?)", (cid, now),
            )
        with self.assertLogs("lowram.chat", "WARNING"):
            self.assertEqual(chat.recover_interrupted(None), 1)
        message = self.messages(cid)[0]
        self.assertEqual((message["status"], message["content"]), ("stopped", "half"))
        self.assertIn("Interrupted", message["error"])
        self.assertIn(chat.recover_interrupted, __import__("backend.web").web.STARTUP_HOOKS)


class StreamingApiTests(ChatApiTestCase):
    def test_full_answer_events_and_persistence(self):
        cid = self.new_chat()
        self.fake.push(Stream(["Hi", " there", "!"], delay=0.01))
        client = self.send(cid, "  What   is\nLowRAM? " + "x" * 80)
        self.assertEqual(client.status, 200)
        self.assertTrue(client.headers["Content-Type"].startswith("text/event-stream"))
        events = client.rest()
        self.assertEqual([e["type"] for e in events], ["start", "token", "token", "token", "done"])
        start, done = events[0], events[-1]
        self.assertEqual(start["user_message"]["role"], "user")
        self.assertEqual(start["assistant_message"]["status"], "streaming")
        self.assertEqual("".join(e["text"] for e in events[1:-1]), "Hi there!")
        self.assertEqual((done["message"]["content"], done["message"]["status"]), ("Hi there!", "complete"))
        self.assertIsNotNone(done["message"]["duration_s"])
        stored = self.messages(cid)
        self.assertEqual([(m["role"], m["status"]) for m in stored], [("user", "complete"), ("assistant", "complete")])
        self.assertEqual(stored[1]["content"], "Hi there!")
        info = self.api("GET", f"/api/chats/{cid}")[1]["chat"]
        self.assertEqual(len(info["title"]), 60)
        self.assertTrue(info["title"].startswith("What is LowRAM? xxx"))
        self.assertEqual(info["message_count"], 2)
        request = self.fake.requests[0]["json"]
        self.assertEqual(request["messages"][0]["content"], chat.CHAT_SYSTEM_PROMPT)
        self.assertGreaterEqual(request["max_tokens"], 512)
        self.assertIsNone(db.busy_reason())

    def test_history_is_sent_and_code_mode_tokens(self):
        cid = self.new_chat(mode="code")
        self.fake.push(Stream(["first answer"]), Stream(["second answer"]))
        self.send(cid, "first question").rest()
        self.send(cid, "second question").rest()
        sent = self.fake.requests[1]["json"]
        self.assertEqual(
            [(m["role"], m["content"]) for m in sent["messages"][1:]],
            [("user", "first question"), ("assistant", "first answer"), ("user", "second question")],
        )
        self.assertEqual(sent["messages"][0]["content"], chat.CODE_SYSTEM_PROMPT)
        self.assertGreaterEqual(sent["max_tokens"], 1024)

    def test_reasoning_split_across_chunks(self):
        cid = self.new_chat()
        self.fake.push(Stream(["<th", "ink>step one", " step two</thi", "nk>\n\nFinal", " answer"]))
        events = self.send(cid).rest()
        reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning")
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertEqual(reasoning, "step one step two")
        self.assertEqual(tokens, "Final answer")
        self.assertEqual(events[-1]["message"]["content"], "Final answer")
        self.assertEqual(self.messages(cid)[1]["content"], "Final answer")

    def test_invalid_input_and_busy_answer_before_streaming(self):
        cid = self.new_chat()
        for body in ({"content": ""}, {"content": "   "}, {"content": 5}, {"content": "x" * 20001},
                     {"content": "ok", "percent": 5}, {"content": "ok", "percent": "abc"}):
            with self.subTest(body=body):
                client = SSEClient(self.port, f"/api/chats/{cid}/messages", body)
                self.assertEqual(client.status, 400, client.json)
        self.assertEqual(self.send(999).status, 404)
        self.assertEqual(SSEClient(self.port, f"/api/chats/{cid}/messages", {"content": "x"}, token="bad").status, 403)
        with db.inference_slot("image generation"):
            client = self.send(cid)
        self.assertEqual(client.status, 409)
        self.assertIn("image generation", client.json["error"])
        self.assertEqual(self.messages(cid), [])
        self.assertEqual(self.fake.requests, [])
        self.assertIsNone(db.busy_reason())
        self.assertEqual(self.send(cid, "x" * 20000).status, 200)  # boundary accepted

    def test_second_message_while_streaming_is_busy(self):
        cid = self.new_chat()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["a"]))
        client = self.send(cid)
        client.until("token")
        other = self.send(self.new_chat())
        self.assertEqual(other.status, 409)
        self.assertIn(f"chat #{cid}", other.json["error"])
        release.set()
        self.assertEqual(client.rest()[-1]["type"], "done")
        self.assertIsNone(db.busy_reason())

    def test_partial_content_saved_while_streaming(self):
        cid = self.new_chat()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["partial ", "words"]))
        with mock.patch.object(chat, "SAVE_INTERVAL_S", 0.0):
            client = self.send(cid)
            client.until("token")
            self.assertTrue(wait_until(lambda: self.messages(cid)[1]["content"] == "partial words", 5))
            self.assertEqual(self.messages(cid)[1]["status"], "streaming")
            release.set()
            done = client.rest()[-1]
        self.assertEqual((done["type"], done["message"]["content"]), ("done", "partial words!"))

    def test_stop_endpoint_keeps_partial_answer(self):
        cid = self.new_chat()
        self.assertEqual(self.api("POST", f"/api/chats/{cid}/stop"), (200, {"stopped": False}))
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["Once upon", " a time"]))
        client = self.send(cid)
        text = "".join(e.get("text", "") for e in client.until("token"))
        while text != "Once upon a time":
            text += client.next()["text"]
        self.assertTrue(chat.is_streaming(cid))
        started = time.monotonic()
        self.assertEqual(self.api("POST", f"/api/chats/{cid}/stop"), (200, {"stopped": True}))
        rest = client.rest()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(rest[-1]["type"], "done")
        self.assertEqual(rest[-1]["message"]["status"], "stopped")
        stored = self.messages(cid)[1]
        self.assertEqual((stored["status"], stored["content"], stored["error"]), ("stopped", "Once upon a time", None))
        self.assertIsNone(db.busy_reason())
        self.assertTrue(wait_until(lambda: self.fake.disconnects == 1, 5))  # upstream generation cancelled

    def test_client_disconnect_stops_and_releases(self):
        cid = self.new_chat()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["half"]))
        with mock.patch.object(chat, "PING_INTERVAL_S", 0.05):
            client = self.send(cid)
            client.until("token")
            client.close()
            self.assertTrue(wait_until(lambda: self.messages(cid)[1]["status"] == "stopped", 10))
        self.assertEqual(self.messages(cid)[1]["content"], "half")
        self.assertTrue(wait_until(lambda: db.busy_reason() is None, 5))
        self.assertTrue(wait_until(lambda: self.fake.disconnects == 1, 5))

    def test_ping_while_model_is_silent(self):
        cid = self.new_chat()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=[]))
        with mock.patch.object(chat, "PING_INTERVAL_S", 0.05):
            client = self.send(cid)
            client.until("start")
            self.assertEqual(client.next()["type"], "ping")
            release.set()
            self.assertEqual(client.rest()[-1]["type"], "done")

    def test_llm_failures_end_as_error_messages(self):
        cases = [
            (HttpError(500, "model exploded"), "HTTP 500", ""),
            (Disconnect(["cut "]), "interrupted|before the answer was complete", "cut"),
            (Stream([]), "empty", ""),
            (Stream([], reasoning=["only thoughts"]), "empty", ""),
        ]
        for item, expected, partial in cases:
            with self.subTest(expected=expected):
                cid = self.new_chat()
                self.fake.push(item)
                events = self.send(cid).rest()
                last = events[-1]
                self.assertEqual(last["type"], "error")
                self.assertRegex(last["error"], expected)
                self.assertEqual((last["message"]["status"], last["message"]["content"]), ("error", partial))
                stored = self.messages(cid)[1]
                self.assertEqual(stored["status"], "error")
                self.assertRegex(stored["error"], expected)
                self.assertIsNone(db.busy_reason())

    def test_delete_while_streaming(self):
        cid = self.new_chat()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["x"]))
        client = self.send(cid)
        client.until("token")
        self.assertEqual(self.api("POST", f"/api/chats/{cid}/delete"), (200, {"deleted": True}))
        self.assertEqual(client.rest()[-1]["message"]["status"], "stopped")
        self.assertIsNone(db.busy_reason())
        self.assertEqual(self.api("GET", f"/api/chats/{cid}")[0], 404)


class SlotReleaseTests(StreamLLMTestCase):
    """The stream object itself, without the HTTP server."""

    def new_chat(self):
        return chat.create_conversation()["id"]

    def test_close_without_iterating_releases_slot(self):
        cid = self.new_chat()
        stream = chat.start_answer(cid, "hello")
        self.assertIsNotNone(db.busy_reason())
        self.assertEqual(chat.list_messages(cid)[1]["status"], "streaming")
        stream.close()
        self.assertIsNone(db.busy_reason())
        self.assertEqual(chat.list_messages(cid)[1]["status"], "stopped")
        self.assertFalse(chat.is_streaming(cid))
        self.assertEqual(self.fake.requests, [])

    def test_dropping_the_stream_releases_slot(self):
        cid = self.new_chat()
        stream = chat.start_answer(cid, "hello")
        del stream
        gc.collect()
        self.assertIsNone(db.busy_reason())
        self.assertEqual(chat.list_messages(cid)[1]["status"], "stopped")

    def test_full_iteration_releases_before_close(self):
        cid = self.new_chat()
        self.fake.push(Stream(["ok"]))
        stream = chat.start_answer(cid, "hello", 40)
        events = list(stream)
        self.assertEqual(events[-1]["type"], "done")
        self.assertIsNone(db.busy_reason())
        stream.close()  # idempotent
        self.assertEqual(chat.list_messages(cid)[1]["status"], "complete")

    def test_close_mid_stream(self):
        cid = self.new_chat()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.push(Hold(release, first=["some"]))
        stream = chat.start_answer(cid, "hello")
        self.assertEqual(next(stream)["type"], "start")
        self.assertEqual(next(stream)["text"], "some")
        stream.close()
        self.assertIsNone(db.busy_reason())
        stored = chat.list_messages(cid)[1]
        self.assertEqual((stored["status"], stored["content"]), ("stopped", "some"))
        self.assertTrue(wait_until(lambda: self.fake.disconnects == 1, 5))


class EngineTests(StreamLLMTestCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("LOWRAM_LLM_URL")  # restored by the patch.dict of the base class
        self.cid = chat.create_conversation()["id"]

    def fake_engine(self, ensure):
        class EngineError(RuntimeError):
            pass

        calls = []

        def ensure_running(timeout):
            calls.append(timeout)
            return ensure(EngineError)

        module = types.SimpleNamespace(
            EngineError=EngineError, ensure_running=ensure_running,
            status=lambda: {"server": {"model_id": "qwen-coder-3b"}},
        )
        patcher = mock.patch.object(chat, "_engine_module", return_value=module)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_no_model_is_409_before_streaming(self):
        def fail(error):
            raise error("no model installed")

        self.fake_engine(fail)
        with self.assertRaises(web_http_error()) as ctx:
            chat.start_answer(self.cid, "hello")
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(ctx.exception.message.startswith(chat.NO_MODEL_MESSAGE))
        self.assertIn("no model installed", ctx.exception.message)
        self.assertIsNone(db.busy_reason())
        self.assertEqual(chat.list_messages(self.cid), [])

    def test_engine_url_and_model_used(self):
        calls = self.fake_engine(lambda error: self.fake.url)
        self.fake.push(Stream(["from engine"]))
        events = list(chat.start_answer(self.cid, "hello"))
        self.assertEqual(events[-1]["message"]["content"], "from engine")
        self.assertEqual(events[-1]["message"]["model"], "qwen-coder-3b")
        self.assertEqual(len(calls), 1)

    def test_external_url_skips_engine(self):
        calls = self.fake_engine(lambda error: "http://127.0.0.1:1/never")
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_URL": self.fake.url}):
            self.fake.push(Stream(["external"]))
            events = list(chat.start_answer(self.cid, "hello"))
        self.assertEqual(events[-1]["message"]["content"], "external")
        self.assertEqual(calls, [])

    def test_without_engine_module_uses_config_url(self):
        with mock.patch.object(chat, "_engine_module", return_value=None):
            url, model = chat.ensure_model()
        self.assertEqual(url, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertIsNone(model)


class ContextLimitTests(unittest.TestCase):
    def test_engine_context_caps_budget_and_answer(self):
        self.assertEqual(chat.context_limits(6400, 870, "code", 4096), (7040, 1024))
        budget, tokens = chat.context_limits(6400, 870, "chat", 4096)
        self.assertEqual(tokens, 870)
        self.assertEqual(budget, int((4096 - 870 - chat.CTX_RESERVE_TOKENS) * chat.CHARS_PER_TOKEN))
        self.assertLessEqual(budget / chat.CHARS_PER_TOKEN + tokens + chat.CTX_RESERVE_TOKENS, 4096)
        self.assertEqual(chat.context_limits(6400, 2048, "chat", 2048)[1], 1024)  # answer at most half
        self.assertEqual(chat.context_limits(2000, 300, "chat", 131072), (4000, 512))  # RAM plan is smaller

    def test_external_server_keeps_ram_plan(self):
        self.assertEqual(chat.context_limits(6400, 300, "chat", None), (12800, 512))
        self.assertEqual(chat.context_limits(6400, 300, "code", None), (12800, 1024))


class ManagedEngineStreamTests(ChatApiTestCase):
    """The real engine API shape: status(), start(persist=False), ensure_running(timeout), ctx_size()."""

    def setUp(self):
        super().setUp()
        os.environ.pop("LOWRAM_LLM_URL")  # managed engine; restored by the base class

        class EngineError(RuntimeError):
            pass

        self.EngineError = EngineError
        self.loaded = threading.Event()
        self.addCleanup(self.loaded.set)
        self.state = {"state": "stopped", "model_id": None, "ctx_size": None}
        self.start_error = None
        self.load_error = None

        def start(model_id=None, *, persist=True):
            if self.start_error:
                raise self.start_error
            self.state = {"state": "starting", "model_id": "qwen2.5-3b", "ctx_size": 4096}
            return dict(self.state)

        def ensure_running(timeout):
            self.loaded.wait(10)
            if self.load_error:
                raise self.load_error
            self.state = {**self.state, "state": "ready"}
            return self.fake.url

        engine = types.SimpleNamespace(
            EngineError=EngineError, start=start, ensure_running=ensure_running, ctx_size=lambda: 4096,
            status=lambda: {"server": dict(self.state), "paused": False},
        )
        patcher = mock.patch.object(chat, "_engine_module", return_value=engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_events_while_the_model_loads(self):
        cid = self.new_chat(mode="code")
        self.fake.push(Stream(["loaded and answering"]))
        with mock.patch.object(chat, "STATUS_INTERVAL_S", 0.05):
            client = self.send(cid, "q" * 15000)
            self.assertEqual(client.status, 200)
            self.assertEqual([e["type"] for e in client.until("status")], ["start", "status"])
            self.assertEqual(client.next(), {"type": "status", "text": chat.LOADING_TEXT})  # repeated
            self.assertEqual(self.fake.requests, [])
            self.loaded.set()
            events = client.rest()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["message"]["model"], "qwen2.5-3b")
        sent = self.fake.requests[0]["json"]
        prompt_chars = sum(len(m["content"]) for m in sent["messages"])
        self.assertEqual(sent["max_tokens"], 1024)
        self.assertLessEqual(prompt_chars / chat.CHARS_PER_TOKEN + sent["max_tokens"] + 256, 4096)
        self.assertIn("characters left out", sent["messages"][-1]["content"])

    def test_ready_engine_streams_without_status(self):
        self.state = {"state": "ready", "model_id": "qwen2.5-3b", "ctx_size": 4096}
        self.loaded.set()
        cid = self.new_chat()
        types_seen = [e["type"] for e in self.send(cid).rest()]
        self.assertEqual(types_seen, ["start", "token", "done"])

    def test_engine_reason_is_409_before_streaming(self):
        for reason in ("No model is installed yet.", "The models drive /Volumes/SSD is not connected",
                       "The engine is paused while an image or video job uses the memory"):
            with self.subTest(reason=reason):
                self.start_error = self.EngineError(reason)
                cid = self.new_chat()
                client = self.send(cid)
                self.assertEqual(client.status, 409)
                self.assertTrue(client.json["error"].startswith(chat.NO_MODEL_MESSAGE))
                self.assertIn(reason, client.json["error"])
                self.assertEqual(self.messages(cid), [])
                self.assertIsNone(db.busy_reason())

    def test_load_failure_after_start_is_an_error_event(self):
        self.load_error = self.EngineError("llama-server exited: out of memory")
        self.loaded.set()
        cid = self.new_chat()
        events = self.send(cid).rest()
        self.assertEqual([e["type"] for e in events], ["start", "status", "error"])
        self.assertIn("out of memory", events[-1]["error"])
        self.assertEqual(self.messages(cid)[1]["status"], "error")
        self.assertIsNone(db.busy_reason())

    def test_stop_while_loading(self):
        cid = self.new_chat()
        with mock.patch.object(chat, "STOP_GRACE_S", 0.2):
            client = self.send(cid)
            client.until("status")
            self.assertEqual(self.api("POST", f"/api/chats/{cid}/stop"), (200, {"stopped": True}))
            done = client.rest()[-1]
        self.assertEqual((done["type"], done["message"]["status"]), ("done", "stopped"))
        self.assertIsNone(db.busy_reason())
        self.loaded.set()


def web_http_error():
    from backend.web import HTTPError

    return HTTPError


# --------------------------------------------------------------------------- v0.3.2 smart features


class StreamStatsTests(StreamLLMTestCase):
    def test_usage_finish_reason_and_rate(self):
        stats = {}
        self.fake.push(Stream(["a", "b", "c", "d", "e"], delay=0.03, usage=42, finish_reason="length"))
        self.assertEqual("".join(llm.stream_chat(MSGS, max_tokens=8, stats=stats)), "abcde")
        self.assertEqual((stats["tokens"], stats["finish_reason"]), (42, "length"))
        self.assertGreater(stats["tokens_per_s"], 0)
        self.assertEqual(self.fake.requests[0]["json"]["stream_options"], {"include_usage": True})

    def test_without_usage_counts_deltas(self):
        stats = {}
        self.fake.push(Stream(["x", "y", "z"], reasoning=["r"]))
        list(llm.stream_chat(MSGS, max_tokens=8, stats=stats))
        self.assertEqual((stats["tokens"], stats["finish_reason"]), (4, "stop"))
        stats = {}
        self.fake.push(JsonAnswer("whole"))
        list(llm.stream_chat(MSGS, max_tokens=8, stats=stats))
        self.assertEqual(stats["tokens"], 1)


class RenderAttachmentTests(unittest.TestCase):
    def test_full_and_fenced(self):
        files = [{"name": "a.md", "content": "has ``` fence", "chars": 13}]
        text = chat.render_user_message("Summarize", files, 10_000)
        self.assertTrue(text.startswith("Summarize\n\nAttached file: a.md\n````\nhas ``` fence\n````"))

    def test_truncated_fairly_with_visible_note(self):
        files = [{"name": "small.txt", "content": "s" * 100, "chars": 100},
                 {"name": "big.txt", "content": "b" * 50_000, "chars": 50_000}]
        text = chat.render_user_message("Question?", files, 3000)
        self.assertLessEqual(len(text), 3100)
        self.assertTrue(text.startswith("Question?"))
        self.assertIn("s" * 100 + "\n```", text)  # the small file fits completely
        self.assertRegex(text, r"\[…truncated – \d+ characters omitted\]")
        self.assertNotIn("small.txt\n```\n" + "s" * 100 + "\n```\n[…truncated", text)

    def test_names_and_validation(self):
        clean = chat.validate_attachments([
            {"name": "../../etc/pass\x00wd", "content": "x\r\ny"}, {"name": "C:\\dir\\" + "n" * 200 + ".py", "content": ""},
            {"name": "  ", "content": "z"},
        ])
        self.assertEqual(clean[0], {"name": "passwd", "content": "x\ny", "chars": 3})
        self.assertLessEqual(len(clean[1]["name"]), 120)
        self.assertTrue(clean[1]["name"].endswith(".py"))
        self.assertEqual(clean[2]["name"], "attachment-3.txt")
        bad = [
            "x", [{"name": "a"}], [{"name": 1, "content": "x"}], [{"name": "a", "content": "x"}] * 6,
            [{"name": "a", "content": "nul\x00"}], [{"name": "a", "content": "é" * (100 * 1024 + 1)}],
            [{"name": "a", "content": "\ud800"}],
        ]
        for value in bad:
            with self.subTest(value=str(value)[:40]), self.assertRaises(ValueError):
                chat.validate_attachments(value)
        self.assertEqual(len(chat.validate_attachments([{"name": "a", "content": "x" * 200 * 1024}])), 1)


class SmartChatApiTests(ChatApiTestCase):
    def sent(self, index=-1):
        return self.fake.requests[index]["json"]["messages"]

    def test_custom_instructions(self):
        status, data = self.api("POST", "/api/chats/instructions", {"instructions": " Always answer\x07 in rhymes.\n "})
        self.assertEqual((status, data), (200, {"instructions": "Always answer in rhymes."}))
        self.assertEqual(self.api("POST", "/api/chats/instructions", {"instructions": "x" * 4001})[0], 400)
        self.assertEqual(self.api("POST", "/api/chats/instructions", {"instructions": 5})[0], 400)
        for mode in ("chat", "code"):
            self.send(self.new_chat(mode=mode)).rest()
            system = self.sent()[0]["content"]
            self.assertTrue(system.startswith(chat.SYSTEM_PROMPTS[mode]))
            self.assertTrue(system.endswith("Always answer in rhymes."))
        from backend import settings

        settings.update("chat", {"instructions": "y" * 5000})  # via the generic settings API
        with self.assertLogs("lowram.chat", "WARNING"):
            self.assertEqual(len(chat.current_instructions()), 4000)

    def test_attachments_stored_and_in_prompt(self):
        files = [{"name": "notes.txt", "content": "remember the milk"}, {"name": "dir/app.py", "content": "print(1)\n"}]
        events = self.send(self.new_chat(), "What is in these files?", attachments=files).rest()
        user = events[0]["user_message"]
        self.assertEqual(user["attachments"], [{"name": "notes.txt", "chars": 17}, {"name": "app.py", "chars": 9}])
        prompt = self.sent()[-1]["content"]
        self.assertTrue(prompt.startswith("What is in these files?"))
        self.assertIn("Attached file: notes.txt\n```\nremember the milk\n```", prompt)
        self.assertIn("Attached file: app.py\n```\nprint(1)\n\n```", prompt)
        cid = events[0]["chat"]["id"]
        self.assertEqual(self.messages(cid)[0]["attachments"], user["attachments"])
        self.assertEqual(self.messages(cid)[1]["attachments"], [])
        # a follow-up still sees the earlier file through the history
        self.send(cid, "And again?").rest()
        self.assertIn("remember the milk", self.sent()[1]["content"])

    def test_attachment_only_message_large_body_and_errors(self):
        cid = self.new_chat()
        big = [{"name": f"f{i}.txt", "content": "z" * (200 * 1024)} for i in range(5)]  # > 1 MiB body
        client = self.send(cid, "", attachments=big)
        self.assertEqual(client.status, 200, client.json)
        events = client.rest()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.api("GET", f"/api/chats/{cid}")[1]["chat"]["title"], "f0.txt")
        prompt = self.sent()[-1]["content"]
        self.assertRegex(prompt, r"\[…truncated – \d+ characters omitted\]")
        self.assertLess(len(prompt), 60_000)  # within the context budget
        for files in ([{"name": "a", "content": "x"}] * 6, [{"name": "a", "content": "\x00"}], "nope"):
            self.assertEqual(self.send(cid, "hi", attachments=files).status, 400)
        self.assertEqual(self.send(cid, "", attachments=[]).status, 400)

    def test_stats_and_truncated_flag(self):
        cid = self.new_chat()
        self.fake.push(Stream(["one ", "two"], delay=0.03, usage=7))
        done = self.send(cid).rest()[-1]["message"]
        self.assertEqual((done["tokens"], done["finish_reason"], done["truncated"]), (7, "stop", False))
        self.assertIsNotNone(done["tokens_per_s"])
        self.fake.push(Stream(["cut"], finish_reason="length"))
        done = self.send(cid).rest()[-1]["message"]
        self.assertEqual((done["finish_reason"], done["truncated"], done["tokens"]), ("length", True, 1))
        stored = self.messages(cid)[-1]
        self.assertEqual((stored["finish_reason"], stored["truncated"]), ("length", True))

    def test_continue_appends_to_cut_off_answer(self):
        cid = self.new_chat()
        self.assertEqual(SSEClient(self.port, f"/api/chats/{cid}/continue", {}).status, 409)  # empty chat
        self.fake.push(Stream(["Hello"], finish_reason="length", usage=5))
        first = self.send(cid, "Write a long text").rest()[-1]["message"]
        self.fake.push(Stream([" world", "!"], usage=3))
        client = SSEClient(self.port, f"/api/chats/{cid}/continue", {})
        self.assertEqual(client.status, 200, client.json)
        events = client.rest()
        start, done = events[0], events[-1]
        self.assertEqual((start["action"], start["assistant_message"]["id"]), ("continue", first["id"]))
        self.assertEqual(done["message"]["content"], "Hello world!")
        self.assertEqual((done["message"]["tokens"], done["message"]["truncated"]), (8, False))
        messages = self.sent()
        self.assertEqual([m["role"] for m in messages[-3:]], ["user", "assistant", "user"])
        self.assertEqual(messages[-3]["content"], "Write a long text")
        self.assertEqual(messages[-2]["content"], "Hello")
        self.assertEqual(messages[-1]["content"], chat.CONTINUE_NOTE)
        self.assertEqual(len(self.messages(cid)), 2)
        self.assertEqual(SSEClient(self.port, f"/api/chats/{cid}/continue", {}).status, 409)  # not cut off now

    def test_failed_continue_keeps_the_answer(self):
        cid = self.new_chat()
        self.fake.push(Stream(["Partial"], finish_reason="length"), HttpError(500, "boom"))
        self.send(cid).rest()
        events = SSEClient(self.port, f"/api/chats/{cid}/continue", {}).rest()
        self.assertEqual(events[-1]["type"], "error")
        stored = self.messages(cid)[1]
        self.assertEqual((stored["content"], stored["status"], stored["truncated"]), ("Partial", "complete", True))
        self.assertIsNone(db.busy_reason())

    def test_regenerate_replaces_last_answer(self):
        cid = self.new_chat()
        self.assertEqual(SSEClient(self.port, f"/api/chats/{cid}/regenerate", {}).status, 409)
        self.fake.push(Stream(["first"]), Stream(["second"]), Stream(["third"]))
        self.send(cid, "q1").rest()
        old = self.send(cid, "q2", attachments=[{"name": "a.txt", "content": "file text"}]).rest()[-1]["message"]
        events = SSEClient(self.port, f"/api/chats/{cid}/regenerate", {}).rest()
        start, done = events[0], events[-1]
        self.assertEqual((start["action"], start["replaced_message_id"]), ("regenerate", old["id"]))
        self.assertEqual(start["user_message"]["content"], "q2")
        self.assertNotEqual(done["message"]["id"], old["id"])
        self.assertEqual(done["message"]["content"], "third")
        stored = self.messages(cid)
        self.assertEqual([m["content"] for m in stored], ["q1", "first", "q2", "third"])
        messages = self.sent()
        self.assertEqual([m["content"] for m in messages[1:3]], ["q1", "first"])
        self.assertTrue(messages[-1]["content"].startswith("q2") and "file text" in messages[-1]["content"])
        with db.inference_slot("image job"):
            self.assertEqual(SSEClient(self.port, f"/api/chats/{cid}/regenerate", {}).status, 409)

    def test_search(self):
        apples = self.new_chat(title="Äpfel und Birnen")
        rates = self.new_chat(title="Rates")
        other = self.new_chat(title="Other")
        self.fake.push(Stream(["It grew by 100% in my_var"]))
        self.send(rates, "growth?").rest()

        def search(q):
            from urllib.parse import quote

            status, data = self.api("GET", f"/api/chats?q={quote(q)}")
            self.assertEqual(status, 200, data)
            return [c["id"] for c in data["chats"]]

        self.assertEqual(search("äPFEL"), [apples])
        self.assertEqual(search("100%"), [rates])
        self.assertEqual(search("my_var"), [rates])
        self.assertEqual(search("%"), [rates])  # literal, not a wildcard
        self.assertEqual(search("_"), [rates])
        self.assertEqual(search("GROWTH"), [rates])
        self.assertEqual(search("nothing like this"), [])
        self.assertEqual(set(search("")), {rates, other, apples})
        self.assertEqual(self.api("GET", "/api/chats?q=" + "x" * 201)[0], 400)

    def test_export_markdown(self):
        cid = self.new_chat(title="Über Ideen! #1")
        self.fake.push(Stream(["<think>secret reasoning</think>The **answer**"]))
        self.send(cid, "My question", attachments=[{"name": "n.txt", "content": "abc"}]).rest()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", f"/api/chats/{cid}/export")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/markdown"))
        self.assertIn('filename="Uber-Ideen-1.md"', resp.getheader("Content-Disposition"))
        self.assertTrue(body.startswith("# Über Ideen! #1\n"))
        self.assertIn("## User\n\nMy question", body)
        self.assertIn("## Assistant", body)
        self.assertIn("The **answer**", body)
        self.assertIn("n.txt (3 characters)", body)
        self.assertNotIn("secret reasoning", body)
        self.assertEqual(self.api("GET", "/api/chats/999/export")[0], 404)


class MigrationTests(LowRAMTestCase):
    def test_v03_database_gets_new_columns(self):
        import sqlite3

        from backend import config

        conn = sqlite3.connect(config.db_path())
        conn.executescript(
            "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '', "
            "mode TEXT NOT NULL DEFAULT 'chat', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL "
            "REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'complete', error TEXT, model TEXT, duration_s REAL, created_at TEXT NOT NULL);"
            "INSERT INTO conversations (title, mode, created_at, updated_at) VALUES ('old', 'chat', 'x', 'x');"
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (1, 'user', 'hi', 'x');"
        )
        conn.close()
        chat._MIGRATED.discard(str(config.db_path()))
        message = chat.list_messages(1)[0]
        self.assertEqual((message["content"], message["tokens"], message["truncated"], message["attachments"]),
                         ("hi", None, False, []))
        self.assertEqual(chat.list_conversations("HI")[0]["title"], "old")


if __name__ == "__main__":
    unittest.main()
