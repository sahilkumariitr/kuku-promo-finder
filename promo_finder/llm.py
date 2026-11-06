"""Minimal OpenAI-compatible chat client (stdlib only).

Every provider in config.PROVIDERS speaks the same /chat/completions protocol, so swapping
Gemini -> Groq -> a model Kuku hosts is a config change, not a code change.

Built for free tiers: requests are paced (RPM), per-minute 429s wait for the provider's retryDelay,
and a model that is overloaded, retired or out of daily quota hands over to the next model in the chain.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    """kind: auth | model | quota_day | overload | other"""

    def __init__(self, msg: str, status: int | None = None, kind: str = "other"):
        super().__init__(msg)
        self.status = status
        self.kind = kind

    @property
    def fatal(self) -> bool:
        """Errors that will fail identically on every later call, so the run should stop."""
        return self.kind in ("auth", "model", "quota_day")


def classify(status: int, body: str) -> str:
    low = body.lower()
    if status == 401 or status == 403 or (status == 400 and "api key" in low):
        return "auth"
    if status == 404:
        return "model"
    if status == 429:
        return "quota_day" if ("perday" in low.replace(" ", "") or "per day" in low) else "rate"
    if status in (408, 500, 502, 503, 504):
        return "overload"
    return "other"


def retry_delay(body: str, default: float = 30.0) -> float:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
    return min(float(m.group(1)) + 1, 90.0) if m else default


class LLMClient:
    def __init__(self, base_url: str, models: list[str], api_key: str = "", temperature: float = 0.3,
                 max_retries: int = 4, timeout: int = 300, min_interval: float = 0.0,
                 overload_retries: int = 2, retry_base: float = 10.0, log=print):
        self.base_url = base_url.rstrip("/") + "/"
        self.models = [m for m in dict.fromkeys(models) if m]  # ordered, de-duplicated
        self.api_key = api_key
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self.min_interval = min_interval
        self.overload_retries = overload_retries
        self.retry_base = retry_base
        self.log = log
        self.calls = 0          # successful responses
        self.attempts = 0       # HTTP requests sent (what free-tier quotas count)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.by_model: dict[str, int] = {}
        self.last_model: str | None = None
        self._last_call = 0.0

    @property
    def model(self) -> str:
        return self.models[0] if self.models else ""

    def _pace(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _post(self, payload: dict) -> dict:
        self._pace()
        self.attempts += 1
        req = urllib.request.Request(self.base_url + "chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def _chat_one(self, model: str, system: str, user: str, json_mode: bool) -> str:
        payload = {"model": model, "temperature": self.temperature,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last, status, overloads = None, None, 0
        for attempt in range(self.max_retries + 1):
            try:
                data = self._post(payload)
                content = data["choices"][0]["message"]["content"] or ""
                self.calls += 1
                self.by_model[model] = self.by_model.get(model, 0) + 1
                for k in self.usage:
                    self.usage[k] += (data.get("usage") or {}).get(k, 0) or 0
                return content
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:1500]
                last, status = f"HTTP {e.code}: {body}", e.code
                if e.code == 400 and json_mode and "response_format" in body:
                    payload.pop("response_format", None)  # provider without JSON mode
                    continue
                kind = classify(e.code, body)
                if kind in ("auth", "model", "quota_day", "other"):
                    raise LLMError(last, status, kind)
                if kind == "rate":  # per-minute limit: wait as long as the provider asks
                    d = retry_delay(body)
                    self.log(f"    rate limited on {model}; waiting {d:.0f}s")
                    time.sleep(d)
                    continue
                overloads += 1  # kind == "overload"
                if overloads > self.overload_retries:
                    raise LLMError(last, status, "overload")
                d = self.retry_base * overloads
                self.log(f"    {model} busy (HTTP {e.code}); retry in {d:.0f}s")
                time.sleep(d)
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
                last = repr(e)
                time.sleep(min(60, 4 * 2 ** attempt))
        raise LLMError(last or "unknown error", status, "overload" if status in (500, 502, 503, 504) else "other")

    def chat(self, system: str, user: str, json_mode: bool = True) -> str:
        """Try each model in order; move on when one is retired, overloaded or out of daily quota."""
        err: LLMError | None = None
        for i, model in enumerate(self.models):
            try:
                text = self._chat_one(model, system, user, json_mode)
                self.last_model = model
                return text
            except LLMError as e:
                err = e
                if e.kind in ("auth", "other"):
                    raise
                nxt = self.models[i + 1] if i + 1 < len(self.models) else None
                if nxt:
                    self.log(f"    {model}: {e.kind.replace('_', ' ')}; falling back to {nxt}")
        raise err or LLMError("no models configured", kind="model")


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
