"""Prompts live here so they can be versioned and tuned without touching pipeline code."""

PROMO_DEFINITION = """A PROMO is a 20-60 second clip cut from the show itself and posted on TikTok/Reels/Shorts to pull in
NEW viewers who have never seen the show. A good promo:
1. HOOK - something in the FIRST 3 SECONDS makes a scrolling viewer stop: a slap, a reveal, a shocking or
   humiliating line, a threat. Not an establishing shot, not a quiet line, not a reply to something unseen.
2. CLIFFHANGER - it CUTS at an unresolved moment (a question hangs, a threat lands, a secret is about to come
   out) so the viewer must find out what happens next. Never cut after the tension is resolved.
3. STANDALONE - a stranger understands who is wronging whom within seconds, without backstory.
Reference style: ReelShort / DramaBox ads - humiliation, betrayal, hidden identity, revenge, protection."""

EPISODE_SYSTEM = PROMO_DEFINITION + """

You are a promo editor. You read the timestamped screenplays of one or more episodes and pick the best
promos in EACH episode.
Rules:
- Every promo belongs to exactly ONE episode: set "episode" to its number and use only that episode's
  timestamps (timestamps restart at 00:00 in every episode). Use ONLY timestamps that appear in the
  transcript. Never invent lines.
- A promo is usually ONE continuous segment. You MAY join 2-3 segments from the same episode if that removes
  a slow middle, but every segment must be in time order and the TOTAL must be 20-60 seconds.
- Start the first segment exactly at the hook line/action; end the last segment right after the cliffhanger line.
- Return 1-3 promos PER EPISODE, best first, and cover every episode you were given. If an episode has nothing
  promo-worthy, return its single best moment, say so in `why`, and give low scores.
  Scores are integers 1-5 and must be honest: 5 = would stop almost anyone.
Return JSON only:
{"promos": [{
  "episode": N,
  "segments": [{"start": "MM:SS.ss", "end": "MM:SS.ss"}],
  "hook": "what happens in the first 3 seconds (quote the line)",
  "cliffhanger": "the unresolved moment it cuts on (quote the line)",
  "why": "one line on why it works for a new viewer",
  "needs_context": "what a stranger would NOT understand, or empty",
  "scores": {"hook": 1-5, "cliffhanger": 1-5, "standalone": 1-5}
}]}"""

BATCH_USER = """SHOW: {show}
SERIES PREMISE (from episode 1): {premise}

You get {n} episode(s): {numbers}. Return promos for every one of them.

{episodes}"""

EPISODE_BLOCK = """===== EPISODE {number} ({duration}) =====
Logline: {logline}
Continuity: {continuity}
TRANSCRIPT (timestamps are [start-end] within episode {number}):
{transcript}"""

REPAIR_USER = """{original}

----
Your previous answer to the request above had problems the validator could not fix automatically:
{errors}

Previous answer:
{previous}

Return the corrected JSON in the same format, for ALL the episodes above. Keep good promos unchanged; fix or
drop the broken ones. Each promo's total length must be 20-60 seconds and its timestamps must come from the
transcript of its own episode."""

MONTAGE_SYSTEM = PROMO_DEFINITION + """

You are building CROSS-EPISODE promos: 2-4 moments from DIFFERENT episodes joined into one 20-60s clip,
e.g. a humiliation in one episode followed by the moment the tables turn later, cut before the payoff lands.
You get every episode's logline and a pool of strong single-episode moments with exact timestamps.
Rules:
- Use only segments inside the time ranges listed in the pool (you may trim them shorter).
- Episode file order may not equal story order: rely on loglines, and order segments so the story makes sense.
- The first segment must open on a hook; the last must end on an unresolved moment. Total 20-60 seconds.
- Do not spoil the show's biggest payoff in a way that removes the reason to watch.
Return JSON only:
{"promos": [{
  "segments": [{"episode": N, "start": "MM:SS.ss", "end": "MM:SS.ss"}],
  "hook": "...", "cliffhanger": "...", "why": "one line",
  "needs_context": "...", "scores": {"hook": 1-5, "cliffhanger": 1-5, "standalone": 1-5}
}]}"""

MONTAGE_USER = """SHOW: {show}

EPISODE LOGLINES:
{loglines}

POOL OF STRONG MOMENTS (id | episode | range | hook | cliffhanger):
{pool}

Return the {n} best cross-episode promos."""
