"""Minimal OpenAI-compatible chat client (stdlib only).

Every provider in config.PROVIDERS speaks the same /chat/completions protocol, so swapping
Gemini -> Groq -> a model Kuku hosts is a config change, not a code change.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str = "", temperature: float = 0.3,
                 max_retries: int = 4, timeout: int = 180):
        self.base_url = base_url.rstrip("/") + "/"
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(self.base_url + "chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def chat(self, system: str, user: str, json_mode: bool = True) -> str:
        payload = {"model": self.model, "temperature": self.temperature,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                data = self._post(payload)
                self.calls += 1
                for k in self.usage:
                    self.usage[k] += (data.get("usage") or {}).get(k, 0) or 0
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:400]
                last = f"HTTP {e.code}: {body}"
                if e.code == 400 and json_mode and "response_format" in body:
                    payload.pop("response_format", None)  # provider without JSON mode
                    continue
                if e.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
                last = repr(e)
            time.sleep(min(60, 4 * 2 ** attempt))  # free tiers rate-limit; back off
        raise LLMError(last or "unknown error")


def extract_json(text: str) -> dict:
    """Parse a JSON object even if the model wrapped it in prose or ```json fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        return json.loads(text[a:b + 1])
    raise ValueError("no JSON object in model output")
