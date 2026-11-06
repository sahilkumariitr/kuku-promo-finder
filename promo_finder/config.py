"""All tunables in one place. Override with a TOML file (--config) or env vars for the LLM."""
from __future__ import annotations

import os

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9/3.10: TOML config needs `pip install tomli`
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None
from dataclasses import dataclass, field, fields

# OpenAI-compatible endpoints. Swapping models = changing one line (or LLM_PROVIDER env var).
PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.8-flash", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1/", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/", "meta-llama/llama-3.3-70b-instruct:free", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1/", "gpt-4o-mini", "OPENAI_API_KEY"),
    "ollama": ("http://localhost:11434/v1/", "qwen2.5:7b", ""),
}

# Tried in order when the main model is overloaded, retired or out of daily quota.
# On Gemini's free tier each model has its own quota (3.8/3.6 Flash: 5 RPM, 20 requests/day;
# 3.5 Flash-Lite: 500/day), so the chain also stretches what one free key can do.
FALLBACKS = {
    "gemini": ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
}
# Seconds between requests, to stay under the provider's free-tier requests-per-minute.
PACING = {
    "gemini": 13.0,   # 5 RPM
    "groq": 2.5,      # 30 RPM
    "openrouter": 3.5,
}


@dataclass
class Config:
    # promo rules from the brief
    min_len: float = 20.0
    max_len: float = 60.0
    hook_window: float = 3.0

    # candidate search (rules backend)
    per_episode: int = 3          # promos kept per episode
    max_overlap: float = 0.3      # max fraction two kept promos may share

    # ranking weights
    w_hook: float = 0.40
    w_cliff: float = 0.35
    w_standalone: float = 0.25
    w_llm: float = 0.7            # blend: LLM judgement vs rule features (LLM backend only)
    funnel_bonus: float = 0.05    # early episodes send new viewers straight into ep 1-10
    funnel_max_ep: int = 10
    same_ep_penalty: float = 0.15 # diversity when ordering the posting list
    near_ep_penalty: float = 0.06
    near_ep_distance: int = 2
    low_confidence: float = 0.65  # promos scoring below this get flagged for the editor

    # LLM
    provider: str = "gemini"
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    temperature: float = 0.3
    include_camera: bool = False
    max_retries: int = 4
    overload_retries: int = 2     # retries per model on HTTP 5xx before falling back
    retry_base: float = 10.0      # seconds; backoff = retry_base * attempt
    episodes_per_call: int = 10   # batch episodes into one request (free tiers cap requests/day)
    min_interval: float = -1.0    # seconds between requests; -1 = provider default (PACING)
    fallback_models: list = field(default_factory=lambda: ["auto"])  # ["auto"] = provider default
    rules_fallback: bool = True   # episodes the LLM fails on get the rules backend's best pick
    cache: bool = True            # reuse identical successful requests (resume after a stop)
    repair_rounds: int = 1
    montages: int = 5             # cross-episode promos to request (0 = off)
    pool_per_episode: int = 3

    extra: dict = field(default_factory=dict)

    def llm_settings(self):
        base, model, key_env = PROVIDERS.get(self.provider, PROVIDERS["gemini"])
        base = os.environ.get("LLM_BASE_URL", self.base_url or base)
        model = os.environ.get("LLM_MODEL", self.model or model)
        key_env = self.api_key_env or key_env
        key = os.environ.get("LLM_API_KEY") or (os.environ.get(key_env, "") if key_env else "")
        return base, model, key

    def llm_models(self) -> list[str]:
        _, model, _ = self.llm_settings()
        env = os.environ.get("LLM_FALLBACK_MODELS")
        if env is not None:
            fb = [m.strip() for m in env.split(",") if m.strip()]
        elif self.fallback_models == ["auto"]:
            fb = FALLBACKS.get(self.provider, [])
        else:
            fb = list(self.fallback_models)
        return [model] + [m for m in fb if m != model]

    def pacing(self) -> float:
        env = os.environ.get("LLM_MIN_INTERVAL")
        if env is not None:
            return float(env)
        return self.min_interval if self.min_interval >= 0 else PACING.get(self.provider, 0.0)


def load_config(path: str | None = None, **overrides) -> Config:
    cfg = Config()
    if path:
        if tomllib is None:
            raise SystemExit("--config needs Python 3.11+ or `pip install tomli`")
        with open(path, "rb") as f:
            data = tomllib.load(f)
        names = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in names:
                setattr(cfg, k, v)
    if os.environ.get("LLM_PROVIDER"):
        cfg.provider = os.environ["LLM_PROVIDER"]
    if os.environ.get("LLM_RETRY_BASE"):
        cfg.retry_base = float(os.environ["LLM_RETRY_BASE"])
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    return cfg
