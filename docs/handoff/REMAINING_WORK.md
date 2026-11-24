# Remaining work (handoff prompt)

Paste everything below the line into the assistant that will continue the work.

---

You are continuing work on the LegalMind backend: a FastAPI service that answers questions about a
published collection of laws and cites the provision and PDF page for every statement.

## Get the code

```bash
git clone https://github.com/MharoonZ/legalmind-backend
cd legalmind-backend
git checkout feature/ask
python -m venv .venv && source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt
pytest -q            # expected: 575 passed, 5 skipped
ruff check .         # expected: clean
```

Read `README.md`, `docs/ARCHITECTURE.md` and `docs/AUDIT_2026-11.md` first. The answer path is
`app/retrieval` (snapshot, law resolver, reference parser, BM25, fusion) and `app/qa` (rerank,
context, prompt, llm, citations, service). Tests use the in-memory fixture in `tests/corpus.py` and
need no database, model or network.

## Rules

- Work on branch `feature/ask`. Do not open a pull request unless asked.
- Never commit `.env`, API keys or database passwords.
- No fix may be specific to one law, one section number or one question.
- Every change needs a test that fails without it. Keep `pytest` and `ruff check .` green.
- A citation's `quote` must always be the law's own wording taken from the published chunk text,
  never the model's text. Statute brackets such as `1[three years]` and `section 3[59]` are part of
  the law and must never be treated as citation markers.
- Do not change the `/ask` or `/search/legal` response fields, the database schema or migrations.
- Do not move the Phase A/B modules (`app/services`, `app/repositories`, `app/workers`).

## Task 1: review and apply three unfinished patches

Three patches are in `docs/handoff/`. They were written by another engineer and were NOT reviewed.
Each applies cleanly to this commit (`git apply docs/handoff/<name>.patch`). Review every line
before keeping it, and try to break it.

### `llm.patch` (app/qa/llm.py, tests/test_qa_llm.py) — suite passed with it applied (602 passed)
It is meant to make these true. Verify each one yourself:
1. A non-retryable failure of one model call (HTTP 400, a 404 on the primary, a blocked prompt)
   must not discard another call that is still in flight or stop the other model being tried.
   Only a credential rejection (401/403) ends the exchange at once.
2. The extra call a rate-limited model gets after its stated wait happens at most once per model
   per exchange. Total calls per exchange are bounded by
   `1 + qa_llm_max_retries + (_MAX_PARALLEL - 1) + number of models`.
3. A stated rate-limit wait is never shortened or cleared by a later plain 503, a timeout, or the
   success of a call started before the wait was stated.
4. No OpenAI attempt runs past the exchange deadline.
5. `generate_json(..., timeout=None)` is a new keyword that bounds the whole exchange including
   OpenAI failover.
6. When every model is rate limited past the deadline, the error states the wait the provider
   actually asked for (seconds, minutes or hours) and keeps the phrase "rate limit was reached".
7. `_MAX_COOLDOWN_SECONDS` is 900.

Check especially: the loop never spins without awaiting, never runs past the deadline, never has
more than three calls in flight, and always cancels and awaits pending tasks.

### `service.patch` (app/qa/service.py, tests/test_qa_service.py, tests/corpus.py, app/core/config.py, .env.example) — suite passed with it applied (580 passed)
It depends on the `timeout` keyword from `llm.patch`. It is meant to:
1. Give `ask()` one deadline (`qa_request_timeout_seconds` minus a small margin) shared by every
   model exchange, with an injectable clock for tests.
2. Cap the optional question-restatement exchange with a new setting `qa_rewrite_timeout_seconds`
   (default 6) and skip it when under about one second is left.
3. Make no model call when too little time is left: the first call then falls back to the
   text-only reply (named provisions) or `service_unavailable`; a corrective call is skipped and
   the first draft is used.
4. Make the text-only reply (used when no model is reachable) show each provision exactly as
   `snapshot.display[chunk.index]`, with no character altered.

### `citations.patch` (app/qa/citations.py, tests/test_qa_citations.py) — UNFINISHED
With it applied one test outside its files fails:
`tests/test_qa_service.py::test_model_judged_gaps_and_clarifications_are_passed_through`.
Find out whether the patch or the test is wrong and fix the right one. The patch is meant to:
1. Match a citation marker against the sentence or list item that contains it. A marker placed
   after the full stop ("...one year. [E1]") belongs to the sentence before it. Adjacent markers
   ("[E2] [E1]", "[E2] and [E1]") share one statement.
2. Never split an answer sentence after a list number ("1.") or an abbreviation ("No.", "Rs.",
   "s.", "Art."). "(Act No. II of 2005)" must never become "(Act No [1]. II of 2005)".
3. Not depend on shared English words alone: count numbers (any Unicode digit, and English number
   words in the source) so an Urdu answer can still be matched; attach a quote that matches no
   sentence by position, so every returned citation is referenced by at least one `[n]`.
4. Cut a long fallback quote on word boundaries, choosing the window that best supports the
   statement, never mid-word.
5. Remove control characters from the model's answer before processing, and read a bare `[2]`
   written by the model as `[E2]` unless it is attached to a preceding letter, digit or bracket.
6. Rewrite inline "- item" lists only when there are at least two, never before "(", and keep the
   indentation of nested list lines.

After applying all three, run the whole suite: the patches were written in parallel and have never
been tested together.

## Task 2: fix these reported defects

A reviewer reproduced each of these, but nobody verified them independently. Confirm each against
the code before fixing. One (evidence ids colliding for split provisions) is already fixed.

High:
1. `app/qa/smalltalk.py` — an ordinary question such as "What does the Consumer Protection Act
   cover?" is answered as small talk with the list of laws: no retrieval, no model call. A
   catalogue reply must require the question to be about the collection itself ("which laws do
   you have").
2. `app/retrieval/service.py` (`_CONTEXT_BIAS`) — after one turn about law A, a follow-up on a
   different topic returns only law A, because the remembered-law bias doubles the fused score.
   Make it a small tie-breaker so a strong hit in another law still wins.
3. `app/retrieval/service.py` (`_infer_law`) — a bare "section N" plus topic words is assigned to
   the law with the most on-topic chunks, not the law whose section N is about the topic. Score
   each candidate law by the relevance of section N itself; ask the user when none stands out.
4. `app/retrieval/loader.py` (`_read_cache`) — a truncated or corrupted cache file raises instead
   of being a cache miss, so the server stays at "no active publication" after a restart. Catch
   it, discard the pair, fall back to the database; fsync before `os.replace`.
5. `app/retrieval/snapshot.py` / `service.py` — "the Schedule" of a law with numbered schedules,
   or "Schedule 1" of a law with one unnumbered schedule, is reported as not found.
6. `app/retrieval/snapshot.py` (`_cross_references`) — a number that belongs to another enactment
   (a schedule table listing "Opium Act, 1878 Section 9", or a rule citing "section 5" of its
   parent Act) is linked to this law's own section or to a same-numbered rule. Link only when the
   stated kind equals the target's kind, and treat a number as foreign when another instrument is
   named nearby.

Medium:
7. A named law that has nothing on the topic never falls back to the whole collection, because
   the law's own name always matches its chunks. Decide from the topic words only.
8. The forced best provision has no upper bound: a 60-part schedule puts 150,000 characters in the
   prompt. Include only the matched part and its neighbours when the whole does not fit.
9. `app/qa/service.py` (`_compose`) — an `insufficient_evidence` reply can keep `[n]` markers with
   no citations. Strip markers whenever citations are cleared.
10. `app/retrieval/lexical.py` — spelling repair rewrites correctly spelt words that are simply
    absent from the collection (wedding to bedding, protest to protect). Repair only words that
    are unlikely to be real words, or use a smaller edit budget for short words.
11. `app/retrieval/index.py` — while no snapshot is held, every request re-runs the full load.
    Remember the last failure and retry on an interval.
12. `app/retrieval/loader.py`, `index.py` — building and caching a snapshot runs on the event
    loop (about 0.8 s for 531 chunks). Move it to a worker thread.
13. A cache hit skips the loader's "still approved and complete" check. Run that check before
    accepting a cache hit.

Low:
14. `readable()` leaves footnote digits fused to years and words ("18988").
15. Spelling repair scans the whole vocabulary per unknown word; cap repairs per query.
16. The cache directory is never pruned and failed writes leave temp files.
17. The text-only reply does not mention a requested provision that does not exist.
18. Exact lookups ignore the caller's `limit`.

## Task 3: areas nobody has reviewed independently

Review these for correctness and security, with a concrete trigger for anything you report:
- `app/retrieval/laws.py`, `references.py`, `query.py`: a law outside the collection resolved to
  one inside it; follow-ups inheriting the wrong law; regular expressions that backtrack badly on
  4,000-character input.
- `app/api/middleware.py`, `deps.py`, `routes/`, `app/core/`: API-key bypass, rate-limit evasion
  by a caller-chosen identity, the loopback-only gate for administration routes, body-size limits
  without `Content-Length`, request-id header injection into logs, semaphore slots leaked on
  cancellation.

## Task 4: verify against the real system

None of this has been run against the project's Supabase database.
1. With the project `.env`, run `python -m scripts.pipeline.download_models`, then
   `uvicorn app.main:app --host 127.0.0.1 --port 8000` and wait for `/health/ready`.
2. Ask both forms of the original bug report and confirm both are answered with a citation to
   Section 3:
   - "What is Section 3 of Punjab Prevention of Gambling Ordinance 1978?"
   - "What is Section 3 of Punjb Prevention of Gambling Ordinance 1978?"
3. Run `LEGALMIND_TEST_DATABASE_URL=<the database url> pytest tests/test_integration_postgres.py`.
4. Run `python -m scripts.eval.evaluate_ask --url http://127.0.0.1:8000 --pause 5` ONCE. The
   Gemini key is limited to about 20 calls a minute and has a daily cap; do not run it repeatedly.
   Expected: all 52 cases pass when the model is reachable.
5. The OpenAI failover path has only been tested with a fake client. If an OpenAI key is
   available, set `OPENAI_API_KEY` and test it once.

## Finish

Update `docs/AUDIT_2026-11.md` (what was fixed, what remains) and `docs/ARCHITECTURE.md` where
behaviour changed, delete `docs/handoff/` once its patches are merged, commit and push to
`feature/ask`.
