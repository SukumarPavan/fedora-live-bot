"""A tiny OpenAI-compatible server for tests: transcription, streaming chat, model list."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAPI:
    def __init__(self, reply="[happy] Hello there, friend! I can hear you. Nice to meet you today.",
                 transcript="hey robot how are you"):
        self.reply = reply
        self.transcript = transcript
        self.requests = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
                self._send(200, body.encode(), "application/json")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                api.requests.append((self.path, raw))
                if self.path.endswith("/audio/transcriptions"):
                    self._send(200, json.dumps({"text": api.transcript}).encode(), "application/json")
                elif self.path.endswith("/chat/completions"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for i in range(0, len(api.reply), 7):
                        chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                 "choices": [{"index": 0, "delta": {"content": api.reply[i:i + 7]},
                                              "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self._send(404, b"{}", "application/json")

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
