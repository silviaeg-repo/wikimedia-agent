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
ingestion pipeline, no index staleness. The cost is per-question latency. We mitigate
it with caching and with batching several titles into one request — *not* with
concurrency, which the API etiquette rules out (§2.1).

**Stack:** Python 3.11+, `httpx` for the Wikipedia calls, `pytest` for tests. Vendor
SDKs (`anthropic`, `openai`) are optional extras, imported only inside `llm/` (§2.2) —
installing one provider must not require the other.

**Model:** reached only through the provider-agnostic port in §2.2, so the model is a
config value rather than a code dependency. The default and reference implementation is
`claude-opus-5` with adaptive thinking — multi-hop retrieval decisions are exactly what
thinking helps with. Reference cost: $5 / $25 per million input / output tokens. We will
measure real per-question cost during the eval phase (§5) and only then decide whether a
cheaper model or a lower effort setting holds quality — a measured decision, not an
upfront one.

**Agent loop:** we own the request → tool-execute → loop cycle ourselves, in
`agent.py`, written against the provider-agnostic port in §2.2. An earlier draft
delegated this to the Anthropic SDK's `tool_runner`; that helper is Anthropic-specific,
so keeping the model swappable means writing the loop ourselves. It is roughly thirty
lines — a `while` over `stop_reason == "tool_use"` — and it stays identical across
providers.

```
question
   ↓
agent loop (LLMClient + tools)
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

---

## 2.1 The Wikipedia client boundary

All Wikipedia access lives behind one small client in `wikipedia.py`. **Nothing above
that module knows HTTP exists.** The tool layer calls typed Python methods and gets
back typed results or typed exceptions — never a `Response`, never a status code,
never a raw JSON dict. Swapping to a different MediaWiki endpoint, or to a recorded
fixture in tests, should touch exactly one file.

This boundary is what makes the eval harness (§5) cheap and deterministic: tests
substitute a fake client without a single mocked HTTP call.

The client's obligations follow the
[MediaWiki API etiquette guidance](https://www.mediawiki.org/wiki/API:Etiquette) and
[api.php](https://en.wikipedia.org/w/api.php).

### Descriptive User-Agent — mandatory
The documented format is
`clientname/version (contact information e.g. username, email) framework/version`.
Wikimedia blocks non-compliant clients by IP **without notice**, so this is a hard
requirement, not a nicety:
- Set centrally in the client constructor; no request path can bypass it.
- Contact info comes from config, and startup **fails loudly** if it is unset — a
  placeholder UA is the same violation as no UA.
- A unit test asserts the header is present and well-formed on every request.

### Throttling — serial requests, not parallel
The guidance is explicit: *"Making your requests in series rather than in parallel, by
waiting for one request to finish before sending a new request, should result in a safe
request rate."*

- **The client serializes all outbound requests.** No connection-pool concurrency, no
  `asyncio.gather` over fetches. This corrects the earlier "concurrent fetches" plan.
- A minimum interval between requests, configurable, defaulting to a conservative value.
- **Batch instead of parallelize.** Multivalue parameters (`titles=A|B|C`) fetch several
  pages in one call — the sanctioned way to go faster. Cap at 50 titles per request,
  the documented limit for ordinary clients.
- On `ratelimited` / 429 / 5xx: **exponential backoff with jitter**, a bounded retry
  count, then a structured error. Honour `Retry-After` when present.
- `maxlag` is deliberately **not** set by default. The guidance scopes it to
  non-interactive tasks; ours has a user waiting. The client accepts it as an option so
  batch eval runs can set `maxlag=5` and be a good citizen under load.

### Bounded search limits
No unbounded result sets, ever:
- `search_wikipedia` takes `limit`, defaulting to 5, hard-capped at 20. The API permits
  500, but anything past the first handful is context-window spend with no answer value.
- Article fetches are section-scoped by default; whole-article retrieval is opt-in and
  size-capped, truncating at a documented character budget rather than returning
  something that overflows the context window.
- The cap is enforced *in the client*, so no prompt-injected tool argument can raise it.

### Explicit timeouts
- Explicit connect and read timeouts on every request — no library default, no
  unbounded wait. A hung request must surface as a `WikipediaTimeout` the agent can
  report, not a stalled session.
- A total per-question retrieval deadline, so backoff and retries cannot compound into
  an unbounded wait.

### Structured errors
The client raises a typed exception hierarchy; it never returns `None` for failure and
never leaks an `httpx` exception upward:

```
WikipediaError
├── PageNotFound(title)
├── DisambiguationError(title, options)   # carries candidates, so the agent can retry
├── WikipediaTimeout(url, elapsed)
├── RateLimited(retry_after)
└── WikipediaAPIError(code, info)         # from MediaWiki-API-Error / the error block
```

MediaWiki signals errors via the `MediaWiki-API-Error` header and an error code in the
body — the client parses both into `WikipediaAPIError` rather than inspecting status
codes alone. `DisambiguationError` carrying its options is what lets the agent recover
in-loop instead of dead-ending.

### Response caching
Keyed on endpoint + normalized params, TTL-bounded (articles change). Cuts latency and
API load, and makes eval runs reproducible and cheap.

---

## 2.2 The model provider boundary

Same principle as §2.1, applied to the model: all LLM access sits behind one narrow
interface in `llm/`, so switching between Anthropic, OpenAI, or a local model is a
config change, not a rewrite. `agent.py` imports the port, never a vendor SDK.

### The port

One method. Everything else is data:

```python
class LLMClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn: ...

    @property
    def capabilities(self) -> Capabilities: ...
```

With provider-neutral types we own:

| Type | Carries |
|---|---|
| `Message` | `role` + ordered content blocks (`TextBlock`, `ToolCall`, `ToolResult`) |
| `ToolSpec` | name, description, JSON-Schema parameters |
| `ToolCall` | `id`, `name`, parsed `arguments` dict |
| `AssistantTurn` | text, `tool_calls`, `stop_reason`, `Usage` |
| `Usage` | input / output tokens, and **cost in dollars**, computed per-provider |
| `Capabilities` | feature flags — see below |

`stop_reason` normalizes to a closed set: `end_turn`, `tool_use`, `max_tokens`,
`refusal`. Adapters map their vendor's vocabulary onto it, so the loop branches on our
enum and never on a vendor string.

### Adapters

`llm/anthropic.py`, `llm/openai.py`, and `llm/fake.py`. Each one is responsible for all
translation in both directions, and vendor types never cross the boundary:

- **Anthropic** — `content` blocks ↔ our blocks; `tool_use` / `tool_result` blocks;
  `tools[].input_schema`. Sets adaptive thinking and prompt caching where configured.
- **OpenAI** — `tool_calls` with **JSON-string** `arguments` (ours are parsed dicts, so
  the adapter parses on the way in and serializes on the way out); tool results as
  separate `role: "tool"` messages rather than content blocks; `finish_reason` mapped to
  our `stop_reason`.
- **Fake** — a scripted client returning canned turns. This is what makes the agent loop
  testable with no network and no spend (§5, Layer 1).

Each adapter owns its own retry, timeout, and typed-error handling, mapping vendor
exceptions to an `LLMError` hierarchy — the same discipline as §2.1.

### Capabilities, not lowest-common-denominator

The real risk in a multi-provider abstraction is flattening everything to what all
providers share, giving up the good parts of each. So the port does not pretend
providers are identical — it **declares differences and lets callers adapt**:

```python
@dataclass(frozen=True)
class Capabilities:
    extended_thinking: bool
    prompt_caching: bool
    parallel_tool_calls: bool
    max_context_tokens: int
```

Provider-specific tuning (Anthropic's adaptive thinking and effort, prompt-cache
breakpoints, OpenAI's equivalents) lives in **adapter-level config**, not in the port's
signature. An adapter applies what its provider supports and ignores the rest. The agent
reads `capabilities` when behaviour genuinely must differ — e.g. sizing retrieval to
`max_context_tokens` — and otherwise stays oblivious.

### What this costs

Worth stating plainly, since it is a real trade:

- **We write and maintain the agent loop ourselves**, rather than using
  `client.beta.messages.tool_runner`. Small and stable, but ours.
- **Provider-specific features need adapter plumbing** to reach the agent. Anything not
  in `Capabilities` is invisible above the boundary.
- **Every adapter needs its own conformance testing**, or "swappable" is a claim rather
  than a fact — hence the shared suite below.

The benefit that justifies it: model choice becomes a measured decision (§5) instead of
an architectural commitment, and the agent loop becomes testable with zero spend.

### Validating the abstraction

- **A shared conformance suite** runs against every adapter: same tool-call round-trip,
  same error mapping, same `stop_reason` normalization. An adapter is "done" when it
  passes, not when it returns a string.
- **Cross-provider evals.** The §5 harness takes the provider as a parameter, so the
  same graded question set scores Claude and GPT side by side, with cost and latency per
  question. This is the point of the abstraction: a model decision backed by our own
  numbers on our own task.

---

## 2.3 Guiding principles

Applies to every phase in §4. Where a principle and a deadline conflict, the principle
wins and the scope shrinks.

1. **Ground everything, invent nothing.** Every factual claim traces to retrieved text.
   No answer from model priors, however confident. "I don't know" is a correct answer
   and is graded as one (§5).
2. **A fabricated citation is the worst failure.** It is worse than a wrong answer,
   because it looks trustworthy. Hence citations are verified programmatically and held
   to a stricter bar than correctness.
3. **One boundary per concern.** HTTP lives in `wikipedia.py`, model calls in `llm/`,
   prompts in `agent.py`, schemas in `tools.py`. A change of API shape must not reach
   the agent, and a change of prompt must not reach the client.
4. **Be a good API citizen.** Wikipedia is donated infrastructure. Serial requests,
   honest UA, batching over hammering. When guidance and convenience conflict, follow
   the guidance — and when this plan contradicts upstream guidance, upstream wins and
   the plan gets corrected.
5. **Bound everything.** Result counts, article sizes, retries, timeouts, retrieval
   calls per question. Every loop has a ceiling and every wait has a deadline.
6. **Fail loudly, degrade honestly.** Config errors crash at startup, not mid-question.
   Retrieval failures reach the user as "I couldn't retrieve this", never as silence or
   an unsourced guess.
7. **Typed at the seams.** Typed arguments, typed returns, typed exceptions across every
   module boundary, checked in CI.
8. **Test without the network or the model.** Layers 1 and 2 (§5) must stay runnable
   offline and free. Only Layer 3 spends money, and it is opt-in.
9. **Measure before optimizing.** Model choice, effort level, and cost decisions come
   from eval numbers, not intuition.
10. **Tool arguments are untrusted.** The model chooses them and retrieved content can
    influence that choice. The client validates and clamps every argument; limits are
    enforced server-side of the boundary, never by prompt instruction alone.
11. **No vendor SDK above the port.** `import anthropic` and `import openai` appear only
    inside `llm/`, enforced by a lint rule in CI — not by good intentions. The model is a
    config value, so provider choice stays a measured decision (§5) rather than an
    architectural commitment. Where providers genuinely differ, declare it in
    `Capabilities` rather than flattening to the lowest common denominator.

---

## 3. Repository layout

```
wikimedia-agent/
├── project-plan.md
├── README.md
├── pyproject.toml
├── src/wikimedia_agent/
│   ├── agent.py           # provider-agnostic agent loop, system prompt, tool wiring
│   ├── tools.py           # tool specs + handlers (no vendor types)
│   ├── llm/               # the §2.2 model boundary
│   │   ├── port.py        #   Protocol + Message/ToolSpec/AssistantTurn/Capabilities
│   │   ├── anthropic.py   #   adapter (default)
│   │   ├── openai.py      #   adapter
│   │   └── fake.py        #   scripted adapter for tests
│   ├── wikipedia.py       # the §2.1 API client: UA, throttle, timeouts, typed errors
│   ├── citations.py       # citation extraction + formatting
│   └── cli.py             # entry point
├── tests/
│   ├── unit/              # mocked API, no network, no model calls
│   ├── conformance/       # one suite, run against every LLM adapter
│   └── integration/       # real API, recorded fixtures
└── evals/
    ├── dataset.jsonl      # graded question set
    └── run_eval.py
```

---

## 4. Build phases

Each phase ends with a commit pushed to `main`. Phases 1–4 are independently testable
without spending a cent on model calls — the fake adapter (§2.2) means even the agent
loop is exercised for free.

| # | Phase | Deliverable | Done when |
|---|---|---|---|
| 0 | Plan & scaffold | This document, `pyproject.toml`, CI skeleton | Plan committed; `pytest` runs green on an empty suite |
| 1 | Wikipedia client | `wikipedia.py` — the §2.1 boundary: UA, serial throttle, bounded limits, timeouts, typed errors, caching | Unit tests pass against mocked responses; integration tests pass against the live API; no HTTP type escapes the module |
| 2 | Tool layer | `tools.py` — the three tools with typed schemas, calling the client only | Each tool callable standalone; `PageNotFound` / `DisambiguationError` / timeouts map to useful tool results |
| 3 | Model boundary | `llm/` — port, Anthropic adapter, fake adapter | Conformance suite passes for both; no vendor import outside `llm/` |
| 4 | Agent loop | `agent.py` + system prompt + citation formatting | Answers a single-hop question end to end with a correct citation, driven by the fake adapter in tests |
| 5 | Multi-hop & robustness | Iterative retrieval, retrieval-failure paths, token budget | Answers a two-hop question; refuses cleanly when Wikipedia lacks the answer |
| 6 | Evaluation harness | `evals/` — dataset and runner (§5) | Full suite runs, emits a scored report, per-question cost recorded |
| 7 | CLI & docs | `cli.py`, README with setup and examples | A new user can install and ask a question from the README alone |
| 8 | Second provider | `llm/openai.py` | Passes the same conformance suite; cross-provider eval numbers reported (§5) |

---

## 5. Validation

Three layers, cheapest first. The eval set is built before we start tuning prompts,
so we are never tuning against a moving target.

### Layer 1 — Unit tests (no network, no model)
Run on every commit, fast.
- Wikipedia client: URL construction, UA header present and well-formed on every
  request, startup failure on unset contact info, serial-request enforcement and the
  minimum interval, `limit` clamping at the cap, explicit timeouts set, backoff on
  429/5xx, cache hit/miss, section extraction.
- Error mapping: each MediaWiki error shape produces the right typed exception, and no
  `httpx` exception escapes the client.
- Tool functions against recorded fixtures: normal article, disambiguation page,
  missing title, redirect, very long article.
- Citation formatting and parsing.

### Layer 1.5 — Adapter conformance (no network, no model)
One suite, parameterized over every adapter, so "swappable" is a tested fact rather than
a claim. Each adapter must demonstrate: a tool-call round-trip survives translation in
both directions; `stop_reason` normalizes to our closed set; vendor errors map to the
`LLMError` hierarchy; `Usage` reports tokens and dollar cost; and no vendor type escapes
the boundary. Runs against recorded vendor payloads — free, offline, every commit.

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

**Cross-provider runs.** The harness takes the provider as a parameter, so the same
graded set scores each configured model side by side on correctness, citation validity,
cost, and latency. This is the payoff of §2.2: the model decision comes from our numbers
on our task, and re-running it after any provider releases a new model is one flag, not
a migration.

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
| Wikimedia IP-blocks us for non-compliant access | Mandatory descriptive UA that startup enforces, strictly serial requests, batching, backoff, caching (§2.1) |
| Long articles exhausting the context window | Section-scoped fetching; summary-first disambiguation |
| Wikipedia content itself being wrong or vandalised | Out of our control — we ground and cite, so the user can check the source. Document this limitation in the README |
| The abstraction leaks — an adapter quietly behaves differently | Shared conformance suite (§5, Layer 1.5) plus a CI lint rule banning vendor imports above `llm/` |
| The abstraction flattens away what makes a provider good | Provider-specific tuning lives in adapter config, and real differences are declared in `Capabilities` rather than hidden |
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
