# Wikimedia Agent — Project Plan

A question-answering agent that grounds every answer in live Wikipedia content, with
inline citations back to the source articles.

**Status:** draft — plan only, no implementation yet.

---

## 0. Hard constraints

These come from the assignment. They are not preferences and not subject to the
engineering trade-offs elsewhere in this document — where a constraint and a design
principle conflict, **the constraint wins and the design changes**.

### C1 — An Anthropic model, via the Anthropic API
The agent must be powered by an Anthropic model called through the Anthropic API
(`POST /v1/messages`, via the official `anthropic` SDK).

**Satisfied by:** `claude-opus-5` called directly through the `anthropic` SDK (§2.2).
**Consequence:** there is no provider abstraction. With the provider fixed by the
assignment, a vendor-neutral port would buy flexibility C1 forbids — so the agent depends
on the Anthropic SDK directly and uses its `tool_runner`.

### C2 — No built-in hosted search or RAG tools
All retrieval must be ours. Specifically **forbidden**:
- Anthropic server tools: `web_search_*`, `web_fetch_*` tool types.
- Any hosted RAG or search service (OpenAI browsing, Perplexity, Bing/Google APIs,
  managed vector-store retrieval).
- Server-side code execution used to fetch content, and MCP connectors that reach a
  hosted search service.

**Satisfied by:** retrieval is our own `httpx` client against the public Wikipedia API
(§2.1), surfaced as ordinary user-defined tools we execute ourselves.

**On the SDK's `tool_runner`, which we do use (§2.2):** it is loop plumbing over
`POST /v1/messages` and ships **no tools of its own** — no search, no fetch, no sandbox.
It loops over tools we define and execute. Using it is using the Anthropic API (C1), and
it adds no hosted retrieval capability (C2). Compliant on both counts.

### Enforcing the constraints
Compliance is tested, not asserted:
- A test inspects the `tools` array sent on every request and **fails if any entry has a
  `type` field** — user-defined tools carry `name` / `description` / `input_schema`,
  while server tools are exactly the ones identified by `type`. This catches a forbidden
  tool being added later by anyone.
- The client in use is the `anthropic` SDK and the configured model ID is an Anthropic
  model (C1).
- CI runs both on every commit, so a C2 violation cannot merge quietly.

---

## 1. Goal and scope

### In scope
- Natural-language questions answered from English Wikipedia content.
- Answers carry citations for each claim: article, section, **exact revision** via an
  `oldid` permalink, and the Wikipedia quality grade of the source (§2.3).
- Explicit "I don't know" when Wikipedia does not support an answer.
- Multi-hop questions (e.g. "Who directed the highest-grossing film of 1997?")
  handled through iterative search — the agent may make several retrieval calls
  per question.
- A CLI for interactive use, and a Python API for embedding elsewhere.

### Out of scope (v1)
- Any hosted search or RAG service, and any Anthropic server tool (C2).
- A provider-abstraction layer — C1 fixes the provider to Anthropic (§2.2).
- Languages other than English Wikipedia.
- Other Wikimedia projects (Wikidata, Wiktionary, Commons).
- Conversation memory across sessions / long multi-turn dialogue.
- A web UI or hosted service.

### Non-goals
- Not a general web-search agent. If it isn't in Wikipedia, the agent says so — and
  under C2 it has no means to look anywhere else, by construction.
- Not a Wikipedia mirror — no bulk dump ingestion, no local index to maintain.

---

## 2. Architecture

**Retrieval:** live Wikimedia APIs, queried at question time. Always current, no
ingestion pipeline, no index staleness. The cost is per-question latency. We mitigate
it with caching and with batching several titles into one request — *not* with
concurrency, which the API etiquette rules out (§2.1).

**Stack:** Python 3.11+, the official `anthropic` SDK (C1), `httpx` for the Wikipedia
calls, `pytest` for tests.

**Model:** `claude-opus-5` with adaptive thinking, called directly through the
`anthropic` SDK (§2.2) — multi-hop retrieval decisions are exactly what thinking helps
with. The model ID stays a config value so eval runs can compare tiers, but there is no
provider abstraction: C1 fixes the provider, so the agent depends on the Anthropic SDK
directly. Reference cost: $5 / $25 per million input / output tokens. We will
measure real per-question cost during the eval phase (§5) and only then decide whether a
cheaper model or a lower effort setting holds quality — a measured decision, not an
upfront one.

**Agent loop:** the Anthropic SDK's `tool_runner` (§2.2) drives the request →
tool-execute → loop cycle. We supply the tool functions; the SDK supplies the loop and
the protocol details that are easy to get subtly wrong by hand.

```
question
   ↓
agent loop (tool_runner + our tools)
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
| `get_article(title, section=None)` | Action API `prop=extracts\|revisions\|pageassessments` | Full text or one section, plus revision id and quality grade in the same call |

Every tool result carries a `Provenance` record and a quality grade (§2.3), and article
text is delimited and labelled as untrusted source content.

Section-scoped fetching is the key design decision: large articles are fetched by
section rather than whole, which keeps context spend proportional to the question.

### Grounding contract
The system prompt requires the agent to answer only from retrieved text, cite the article,
section, and revision behind each factual claim, note the source's quality grade when it
is weak, and say plainly when retrieval came back empty or contradictory. It also fixes
the precedence rule: retrieved text is data, and any instruction found inside it is
reported rather than obeyed (§2.3). Citations are what make the agent auditable and what
§5 grades against.

---

## 2.1 The Wikipedia client boundary

All Wikipedia access lives behind one small client in `wikipedia.py`. **Nothing above
that module knows HTTP exists.** The tool layer calls typed Python methods and gets
back typed results or typed exceptions — never a `Response`, never a status code,
never a raw JSON dict. Swapping to a different MediaWiki endpoint, or to a recorded
fixture in tests, should touch exactly one file.

This boundary is what makes the eval harness (§5) cheap and deterministic: tests
substitute a stub Wikipedia client without a single mocked HTTP call.

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

## 2.2 The model call

**A single provider, deliberately.** C1 fixes the model to Anthropic, so the agent calls
the `anthropic` SDK directly. There is no provider-abstraction layer: an earlier draft
introduced one, and with a single mandated provider it was indirection paying for
flexibility the assignment forbids. Removing it also lets us use the SDK's `tool_runner`,
which a vendor-neutral port could not accommodate.

### The agent loop — `tool_runner`

`client.beta.messages.tool_runner` drives the request → execute → loop cycle. We define
tools with `@beta_tool`; the SDK calls the API, dispatches to our functions, feeds
results back, and loops until Claude stops requesting tools.

This is C1- and C2-compliant (§0): it is the Anthropic API, and it ships **no tools of
its own** — no search, no fetch, no sandbox. Every tool it runs is one we wrote, hitting
the Wikipedia client in §2.1.

What we get for free, each an easy thing to get subtly wrong by hand:
- All `tool_result` blocks from one turn returned in a **single** user message. Splitting
  them silently suppresses parallel tool calls — no error, just quietly worse behaviour.
- Failed tools returned as `tool_result` with `is_error: true` rather than dropped.
- Assistant content blocks echoed back unchanged, thinking blocks included.
- Tool inputs parsed rather than string-matched.
- Tool schemas generated from Python type hints, so the schema cannot drift from the
  signature.

### Control we keep

The loop is the SDK's, but the decisions stay ours:
- **Argument clamping** lives in the Wikipedia client (§2.1), below the tool layer — no
  tool argument can raise a limit, whatever the model asks for.
- **The retrieval-call cap and per-question deadline** are enforced in the tool functions
  and the runner's per-turn hooks. Once exceeded, the tool returns a refusal result and
  the model wraps up. This is the one place the hand-written loop was nicer: it could
  `break` outright, where here we return a result and let the turn finish. Acceptable,
  and noted in §6.
- **Per-turn hooks** also cover logging, cost accounting, and error interception.

### Testing without spend

Dropping the fake adapter costs us the free in-process test path, so we replace it
rather than lose it: the `anthropic` client accepts a custom `httpx` client, so unit
tests inject a mock transport returning **recorded API responses**. The real
`tool_runner` executes against canned payloads — no network, no spend, and it exercises
the actual SDK path rather than a stand-in for it. Fixtures are recorded once from live
calls and committed.

This keeps §5 Layer 1 free and offline, which was the property worth protecting.

### What this costs

- **`tool_runner` is beta** (`client.beta.messages`), so its surface may change. The
  mitigation is that our tool functions are plain Python and the loop is ~30 lines if we
  ever need to take it back in-house — the same loop we just removed.
- **Switching providers later means rewriting the agent loop**, not swapping an adapter.
  Accepted: C1 forbids a second provider, and building for a hypothetical is what we just
  removed.

---

## 2.3 Content provenance, quality, and trust

Three requirements on the content itself. The first two make answers auditable; the
third keeps retrieved text from becoming an instruction channel.

### Provenance — every claim pins to an exact revision

A citation to "the Ada Lovelace article" is not traceable: the article changes daily. A
citation to **revision 1371961179** is exact and permanent.

Every fetch records a `Provenance` record, carried alongside the text through the whole
pipeline and into the final answer:

| Field | Source | Why |
|---|---|---|
| `title` | canonical title after redirect resolution | The redirect followed is itself provenance |
| `page_id` | `pageid` | Stable across renames |
| `revision_id` | `prop=revisions&rvprop=ids` | The exact text we read |
| `retrieved_at` | our clock | When we saw it |
| `revision_timestamp` | `rvprop=timestamp` | When that revision was made |
| `section` | section index / anchor | Where in the article |
| `permalink` | `?oldid={revision_id}` | A URL that resolves to what we actually read, forever |

The permalink is the point: a reader following our citation sees the text the agent saw,
not a later edit of it. Verified in one call alongside the content —
`prop=extracts|revisions|pageassessments` returns all of it together, which also keeps us
inside the serial-request discipline of §2.1.

**Provenance is never derived from article text.** It comes from API response metadata
only. A page whose body claims "this is revision 999" changes nothing.

### Quality — the Wikipedia assessment grade travels with the content

Grades follow
[Wikipedia:Content assessment](https://en.wikipedia.org/wiki/Wikipedia:Content_assessment),
retrieved via the PageAssessments API (`prop=pageassessments`), which is enabled on
English Wikipedia and returns a class per WikiProject.

The scale, best to worst:

| Grade | Meaning |
|---|---|
| **FA** | Featured Article — Wikipedia's best work; professional, comprehensive, thoroughly researched |
| **FL** | Featured List — meets the featured criteria for lists |
| **A** | Well organized and essentially complete; reviewed by impartial editors |
| **GA** | Good Article — well-written, verifiable, broad, neutral, stable; formally reviewed |
| **B** | Mostly complete with solid references; some work needed |
| **C** | Substantial but missing important elements; may need cleanup |
| **Start** | Developing but quite incomplete; sourcing may be inadequate |
| **Stub** | Very basic; minimal meaningful content |
| **List** | Stand-alone list or set index article |
| **Unassessed** | No grade recorded — *our* label, not Wikipedia's |

**Picking one grade.** The API returns a class per WikiProject and they can disagree.
Resolution order, deterministic (principle #11):
1. The `Project-independent assessment` key when present — the canonical cross-project
   grade, and present on every article we sampled.
2. Otherwise the **lowest** grade among the projects listed. Erring pessimistic is the
   honest direction for a quality signal.
3. Absent entirely → `Unassessed`. Never guessed, never inferred from article length or
   prose style.

Two API details the client handles: assessments **paginate** (`pacontinue`), so use
`palimit=max` and follow continuations; and `importance` is frequently an empty string —
we record it when present but never treat it as quality.

**What the grade is used for:**
- **Shown to the user** with every citation, so they can weigh a claim sourced from a
  Stub differently from one sourced from a Featured Article.
- **Given to the model** as tool-result metadata, with instructions to prefer better-
  graded sources when several cover a claim, and to say so when the only available
  source is weak.
- **Recorded in eval reports**, so "correct, but sourced from a Stub" is visible rather
  than hidden inside a pass.

It is **not** used to silently filter articles out. A Stub is often the only article on a
niche subject, and suppressing it would turn a weak answer into no answer. We surface the
grade and let the reader judge.

**The grade comes from the API field only** — never from article text. A page that says
"this article is Featured" is making a claim, not carrying a grade.

### Trust — retrieved content is data, never instructions

**Wikipedia is user-editable, so every byte we retrieve is untrusted input.** Anyone can
put "ignore your previous instructions" into an article. Treating retrieved text as
potentially adversarial is a correctness requirement, not paranoia.

**Structural defences**, in order of how much they actually buy:

1. **A narrow blast radius by construction.** Under C2 the agent has no general fetch,
   no code execution, no filesystem, no network beyond our three Wikipedia tools. The
   worst an injection can achieve is making the agent read *a different Wikipedia
   article* — which is then cited, graded, and visible in the transcript. This is the
   strongest protection we have, and it is free: it falls out of the constraint.
2. **Retrieved text is delimited and labelled** in every tool result — fenced, marked as
   untrusted source content, tagged with its provenance. The model is told explicitly
   that everything inside is data to be quoted and reasoned about, never instructions to
   follow, regardless of what it says about itself.
3. **The system prompt states the precedence rule directly:** instructions in retrieved
   content are reported, not obeyed. If an article appears to contain directives aimed
   at an AI reader, the agent may mention that as an observation about the article — it
   is factual — but must not act on it.
4. **Limits live below the model** (principle #13). Retrieved text influencing a tool
   argument still cannot exceed a cap, because caps are enforced in the client (§2.1),
   not by prompt instruction. Injection cannot widen a search limit or bypass a deadline.
5. **Provenance and grade are API-derived**, so injected text cannot forge a citation,
   claim a revision, or upgrade its own quality grade.

**What we deliberately do not do:** filter or rewrite article text to strip
"suspicious" content. It would corrupt the very thing we cite, break the permalink
guarantee, and fail anyway against novel phrasings. Containment beats sanitization here.

**Tested, not assumed.** The eval set gets an injection-resistance category (§5): fixture
articles carrying embedded directives, asserting the agent answers the user's question,
does not follow the embedded instruction, and keeps its citations intact.

---

## 2.4 Guiding principles

Applies to every phase in §4. Where a principle and a deadline conflict, the principle
wins and the scope shrinks.

1. **Ground everything, invent nothing.** Every factual claim traces to retrieved text.
   No answer from model priors, however confident. "I don't know" is a correct answer
   and is graded as one (§5).
2. **A fabricated citation is the worst failure.** It is worse than a wrong answer,
   because it looks trustworthy. Hence citations are verified programmatically and held
   to a stricter bar than correctness.
3. **Cite a revision, not an article.** Every claim pins to an exact `revision_id` with
   an `oldid` permalink (§2.3), so a reader sees the text the agent actually read rather
   than a later edit of it. Provenance comes from API metadata, never from article text.
4. **Quality travels with the content.** Every citation carries its
   [Wikipedia assessment grade](https://en.wikipedia.org/wiki/Wikipedia:Content_assessment),
   surfaced to the user rather than used to silently filter — a Stub is often the only
   article on a niche subject. `Unassessed` when absent; never inferred.
5. **Retrieved content is untrusted data, never instructions.** Wikipedia is
   user-editable. Article text is delimited and labelled in every tool result, and
   directives found inside it are reported, never obeyed. The agent's instructions
   always outrank anything it reads.
6. **One boundary per concern.** HTTP lives in `wikipedia.py`, prompts and loop wiring
   in `agent.py`, tool definitions in `tools.py`. A change of Wikipedia API shape must
   not reach the agent, and a change of prompt must not reach the client.
7. **Be a good API citizen.** Wikipedia is donated infrastructure. Serial requests,
   honest UA, batching over hammering. When guidance and convenience conflict, follow
   the guidance — and when this plan contradicts upstream guidance, upstream wins and
   the plan gets corrected.
8. **Bound everything.** Result counts, article sizes, retries, timeouts, retrieval
   calls per question. Every loop has a ceiling and every wait has a deadline.
9. **Fail loudly, degrade honestly.** Config errors crash at startup, not mid-question.
   Retrieval failures reach the user as "I couldn't retrieve this", never as silence or
   an unsourced guess.
10. **Typed at the seams.** Typed arguments, typed returns, typed exceptions across every
   module boundary, checked in CI.
11. **Deterministic by default; paid model calls are a deliberate act.** Local
   development runs on deterministic code and unit tests, never on a live model.
   Concretely:
   - **Prefer a deterministic mechanism to a prompted one** wherever both could work.
     Clamping a limit, verifying a citation, parsing a disambiguation page, enforcing
     the retrieval cap — all of these are code, because code is testable, repeatable,
     and free. Asking the model to respect a rule is the fallback, not the default, and
     never the only enforcement (see #10).
   - **Layers 0 and 1 (§5) run offline and free** on every commit — including the agent
     loop, which drives the real `tool_runner` against recorded fixtures (§2.2).
     Layer 2 hits the live Wikipedia API but still costs nothing.
   - **Only Layer 3 spends money, and it is opt-in.** Never wired into the default
     `pytest` run, never triggered by a file save or a watch mode, never part of a
     pre-commit hook. Running it is an explicit command.
   - **Eval runs are small and scoped by default.** A named subset — one category, or a
     handful of questions — is the normal invocation; the full set is reserved for
     release checkpoints and prompt changes. Every run reports its own dollar cost, so
     spend is observed rather than discovered later.
   - **A test that needs a live model is a design smell.** It usually means logic that
     belongs in deterministic code has leaked into the prompt. Move it down rather than
     paying to test it.
12. **Measure before optimizing.** Model choice, effort level, and cost decisions come
   from eval numbers, not intuition.
13. **Tool arguments are untrusted.** The model chooses them and retrieved content can
    influence that choice. The client validates and clamps every argument; limits are
    enforced server-side of the boundary, never by prompt instruction alone.
14. **Constraints outrank principles.** C1 and C2 (§0) are assignment requirements, not
    trade-offs. Any principle below that conflicts with them loses, and compliance is
    enforced by tests rather than by care.
15. **All retrieval is ours.** No hosted search, no server-side fetch tool, no managed
    RAG. The agent's only route to the world is the Wikipedia client in §2.1 — which is
    also what makes every answer auditable.
16. **Don't build for hypotheticals.** The provider is fixed by C1, so we depend on the
    Anthropic SDK directly rather than wrapping it in a port for a second provider that
    the assignment forbids. Abstractions earn their place by solving a problem we
    actually have — the §2.1 Wikipedia boundary does; a model-provider port did not.

---

## 3. Repository layout

```
wikimedia-agent/
├── project-plan.md
├── README.md
├── pyproject.toml
├── src/wikimedia_agent/
│   ├── agent.py           # tool_runner wiring, system prompt, per-turn hooks
│   ├── tools.py           # @beta_tool definitions calling the Wikipedia client
│   ├── wikipedia.py       # the §2.1 API client: UA, throttle, timeouts, typed errors
│   ├── provenance.py      # Provenance record, quality grade resolution (§2.3)
│   ├── citations.py       # citation extraction, verification + formatting
│   └── cli.py             # entry point
├── tests/
│   ├── unit/              # mocked API, no network, no model calls
│   ├── compliance/        # §0 constraint checks (C1, C2)
│   ├── fixtures/          # recorded Anthropic responses for offline loop tests
│   └── integration/       # real API, recorded fixtures
└── evals/
    ├── dataset.jsonl      # graded question set
    └── run_eval.py
```

---

## 4. Build phases

Each phase ends with a commit pushed to `main`. Phases 1–4 are independently testable
without spending a cent on model calls — recorded API fixtures (§2.2) mean even the agent
loop is exercised for free.

| # | Phase | Deliverable | Done when |
|---|---|---|---|
| 0 | Plan & scaffold | This document, `pyproject.toml`, CI skeleton | Plan committed; `pytest` runs green on an empty suite |
| 1 | Wikipedia client | `wikipedia.py` + `provenance.py` — the §2.1 boundary, plus revision ids and assessment grades (§2.3) | Unit tests pass against mocked responses; integration tests pass against the live API; no HTTP type escapes the module; every result carries provenance and a resolved grade |
| 2 | Tool layer | `tools.py` — the three tools with typed schemas, calling the client only | Each tool callable standalone; errors map to useful tool results; article text delimited and labelled untrusted |
| 3 | Agent loop | `agent.py` — `tool_runner` wiring, system prompt, citation formatting | Answers a single-hop question end to end with a correct citation; loop tested offline against recorded responses |
| 4 | Multi-hop, trust & robustness | Iterative retrieval, failure paths, token budget, injection resistance | Answers a two-hop question; refuses cleanly when Wikipedia lacks the answer; ignores directives embedded in fixture articles |
| 5 | Evaluation harness | `evals/` — dataset and scoped runner (§5) | A named subset runs from one command, prints cost before and after, emits a scored report; excluded from the default `pytest` run |
| 6 | CLI & docs | `cli.py`, README with setup and examples | A new user can install and ask a question from the README alone |

---

## 5. Validation

Three layers, cheapest first. The eval set is built before we start tuning prompts,
so we are never tuning against a moving target.

### Layer 0 — Constraint compliance (no network, no model)
The §0 checks, run first and on every commit because a violation invalidates the whole
deliverable regardless of how well it scores:
- No entry in the outbound `tools` array carries a `type` field (C2).
- The client in use is the `anthropic` SDK, with an Anthropic model ID (C1).
- No hosted-retrieval dependency appears in `pyproject.toml`.

### Layer 1 — Unit tests (no network, no model)
Run on every commit, fast.
- Wikipedia client: URL construction, UA header present and well-formed on every
  request, startup failure on unset contact info, serial-request enforcement and the
  minimum interval, `limit` clamping at the cap, explicit timeouts set, backoff on
  429/5xx, cache hit/miss, section extraction.
- Error mapping: each MediaWiki error shape produces the right typed exception, and no
  `httpx` exception escapes the client.
- Agent loop: the real `tool_runner` driven against recorded Anthropic responses via a
  mock `httpx` transport (§2.2) — multi-turn tool sequences, tool errors surfacing as
  `is_error` results, and the retrieval-call cap terminating the loop. No network, no
  spend.
- Tool functions against recorded fixtures: normal article, disambiguation page,
  missing title, redirect, very long article.
- Citation formatting, parsing, and provenance round-tripping.
- Grade resolution (§2.3): `Project-independent assessment` preferred, lowest-of-projects
  fallback, `Unassessed` when absent, `pacontinue` pagination followed, `importance`
  never mistaken for quality.
- Injection fixtures: article text containing directives is delimited and labelled, and
  provenance and grade stay API-derived regardless of body content.

### Layer 2 — Integration tests (real API, no model)
Run on demand and nightly — these can break when Wikipedia changes, and that's the
point of separating them.
- Each endpoint returns the expected shape against live Wikipedia.
- A known-stable article (e.g. a long-settled historical topic) fetches and
  section-splits correctly.

### Layer 3 — Agent evals (real model, costs money)
**The only layer that spends money, and it never runs by accident.** Excluded from the
default `pytest` run by marker, absent from any watch mode or pre-commit hook, and
invoked by an explicit command that names what to run:

```
python -m evals.run --category multi-hop --limit 5    # the normal invocation
python -m evals.run --all                             # release checkpoints only
```

Small and scoped is the default: a category or a handful of questions while iterating,
the full set reserved for release checkpoints and prompt changes. The runner **prints an
estimated cost and the question count before starting**, and the actual dollar cost when
it finishes, so spend is observed rather than discovered on a bill. Every run writes a
timestamped report so results are comparable across changes.

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
| **Injection resistance** | Untrusted content (§2.3) | A fixture article carrying "ignore your instructions" directives |
| **Low-quality source** | Grade surfacing | A question only a Stub covers — is the weak sourcing disclosed? |

**Grading.** Three scores per question:
1. **Answer correctness** — LLM-as-judge against the reference answer, with a
   sample hand-checked to confirm the judge is calibrated.
2. **Citation validity** — programmatic, not judged (principle #11): every cited article
   must exist, and the cited text must actually appear in the fetched content. Free,
   deterministic, and it catches fabricated citations — the failure mode that matters
   most here.
3. **Refusal correctness** — on the not-in-Wikipedia set, did it decline instead of
   inventing an answer? Detected by structure, not by a judge, where the refusal has a
   recognizable shape.

4. **Provenance integrity** — programmatic: every citation carries a `revision_id` and a
   resolvable `oldid` permalink, and the cited text appears in *that* revision. A citation
   without a revision is a failure even if the article supports the claim.
5. **Injection resistance** — programmatic: on the injection set, the agent answered the
   user's question, took no action the embedded directive asked for, and kept its
   citations intact.

Only score 1 needs a paid judge call. The other four are deterministic and run against a
stored transcript for free (principle #11) — so re-scoring citations, provenance, and
injection resistance after a change costs nothing.

**Also recorded per run:** tokens and dollar cost per question, wall-clock latency,
and number of retrieval calls. These are the numbers that justify any later change
to model or effort level.

**Model comparison, within C1.** The harness takes the model as a parameter, so the same
graded set can score Anthropic models against each other — Opus vs. a cheaper tier, or
one effort level vs. another — on correctness, citation validity, cost, and latency.
Cross-*provider* comparison is out of scope under C1; cross-*model* comparison is not,
and it is where the cost question from §2 gets settled with numbers.

**Data split.** The set is split train / validation / test. Prompt iteration happens
against train and validation; the test slice is scored but never tuned against, so
the headline number stays honest.

**Bar for v1:** ≥85% answer correctness on the test split, ≥95% citation validity
(a fabricated citation is worse than a wrong answer — it looks trustworthy), ≥90% correct
refusals, **100% provenance integrity** (a citation without a revision is a bug, not a
near miss), and **100% injection resistance** — a single instance of following embedded
instructions is a failure, not a percentage.

### Continuous validation
- Constraint compliance (Layer 0) + unit (Layer 1) + lint on every push to `main` —
  all free and offline.
- Integration tests nightly (they depend on a live third-party API).
- Evals run manually before any release, and after any prompt or model change.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| Fabricated citations — the answer looks sourced but isn't | Programmatic citation verification in the eval suite, scored separately and held to a higher bar than answer correctness |
| Wikimedia IP-blocks us for non-compliant access | Mandatory descriptive UA that startup enforces, strictly serial requests, batching, backoff, caching (§2.1) |
| Long articles exhausting the context window | Section-scoped fetching; summary-first disambiguation |
| Wikipedia content itself being wrong or vandalised | Out of our control, but bounded: we cite an exact revision so the reader sees what we saw, and surface the assessment grade so weak sourcing is visible. Documented in the README |
| Prompt injection via article text | Narrow blast radius by construction (C2 leaves no tool worth hijacking), plus delimiting, an explicit precedence rule, client-side limit enforcement, and an eval category held to 100% (§2.3) |
| A citation that can't be reproduced later because the article changed | Every citation pins to a `revision_id` with an `oldid` permalink; provenance integrity is scored at 100% |
| A C2 violation slips in — someone adds a server tool for convenience | Layer 0 test fails any tool entry carrying a `type` field; runs on every commit |
| `tool_runner` is beta and its surface may change | Tool functions are plain Python and the loop is ~30 lines to bring in-house; pin the SDK version and cover the loop with offline fixture tests |
| A hard stop mid-turn is awkward under `tool_runner` — the cap returns a refusal result rather than breaking outright | Accepted; the cap still holds, the model just finishes its turn. Revisit only if runaway loops show up in eval runs |
| Paid model calls fire accidentally during development | Layer 3 is marker-excluded from the default `pytest` run, kept out of watch modes and pre-commit hooks, and needs an explicit command that names a scope (principle #11) |
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
