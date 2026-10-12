import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from localagent.providers import (Message, ProviderRouter, ScriptedProvider, ToolCall, ToolSpec, create_provider,
                                  extract_json)


class FakeServer:
    """Local HTTP server that records requests and replies with a canned body."""

    def __init__(self, reply):
        self.reply, self.requests = reply, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                data = json.dumps(outer.reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


TOOLS = [ToolSpec("read_file", "read", {"type": "object", "properties": {"path": {"type": "string"}},
                                         "required": ["path"], "additionalProperties": False})]
HISTORY = [Message("system", "be brief"), Message("user", "read a.txt"),
           Message("assistant", "", tool_calls=[ToolCall("read_file", {"path": "a.txt"}, id="c1")]),
           Message("tool", "hello", tool_call_id="c1", name="read_file")]


class TestWireFormats(unittest.TestCase):
    def test_openai_compatible(self):
        srv = FakeServer({"model": "m", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "x", "function": {"name": "read_file",
                                                                       "arguments": "{\"path\": \"b\"}"}}]}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        try:
            p = create_provider("vllm:qwen", base_url=srv.url + "/v1", api_key="k")
            c = p.complete(HISTORY, TOOLS, seed=1)
            req = srv.requests[0]
            self.assertEqual(req["path"], "/v1/chat/completions")
            self.assertEqual(req["headers"]["Authorization"], "Bearer k")
            self.assertEqual(req["body"]["messages"][2]["tool_calls"][0]["function"]["name"], "read_file")
            self.assertEqual(req["body"]["messages"][3], {"role": "tool", "tool_call_id": "c1", "content": "hello"})
            self.assertEqual(c.message.tool_calls[0].arguments, {"path": "b"})
            self.assertEqual((c.usage.input_tokens, c.usage.output_tokens), (7, 3))
        finally:
            srv.close()

    def test_anthropic(self):
        srv = FakeServer({"model": "claude", "stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "done"}],
            "usage": {"input_tokens": 5, "output_tokens": 2}})
        try:
            p = create_provider("anthropic:claude-x", base_url=srv.url, api_key="k")
            c = p.complete(HISTORY, TOOLS)
            body = srv.requests[0]["body"]
            self.assertEqual(srv.requests[0]["path"], "/v1/messages")
            self.assertEqual(body["system"], "be brief")
            self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
            self.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")
            self.assertEqual(body["tools"][0]["input_schema"]["required"], ["path"])
            self.assertEqual((c.message.content, c.message.reasoning), ("done", "hmm"))
        finally:
            srv.close()

    def test_gemini(self):
        srv = FakeServer({"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"functionCall": {"name": "read_file", "args": {"path": "z"}}}]}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1}})
        try:
            p = create_provider("gemini:gemini-pro", base_url=srv.url, api_key="k")
            c = p.complete(HISTORY, TOOLS)
            body = srv.requests[0]["body"]
            self.assertIn("gemini-pro:generateContent", srv.requests[0]["path"])
            self.assertEqual([m["role"] for m in body["contents"]], ["user", "model", "user"])
            self.assertEqual(body["contents"][2]["parts"][0]["functionResponse"]["name"], "read_file")
            self.assertNotIn("additionalProperties", body["tools"][0]["functionDeclarations"][0]["parameters"])
            self.assertEqual(c.message.tool_calls[0].name, "read_file")
        finally:
            srv.close()


class TestHelpers(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": "x}\\"y"} hope that helps'), {"a": 'x}"y'})
        with self.assertRaises(ValueError):
            extract_json("no json here")

    def test_router_fallback(self):
        a, b = ScriptedProvider("a"), ScriptedProvider("b")
        r = ProviderRouter(a, {"review": b})
        self.assertIs(r.for_role("review.security"), b)
        self.assertIs(r.for_role("coder.codex"), a)
        r.set("review.security", "mock:sec")
        self.assertEqual(r.for_role("review.security").spec, "mock:sec")

    def test_complete_json_retries(self):
        p = ScriptedProvider(responses=["not json", '{"ok": true}'])
        data, _ = p.complete_json("sys", "user")
        self.assertEqual(data, {"ok": True})

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            create_provider("nope:model")


if __name__ == "__main__":
    unittest.main()
