# Design notes

## 1. Problem framing

**Who:** the promo editor who currently watches every episode of every show to pick promo moments.
**Step automated:** shortlisting candidate clips with exact in/out points. Editing, music and overlays
are out of scope (as stated in the brief).
**Output that can be checked:** a ranked list of segments, each with timestamps that either exist in the
script or are rejected.

The tool does not replace the editor. It turns "watch ~105 minutes per show" into "review the top 10–20
shortlisted clips and skip anything flagged".

## 2. Assumptions

| Assumption | Why it is reasonable | What breaks if wrong |
|---|---|---|
| Screenplay timestamps match the final cut | scripts are timestamped per line and per shot | clip times need a per-episode offset |
| Editors cut on shot boundaries and never mid-line | standard editing practice | snapping could be relaxed via config |
| A promo may come from any episode | the brief allows single or multi-episode clips | none |
| Early episodes are slightly better promos | a new viewer who taps lands on episode 1 | `funnel_bonus` is tunable (default 0.05) |
| Spoilers are acceptable when the title already reveals the twist | microdrama titles usually state the premise | add a spoiler penalty for twist-driven shows |
| File numbering may not equal story order | in the provided pack, some adjacent loglines read out of order and several episodes retell one event | montages are ordered by loglines, not file numbers |

## 3. Key decisions

### LLM proposes, code disposes
Models are good at judging whether a moment hooks a stranger and bad at reproducing timestamps exactly.
So the LLM only *proposes*; code validates every segment against the parsed script, snaps it to real
shot boundaries, extends any cut that would land mid-line, enforces 20–60 s, and checks that a line or
action occurs within the first 3 seconds. Anything still invalid is returned to the model once with the
validator's exact error messages, then dropped.

### Two backends, one validator and ranker
The rules backend exists so that:
1. the tool works with no key, no network and no cost;
2. there is a baseline to measure the LLM against;
3. a provider outage never blocks a release.

It enumerates every window that starts on a shot containing a line or action and ends on a shot boundary
not inside a spoken line, scores it, and keeps the top three non-overlapping windows per episode.

### Blended scoring
`score = w_llm * LLM rating + (1 - w_llm) * rule features`, multiplied by a duration-fit factor (best at
25–45 s). The rule features act as a guard rail: a single over-enthusiastic LLM rating cannot push a clip
with no action in its first 3 seconds to the top.

Rule features, built from the vocabulary that actually occurs in the scripts:

| Signal | Source |
|---|---|
| Emotional intensity | dialogue emotion tags in three tiers (e.g. *screaming/gloating* > *cold/stern* > *gentle/soft*) |
| Physical action | ACTION/SFX lines mentioning slaps, grabs, chains, fire, blood, etc. |
| Reveal words | marriage, poison, heir, secret, betrayal, execution, ... |
| Cliffhanger cues | the episode's own ending, SFX stings ("swell", "ominous", "cut to black"), lines ending on `?` or `...` |
| Needs-context penalty | opening on a reply (`And...`, `But...`), a continuation of the previous line, or a bracketed reaction |

### Posting order
After scoring, a greedy pass orders promos for posting and penalises a clip that shares an episode (or a
neighbouring episode) with one of the previous five posts. The goal is a feed that shows several sides
of the story instead of five cuts of the same scene.

### Model swapping
All providers are reached through the OpenAI-compatible `/chat/completions` protocol with a stdlib HTTP
client, so a stronger model (or an internal gateway) is a base URL and a model name. JSON mode is
requested and automatically dropped for providers that reject it; output is parsed defensively (fenced
code, surrounding prose). Retries use exponential backoff for rate limits.

## 4. Scale and cost

| | Per show (50 episodes) |
|---|---|
| Transcript tokens (camera directions dropped) | ~48k |
| Total input incl. prompts | ~80k |
| Output | ~25k |
| Calls | 50 episode calls + 1 montage call (+ repairs) |
| Cost | free tier on Gemini Flash; a few cents at paid flash-class prices |
| Rules backend | ~2 s, no cost |

Episodes are independent, so a weekly slate of shows parallelises trivially. At real volume:

1. run the rules backend on upload for an instant shortlist;
2. run the LLM backend asynchronously;
3. log which promos editors actually post, and feed that back as labelled data to tune weights and prompts.

## 5. How we know it is good

1. **Hard constraints are enforced in code**: every output clip is 20–60 s, shot-aligned, never cut
   mid-line. These are tested on every commit (`tests/test_pipeline.py`).
2. **Editor agreement**: `promo-finder-eval` measures recall and precision against an editor's own picks.
3. **Manual review** of the top picks against the script (log below).
4. **In production**: share of shortlisted promos that editors post, time saved per show, and the posted
   promos' engagement (3-second hold rate, completion rate, click-through to episode 1) compared with
   hand-picked promos.

## Quality review log

Findings from reviewing rules-backend output on the provided 50-episode show. Script lines are not
quoted because the content is confidential.

| # | Finding | Cause | Action |
|---|---|---|---|
| 1 | The #1 pick opened on a scoffing reaction to a line the viewer never hears | needs-context check ignored bracketed reactions and continuations of the previous line | added both checks; the pick fell to rank ~55 |
| 2 | After that fix, 106 of 127 promos were flagged as opening mid-conversation and the top list filled with action-only openings | the "starts lowercase" pattern was compiled case-insensitive, so it matched every line; it had silently penalised every opening since the first run | scoped the flag to the conjunction words only; now 1 genuine case is flagged, and every episode has at least one promo |
| 3 | Some promos had an empty cliffhanger description | the final seconds before the cut were camera-only | fall back to the last story beat before the cut (caught by the test suite) |
| 4 | One of the strongest turns in the show (a public confrontation, episode 42) ranks #111 | perfect hook score, but the scene resolves into a tender reunion and the long shots around it leave no cut point at peak tension | documented as a limitation; an LLM or an editor proposing an earlier cut would do better |
| 5 | Quiet, dialogue-only episodes still produce a pick | one promo per episode is always kept | flagged `low_confidence` (16 of 141) instead of dropped |
| 6 | Local endpoints on `127.0.0.1` were asked for an API key | the key check only recognised `localhost` | fixed; covered by the end-to-end test |
| 7 | First real Gemini run produced 0 promos | the default model (`gemini-2.5-flash`) had been retired for new API keys (HTTP 404), and the tool kept calling it for every episode while only saying "see raw log" | default moved to `gemini-3.8-flash`; fatal errors (bad key, no access, unknown model) now stop the run after one call and print the provider's message; regression test added |
