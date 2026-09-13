# Wikimedia Agent — Project Plan

A question-answering agent that grounds every answer in live Wikipedia content, with
inline citations back to the source articles.

**Status:** draft — plan only, no implementation yet.

---

## 1. Goal and scope

### In scope
- Natural-language questions answered from English Wikipedia content.
- Answers carry citations (article title + section + URL) for each claim.
- Explicit "I don't know" when Wikipedia does not support an answer.
- Multi-hop questions (e.g. "Who directed the highest-grossing film of 1997?")
  handled through iterative search — the agent may make several retrieval calls
  per question.
- A CLI for interactive use, and a Python API for embedding elsewhere.

### Out of scope (v1)
- Languages other than English Wikipedia.
- Other Wikimedia projects (Wikidata, Wiktionary, Commons).
- Conversation memory across sessions / long multi-turn dialogue.
- A web UI or hosted service.

### Non-goals
- Not a general web-search agent. If it isn't in Wikipedia, the agent says so.
- Not a Wikipedia mirror — no bulk dump ingestion, no local index to maintain.

---

## 2. Architecture

**Retrieval:** live Wikimedia APIs, queried at question time. Always current, no
ingestion pipeline, no index staleness. The cost is per-question latency, which we
mitigate with caching and concurrent fetches.

**Stack:** Python 3.11+, the official `anthropic` SDK, `httpx` for the Wikipedia
calls, `pytest` for tests.

**Model:** `claude-opus-5` with adaptive thinking. Multi-hop retrieval decisions are
exactly the kind of thing thinking helps with. Reference cost: $5 / $25 per million
input / output tokens. We will measure real per-question cost during the eval phase
(§5) and only then decide whether a cheaper model or a lower `effort` setting holds
quality — that's a measured decision, not an upfront one.

**Agent loop:** the Anthropic SDK's tool runner (`client.beta.messages.tool_runner`)
drives the request → tool-execute → loop cycle. We supply tool functions; the SDK
supplies the loop. If we later need approval gates or custom retry behaviour, the
runner's per-turn hooks cover it without hand-writing the loop.

```
question
   ↓
agent loop (Claude + tools)
   ├── search_wikipedia(query)      → candidate article titles + snippets
   ├── get_article(title, section?) → article text, section-scoped
   └── get_summary(title)           → short lead extract, cheap disambiguation
   ↓
answer + citations
```

### Tool surface

| Tool | Wikimedia endpoint | Purpose |
|---|---|---|
| `search_wikipedia(query, limit)` | Action API `list=search` | Find candidate articles for a topic |
| `get_summary(title)` | REST `/page/summary/{title}` | Cheap lead paragraph; resolve disambiguation before spending tokens on a full article |
| `get_article(title, section=None)` | Action API `prop=extracts` / section index | Full text or one section, so long articles don't blow the context window |

Section-scoped fetching is the key design decision: large articles are fetched by
section rather than whole, which keeps context spend proportional to the question.

### Grounding contract
The system prompt requires the agent to answer only from retrieved text, cite the
article and section behind each factual claim, and say plainly when retrieval came
back empty or contradictory. Citations are what make the agent auditable and what
§5 grades against.

### Operational requirements
- **User-Agent header on every request.** The Wikimedia API policy requires a
  descriptive UA with contact info; requests without one get rate-limited or blocked.
  This is not optional and is checked in tests.
- **Rate limiting and retries.** Conservative concurrency cap, exponential backoff on
  429/5xx.
- **Response caching.** Keyed on endpoint + params, TTL-bounded (articles change).
  Cuts both latency and API load, and makes the eval suite reproducible and cheap.

---

## 3. Repository layout

```
wikimedia-agent/
├── project-plan.md
├── README.md
├── pyproject.toml
├── src/wikimedia_agent/
│   ├── agent.py           # agent loop, system prompt, tool wiring
│   ├── tools.py           # @beta_tool definitions
│   ├── wikipedia.py       # API client: UA, retries, cache
│   ├── citations.py       # citation extraction + formatting
│   └── cli.py             # entry point
├── tests/
│   ├── unit/              # mocked API, no network, no model calls
│   └── integration/       # real API, recorded fixtures
└── evals/
    ├── dataset.jsonl      # graded question set
    └── run_eval.py
```

---

## 4. Build phases

Each phase ends with a commit pushed to `main`. Phases 1–3 are independently testable
without spending a cent on model calls.

| # | Phase | Deliverable | Done when |
|---|---|---|---|
| 0 | Plan & scaffold | This document, `pyproject.toml`, CI skeleton | Plan committed; `pytest` runs green on an empty suite |
| 1 | Wikipedia client | `wikipedia.py` — UA, retries, caching, section fetch | Unit tests pass against mocked responses; integration tests pass against the live API |
| 2 | Tool layer | `tools.py` — the three tools with typed schemas | Each tool callable standalone; bad titles, disambiguation pages, and empty results handled |
| 3 | Agent loop | `agent.py` + system prompt + citation formatting | Answers a single-hop question end to end with a correct citation |
| 4 | Multi-hop & robustness | Iterative retrieval, retrieval-failure paths, token budget | Answers a two-hop question; refuses cleanly when Wikipedia lacks the answer |
| 5 | Evaluation harness | `evals/` — dataset and runner (§5) | Full suite runs, emits a scored report, per-question cost recorded |
| 6 | CLI & docs | `cli.py`, README with setup and examples | A new user can install and ask a question from the README alone |

---

## 5. Validation

Three layers, cheapest first. The eval set is built before we start tuning prompts,
so we are never tuning against a moving target.

### Layer 1 — Unit tests (no network, no model)
Run on every commit, fast.
- Wikipedia client: URL construction, UA header present, retry/backoff behaviour,
  cache hit/miss, section extraction.
- Tool functions against recorded fixtures: normal article, disambiguation page,
  missing title, redirect, very long article.
- Citation formatting and parsing.

### Layer 2 — Integration tests (real API, no model)
Run on demand and nightly — these can break when Wikipedia changes, and that's the
point of separating them.
- Each endpoint returns the expected shape against live Wikipedia.
- A known-stable article (e.g. a long-settled historical topic) fetches and
  section-splits correctly.

### Layer 3 — Agent evals (real model, costs money)
The question set lives in `evals/dataset.jsonl`, each entry carrying the question, a
reference answer, and the article(s) that should be cited. Target ~40–60 questions
across five categories:

| Category | What it probes | Example shape |
|---|---|---|
| Single-hop factual | Basic retrieval + extraction | "When was X founded?" |
| Multi-hop | Iterative retrieval | "Who succeeded the person who did X?" |
| Ambiguous entity | Disambiguation handling | A name shared by several subjects |
| Not-in-Wikipedia | Honest refusal | Something Wikipedia genuinely doesn't cover |
| Recently changed | Freshness vs. a stale index | A topic updated in the last month |

**Grading.** Three scores per question:
1. **Answer correctness** — LLM-as-judge against the reference answer, with a
   sample hand-checked to confirm the judge is calibrated.
2. **Citation validity** — programmatic, not judged: every cited article must exist,
   and the cited text must actually appear in the fetched content. This catches
   fabricated citations, which is the failure mode that matters most here.
3. **Refusal correctness** — on the not-in-Wikipedia set, did it decline instead of
   inventing an answer?

**Also recorded per run:** tokens and dollar cost per question, wall-clock latency,
and number of retrieval calls. These are the numbers that justify any later change
to model or effort level.

**Data split.** The set is split train / validation / test. Prompt iteration happens
against train and validation; the test slice is scored but never tuned against, so
the headline number stays honest.

**Bar for v1:** ≥85% answer correctness on the test split, ≥95% citation validity
(a fabricated citation is worse than a wrong answer — it looks trustworthy), and
≥90% correct refusals.

### Continuous validation
- Unit + lint on every push to `main`.
- Integration tests nightly (they depend on a live third-party API).
- Evals run manually before any release, and after any prompt or model change.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| Fabricated citations — the answer looks sourced but isn't | Programmatic citation verification in the eval suite, scored separately and held to a higher bar than answer correctness |
| Wikipedia API rate limits or blocks | Compliant User-Agent, conservative concurrency, backoff, caching |
| Long articles exhausting the context window | Section-scoped fetching; summary-first disambiguation |
| Wikipedia content itself being wrong or vandalised | Out of our control — we ground and cite, so the user can check the source. Document this limitation in the README |
| Per-question cost drifting upward | Cost recorded per eval run; prompt caching on the stable system prompt + tool definitions |
| Multi-hop loops running away | Cap retrieval calls per question; consider a task budget on the agent loop |

---

## 7. Git workflow

- Work commits directly to `main` at each phase boundary, per the project owner's
  preference.
- Every commit leaves the test suite green.
- `project-plan.md` is living — it is updated in the same commit as any change that
  invalidates it.

---

## 8. Open questions

1. Should the agent expose a raw "what did you retrieve?" mode for debugging and
   trust-building, or keep retrieval internal?
2. Is a hard cap on retrieval calls per question the right control, or a token-based
   task budget?
3. Should the not-in-Wikipedia case suggest what the user could search instead, or
   simply decline?
