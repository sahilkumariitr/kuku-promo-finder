# Promo Finder

[![CI](https://github.com/sahilkumariitr/kuku-promo-finder/actions/workflows/ci.yml/badge.svg)](https://github.com/sahilkumariitr/kuku-promo-finder/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Promo Finder reads the timestamped screenplays of a vertical microdrama and returns a ranked list of
ready-to-cut promo clips** for TikTok, Reels and Shorts, with exact segment timestamps, the hook, the
cliffhanger and a one-line reason for each.

Built for the Kuku AI Generalist assignment (Problem 1: *Find the Promos*). Today an editor watches every
episode to pick promo moments by hand; this tool produces a shortlist for a whole show in seconds so the
editor only reviews the top of the list.

A promo, as defined in the brief, is a 20–60 s clip that:

1. **opens on a hook**: something in the first 3 seconds stops a scrolling viewer;
2. **ends on a cliffhanger**: it cuts at an unresolved moment;
3. **stands alone**: it makes sense to someone who has never seen the show.

---

## Contents

- [Highlights](#highlights)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Output](#output)
- [Configuration and model swapping](#configuration-and-model-swapping)
- [Evaluating quality](#evaluating-quality)
- [Results on the provided show](#results-on-the-provided-show)
- [Limitations](#limitations)
- [Project structure](#project-structure)
- [Further reading](#further-reading)

## Highlights

- **Model-agnostic.** Any OpenAI-compatible endpoint (Gemini, Groq, OpenRouter, OpenAI, a local Ollama
  model, or a company gateway). Swapping models is one flag or environment variable.
- **Never trusts the model's timestamps.** Every proposal is validated in code: cuts snap to shot
  boundaries, never land mid-line, stay within 20–60 s, and must put a line or action in the first 3 s.
  Invalid proposals go back to the model once with the exact errors.
- **Works without an API key.** A deterministic rules backend scores every shot-aligned window. It is
  the free fallback and the baseline the LLM has to beat.
- **Cross-episode promos.** The LLM can join moments from different episodes into one clip.
- **Auditable.** Every prompt and unedited model response is saved to `raw_llm/`.
- **Zero runtime dependencies.** The pipeline is pure Python standard library; Streamlit is optional.
- **Measurable.** A built-in evaluator compares output to an editor's own picks (recall and precision).

## Quick start

Requires Python 3.9+.

```bash
git clone https://github.com/sahilkumariitr/kuku-promo-finder.git
cd kuku-promo-finder
pip install -e ".[app,dev]"
```

**1. Try it on the bundled synthetic show (no key needed):**

```bash
promo-finder --input tests/fixtures/synthetic_show --out outputs/demo --backend rules
```

**2. Run on a real show with an LLM.** A free Gemini key from
[Google AI Studio](https://aistudio.google.com/apikey) is enough:

```bash
echo "GEMINI_API_KEY=your-key" > .env      # or: export GEMINI_API_KEY=your-key
promo-finder --input path/to/show_folder --out outputs/llm --backend llm
```

Keys are read from the environment or a git-ignored `.env` file in the working directory.

**3. Use the web app.** Upload a `.zip` of episode screenplays, then inspect each promo next to its script:

```bash
streamlit run app.py
```

Common flags:

| Flag | Purpose |
|---|---|
| `--backend llm\|rules` | LLM proposals (default) or the no-key rules search |
| `--provider gemini\|groq\|openrouter\|openai\|ollama` | choose a preset endpoint |
| `--model NAME` | override the provider's default model |
| `--episodes 1-10` | process a subset |
| `--no-montages` | skip cross-episode promos |
| `--config config.toml` | override any setting (see `config.example.toml`) |

### Input format

One `.txt` file per episode, in Kuku's screenplay format: a header (`EPISODE NUMBER`, `EPISODE LOGLINE`,
`CONTINUITY NOTES`), scenes, and blocks such as:

```
**CAMERA:**
Start: (00:04.80) | End: (00:06.80)
Close-up on her face as the smile fades.

**CHARACTER NAME (Cold, Commanding):**
Start: (00:11.30) | End: (00:13.10)
"A line of dialogue."
```

See [`tests/fixtures/synthetic_show`](tests/fixtures/synthetic_show) for two complete (original, synthetic) episodes.

## How it works

```mermaid
flowchart LR
    A[Episode screenplays] --> B[Parse<br/><i>code</i>]
    B --> C{Backend}
    C -->|llm| D[Propose promos per episode<br/><i>LLM</i>]
    C -->|rules| E[Score every shot-aligned<br/>20-60 s window<br/><i>code</i>]
    D --> F[Validate and snap<br/><i>code</i>]
    F -->|errors| G[Repair once<br/><i>LLM</i>]
    G --> F
    F --> H[Cross-episode montages<br/><i>LLM</i>]
    E --> I[Score and rank<br/><i>code</i>]
    H --> I
    I --> J[promos.json / .csv / report.md]
    J --> K[Editor review<br/><i>human</i>]
```

| Stage | Done by | Why |
|---|---|---|
| Parse blocks into (episode, start, end, kind, speaker, emotion, text) | code | the format is regular; free and never wrong |
| Propose promos with hook, cliffhanger, reason and 1–5 scores | LLM | "would a stranger stop scrolling and follow this?" needs language understanding |
| Snap to shot boundaries, forbid mid-line cuts, enforce 20–60 s, check the first 3 s, reject non-existent timestamps | code | models invent or fudge timestamps; rules are cheap and certain |
| Fix rejected proposals | LLM, one round | validator errors are sent back verbatim |
| Rule features: emotion-tag intensity, physical action, reveal words, music stings, episode-end cliffhangers, mid-conversation openings | code | a second opinion blended into the score, so one odd LLM answer cannot top the list |
| Posting order | code | score, plus a small bonus for early episodes and a diversity penalty so neighbouring posts come from different scenes |
| Final selection | human | the tool shortlists ~3 per episode and flags low-confidence picks |

Design decisions and trade-offs are explained in [`docs/DESIGN.md`](docs/DESIGN.md).

## Output

Each run writes to the `--out` folder:

| File | Contents |
|---|---|
| `promos.json` | all promos plus run metadata (model, calls, token usage, config) |
| `promos.csv` | one row per promo, for editors and spreadsheets |
| `report.md` | "post first" top 10 and the full ranked table |
| `raw_llm/*.json` | LLM backend only: every prompt and unedited response, including repair rounds |

One promo in `promos.json`:

```json
{
  "rank": 1,
  "id": "P001",
  "kind": "single",
  "episodes": [1],
  "segments": [{"episode": 1, "start": "00:36.50", "end": "01:01.50", "start_s": 36.5, "end_s": 61.5}],
  "duration": 25.0,
  "hook": "GUEST (Mocking): \"Look at her. Pathetic.\"",
  "cliffhanger": "VICTOR (Shocked, Whispering): \"No... it can't be you.\"",
  "why": "Opens on a mocking line; uses the episode's own cliffhanger ending.",
  "llm_scores": {},
  "rule_scores": {"hook": 0.92, "cliffhanger": 0.84, "standalone": 0.84, "duration_fit": 1.0},
  "score": 0.922,
  "flags": []
}
```

This is the top pick from the rules backend on the synthetic show. With the LLM backend, `llm_scores`
holds the model's 1–5 ratings for `hook`, `cliffhanger` and `standalone`.

Full field reference: [`docs/OUTPUT_FORMAT.md`](docs/OUTPUT_FORMAT.md). A complete example run on the
synthetic show is in [`examples/synthetic_output`](examples/synthetic_output).

## Configuration and model swapping

Every endpoint is OpenAI-compatible, so changing models never touches code:

```bash
promo-finder ... --provider groq                     # needs GROQ_API_KEY
promo-finder ... --provider ollama                   # fully local, no key (ollama pull qwen2.5:7b)
LLM_BASE_URL=https://gateway.example.com/v1/ \
LLM_MODEL=stronger-model LLM_API_KEY=... promo-finder ...   # any hosted model
```

| Provider | Default model | Key variable |
|---|---|---|
| `gemini` | `gemini-3.8-flash` | `GEMINI_API_KEY` |
| `groq` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| `openrouter` | `meta-llama/llama-3.3-70b-instruct:free` | `OPENROUTER_API_KEY` |
| `openai` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `ollama` | `qwen2.5:7b` | none |

Ranking weights, length limits, the early-episode bonus and the LLM/rules blend are all in
[`config.example.toml`](config.example.toml). Prompts live in
[`promo_finder/prompts.py`](promo_finder/prompts.py) so they can be versioned separately from code.

## Evaluating quality

Label the promos you would cut yourself in a few episodes (`episode,start,end,note`; see
[`eval/gold_template.csv`](eval/gold_template.csv)), then:

```bash
promo-finder-eval --pred outputs/llm/promos.json --gold eval/gold.csv
```

It reports **recall** (share of your picks the tool found, at ≥50% overlap) and **precision** (share of
tool segments that match one of your picks), with a per-pick breakdown. Only labelled episodes are scored.

Tests run on the synthetic show by default, and on any real show via `PROMO_DATA`:

```bash
pytest -q                                   # includes the full LLM path against a local fake API
PROMO_DATA=path/to/show_folder pytest -q
```

## Results on the provided show

The input pack (50 episodes of an English-language show, ~105 minutes, 3,727 timestamped blocks) is
Kuku's confidential content and is **not** included in this repository. Summary of the rules-backend run:

| Metric | Value |
|---|---|
| Episodes processed | 50 / 50 |
| Promos found | 141 (1–3 per episode) |
| Clip length | 22–59 s, all within 20–60 s |
| Runtime | ~2 s, no API cost |
| Flagged low-confidence | 16 (quiet, dialogue-only episodes) |
| Estimated LLM-backend cost | ~80k input / ~25k output tokens per show, 51 calls (fits the Gemini free tier) |

Manual review of the top picks against the script: most open on a confrontation and cut on an episode's
own cliffhanger or an unanswered line. The failures found during review are listed below and in
[`docs/DESIGN.md`](docs/DESIGN.md#quality-review-log).

## Limitations

- **Standalone clarity is approximated.** The rules backend uses proxies (2–4 speakers in conflict, no
  opening on a reply or continuation). It cannot tell when a line refers to a plot point a stranger has
  not seen. That judgement is the LLM's job.
- **Strong hook, soft ending.** When a scene resolves into a tender moment and long shots leave no cut
  point at peak tension, the only valid end is soft and the promo ranks lower than it deserves.
- **Quiet episodes still get a pick.** These are flagged `low_confidence` rather than dropped.
- **Format-dependent rules.** Rule features rely on emotion tags and sound notes; a screenplay without
  them weakens the rules backend (the LLM backend is less affected).
- **Cross-episode promos require the LLM backend.**
- **Screenplay time is assumed to equal final-cut time.** If the edit drifts, timestamps need an offset.

## Project structure

```
promo_finder/
├── parse.py           screenplay text → episodes and timestamped blocks
├── features.py        rule signals: hook, cliffhanger, standalone, duration fit
├── promo.py           Promo model, shot snapping, validation, rule scoring
├── rules_backend.py   no-key candidate search
├── llm.py             OpenAI-compatible client (stdlib), retries, JSON extraction
├── prompts.py         all prompts
├── llm_backend.py     per-episode proposals, repair loop, montages, raw logging
├── rank.py            final score and diversity-aware posting order
├── config.py          every tunable and the provider presets
├── run.py             CLI (promo-finder)
└── evaluate.py        compare against editor picks (promo-finder-eval)
app.py                 Streamlit interface
tests/                 pytest suite, fake OpenAI-compatible server, synthetic show
examples/              example output on the synthetic show
docs/                  design notes and output reference
```

## Further reading

- [`docs/DESIGN.md`](docs/DESIGN.md): problem framing, assumptions, design decisions, scaling, quality review log
- [`docs/OUTPUT_FORMAT.md`](docs/OUTPUT_FORMAT.md): every output field
- [`CONTRIBUTING.md`](CONTRIBUTING.md): development setup

## License

[MIT](LICENSE). Screenplay content used during development belongs to Kuku and is not distributed here.
