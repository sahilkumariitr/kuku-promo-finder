"""Local stand-in for an OpenAI-compatible API, used to test the LLM path end-to-end without a key.

Behaviour by model name:
  retired -> HTTP 404 "no longer available"      (provider retired the model)
  busy    -> HTTP 503 "high demand"              (overloaded; client should fall back)
  noquota -> HTTP 429 with a per-day quota id    (daily free-tier quota exhausted)
  dropped -> closes the connection without replying (what an overloaded server sometimes does)
  nullchoices -> HTTP 200 with "choices": null  (malformed reply)
  other   -> per episode in the prompt: one plausible promo from real timestamps plus one deliberately broken
             promo (too short); the batch's LAST episode gets only the broken one, which forces a repair.
             The repair call regenerates good promos from the transcript it is re-sent.
Run:  python tests/fake_llm_server.py 8765
"""
import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TS = re.compile(r"^\[(\d\d:\d\d\.\d\d)-(\d\d:\d\d\.\d\d)\] ([^\n]+)$", re.M)
EP = re.compile(r"^===== EPISODE (\d+) \(", re.M)
ERRORS = {
    "retired": (404, {"code": 404, "status": "NOT_FOUND", "message": "This model models/retired is no longer available."}),
    "busy": (503, {"code": 503, "status": "UNAVAILABLE", "message": "This model is currently experiencing high demand."}),
    "noquota": (429, {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded.",
                      "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}),
}


def sec(t):
    m, s = t.split(":")
    return int(m) * 60 + float(s)


def ts(x):
    return f"{int(x) // 60:02d}:{x % 60:05.2f}"


def episode_promos(user, broken_last=True):
    """Split a batched prompt into episodes and propose promos for each."""
    starts = [(m.start(), int(m.group(1))) for m in EP.finditer(user)]
    out = []
    for i, (pos, num) in enumerate(starts):
        last = i == len(starts) - 1
        chunk = user[pos: starts[i + 1][0] if i + 1 < len(starts) else len(user)]
        lines = TS.findall(chunk)
        if not lines:
            continue
        hooks = [l for l in lines if "!" in l[2] or "?" in l[2]] or lines
        h = hooks[0]
        end = next((l for l in lines if 25 <= sec(l[1]) - sec(h[0]) <= 50), lines[-1])
        if not (last and broken_last):  # the batch's last episode gets only a broken promo -> forces a repair
            out.append({"episode": num, "segments": [{"start": h[0], "end": end[1]}], "hook": h[2][:80],
                        "cliffhanger": end[2][:80], "why": "fake", "needs_context": "",
                        "scores": {"hook": 4, "cliffhanger": 3, "standalone": 4}})
        out.append({"episode": num, "segments": [{"start": "00:00.00", "end": "00:06.00"}], "hook": "x",
                    "cliffhanger": "y", "why": "deliberately too short",
                    "scores": {"hook": 2, "cliffhanger": 2, "standalone": 2}})
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("model") == "dropped":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if body.get("model") == "nullchoices":
            return self._send(200, {"choices": None})
        if body.get("model") in ERRORS:
            code, err = ERRORS[body["model"]]
            return self._send(code, [{"error": err}])
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "Your previous answer to the request above" in user:
            original = user.split("\n\n----\nYour previous answer", 1)[0]  # repair re-sends the transcript
            out = {"promos": [p for p in episode_promos(original, broken_last=False)
                              if p.get("why") != "deliberately too short"]}
        elif "CROSS-EPISODE" in system:
            rows = re.findall(r"^M\d+ \| ep (\d+) \| (\d\d:\d\d\.\d\d)-(\d\d:\d\d\.\d\d)", user, re.M)
            a, b = rows[0], next(r for r in rows[1:] if r[0] != rows[0][0])
            out = {"promos": [{"segments": [
                {"episode": int(a[0]), "start": a[1], "end": ts(sec(a[1]) + 12)},
                {"episode": int(b[0]), "start": b[1], "end": b[2]}],
                "hook": "fake", "cliffhanger": "fake", "why": "fake montage",
                "scores": {"hook": 4, "cliffhanger": 4, "standalone": 3}}]}
        else:
            out = {"promos": episode_promos(user)}
        self._send(200, {"choices": [{"message": {"content": json.dumps(out)}}],
                         "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 200}})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), H).serve_forever()
