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
| `model` | string | LLM backend only: model name |
| `calls` | int | LLM backend only: successful API calls |
| `prompt_tokens`, `completion_tokens` | int | LLM backend only: usage reported by the provider |

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
| `flags` | string[] | `weak_open`, `needs_context: ...`, `low_confidence: ...` |
| `raw` | object | LLM backend: the model's original proposal before snapping |

## `promos.csv`

One row per promo with the same information flattened. `segments` is written as
`E1 00:36.50-01:01.50 + E4 00:10.00-00:22.00`.

## `raw_llm/`

JSON Lines, one file per episode (`ep_001.json`, ...) plus `montages.json`. Each line is one call:
`model`, `system`, `user`, and `response` or `error`. Repair rounds are appended as further lines.
