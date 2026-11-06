# Output format

## `promos.json`

```json
{ "meta": { ... }, "promos": [ { ... }, ... ] }
```

### `meta`

| Field | Type | Description |
|---|---|---|
| `show` | string | show name (input folder name) |
| `backend` | `"llm"` \| `"rules"` | backend used |
| `episodes` | int | episodes processed |
| `generated` | string | local timestamp of the run |
| `seconds` | float | wall-clock runtime |
| `config` | object | every setting used for the run |
| `model` | string | LLM backend only: primary model name |
| `models_used` | object | LLM backend: answers per model, including ones served from cache |
| `live_requests_by_model` | object | LLM backend: successful live responses per model in this run |
| `calls` | int | LLM backend: successful live responses |
| `http_requests` | int | LLM backend: HTTP requests sent, including retries (what free-tier quotas count) |
| `cache_hits` | int | LLM backend: requests answered from `<out>/cache/` |
| `planned_requests` | int | LLM backend: batches + montage request, before repairs |
| `rules_fallback_episodes` | int[] | LLM backend: episodes that fell back to the rules backend's pick |
| `incomplete` | string | LLM backend, only if the run stopped early: the reason |
| `prompt_tokens`, `completion_tokens` | int | LLM backend: usage reported by the provider (live requests only) |

### Promo

Promos are listed in posting order.

| Field | Type | Description |
|---|---|---|
| `rank` | int | posting order, 1 = post first |
| `id` | string | `P001`, `P002`, ... |
| `kind` | `"single"` \| `"montage"` | one episode, or moments joined across episodes |
| `episodes` | int[] | episodes the promo draws from |
| `segments` | object[] | in playback order: `episode`, `start`/`end` (`MM:SS.ss`), `start_s`/`end_s` (seconds) |
| `duration` | float | total seconds across segments |
| `hook` | string | what happens in the first 3 seconds |
| `cliffhanger` | string | the unresolved moment the promo cuts on |
| `why` | string | one line on why it works |
| `llm_scores` | object | LLM backend: integer 1–5 for `hook`, `cliffhanger`, `standalone` |
| `rule_scores` | object | 0–1 for `hook`, `cliffhanger`, `standalone`, `duration_fit`, plus `notes` |
| `score` | float | final blended score used for ranking |
| `backend` | string | `llm`, `rules`, or `rules-fallback` |
| `flags` | string[] | `weak_open`, `needs_context: ...`, `low_confidence: ...`, `fallback: ...` |
| `raw` | object | LLM backend: the model's original proposal before snapping, plus the `model` that answered |

## `promos.csv`

One row per promo with the same information flattened. `segments` is written as
`E1 00:36.50-01:01.50 + E4 00:10.00-00:22.00`.

## `raw_llm/`

JSON Lines, one file per batch (`ep_001-010.json`, ...) plus `montages.json`. Each line is one request:
`model`, `system`, `user`, and `response` or `error`; `cached: true` marks answers reused from `cache/`.
A repair request, when needed, is appended as a further line.

## `cache/`

One file per successful request, named by a hash of its prompt: `{"model": ..., "response": ...}`. Rerunning
the same command reuses these instead of calling the API. Delete the folder to force fresh answers.
