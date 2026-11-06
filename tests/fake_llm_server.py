"""Local stand-in for an OpenAI-compatible API, used to test the LLM path end-to-end without a key.

It answers each episode with: one plausible promo built from real timestamps in the prompt,
one deliberately broken promo (too short) to exercise the repair loop, and for the repair call
returns only the good one. Run:  python tests/fake_llm_server.py 8765
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TS = re.compile(r"^\[(\d\d:\d\d\.\d\d)-(\d\d:\d\d\.\d\d)\] ([^\n]+)$", re.M)


def sec(t):
    m, s = t.split(":")
    return int(m) * 60 + float(s)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("model") == "retired":  # mimic a provider retiring a model
            err = json.dumps([{"error": {"code": 404, "status": "NOT_FOUND",
                                         "message": "This model models/retired is no longer available."}}]).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return
        user = body["messages"][1]["content"]
        if "CROSS-EPISODE" in body["messages"][0]["content"]:
            rows = re.findall(r"^M\d+ \| ep (\d+) \| (\d\d:\d\d\.\d\d)-(\d\d:\d\d\.\d\d)", user, re.M)
            a, b = rows[0], next(r for r in rows[1:] if r[0] != rows[0][0])
            out = {"promos": [{"segments": [
                {"episode": int(a[0]), "start": a[1], "end": f"{int(sec(a[1]) + 12) // 60:02d}:{(sec(a[1]) + 12) % 60:05.2f}"},
                {"episode": int(b[0]), "start": b[1], "end": b[2]}],
                "hook": "fake", "cliffhanger": "fake", "why": "fake montage",
                "scores": {"hook": 4, "cliffhanger": 4, "standalone": 3}}]}
        elif user.startswith("Your previous answer"):
            prev = json.loads(user.split("Previous answer:\n", 1)[1].split("\n\nReturn the corrected", 1)[0])
            out = {"promos": prev["promos"][:1]}
        else:
            lines = TS.findall(user)
            hooks = [l for l in lines if "!" in l[2] or "?" in l[2]] or lines
            h = hooks[0]
            end = next((l for l in lines if 25 <= sec(l[1]) - sec(h[0]) <= 50), lines[-1])
            out = {"promos": [
                {"segments": [{"start": h[0], "end": end[1]}], "hook": h[2][:80], "cliffhanger": end[2][:80],
                 "why": "fake", "needs_context": "", "scores": {"hook": 4, "cliffhanger": 3, "standalone": 4}},
                {"segments": [{"start": "00:00.00", "end": "00:06.00"}], "hook": "x", "cliffhanger": "y",
                 "why": "deliberately too short", "scores": {"hook": 2, "cliffhanger": 2, "standalone": 2}}]}
        resp = {"choices": [{"message": {"content": json.dumps(out)}}],
                "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 200}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), H).serve_forever()
