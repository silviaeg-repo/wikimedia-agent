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

**Satisfied by:** `claude-opus-5` for the agent, called directly through the `anthropic`
SDK (§2.2). The eval judge is `claude-sonnet-5` — also Anthropic via the Anthropic API, so
C1 holds on both sides.
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
- The client in use is the `anthropic` SDK, and **both** configured model IDs — agent and
  judge — are Anthropic models (C1).
- CI runs both on every commit, so a C2 violation cannot merge quietly.

---

## 1. Goal and scope

### In scope
- Natural-language questions answered from English Wikipedia content.
- Answers carry citations for each claim: the article, a link to it, its section, and
  its Wikipedia quality grade — with low-quality sources flagged inline at the claim and
  in the source list (§2.3). The exact revision is recorded internally for verification
  but not shown.
- Every article consulted is named in the response, cited or not.
- Explicit "I don't know" when Wikipedia does not support an answer.
- Multi-hop questions (e.g. "Who directed the highest-grossing film of 1997?")
  handled through iterative search — the agent may make several retrieval calls
  per question.
- A CLI for interactive use, and a Python API for embedding elsewhere.
- **Multi-turn conversation** (§2.4): follow-up questions resolve against what was already
  asked and retrieved — "Who was Ben Franklin?" then "Where was he born?" — with the
  sources and their quality grades carried forward.

### Out of scope (v1)
- Any hosted search or RAG service, and any Anthropic server tool (C2).
- A provider-abstraction layer — C1 fixes the provider to Anthropic (§2.2).
- Languages other than English Wikipedia.
- Other Wikimedia projects (Wikidata, Wiktionary, Commons).
- Memory *across* sessions — each session starts clean. Multi-turn conversation
  *within* a session is in scope (§2.4).
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

**Stack:** Python 3.9+, the official `anthropic` SDK (C1), `httpx` for the Wikipedia
calls, `pytest` for tests, with `ruff` and `mypy --strict` in CI.

*Revised in Phase 0.* The plan originally said 3.11+. The development machine has only
3.9 available and no package manager to install a newer one, and a floor we cannot run
tests against is worse than a slightly lower one — the code is 3.9-compatible and runs
unchanged on 3.11+. Phase 5 may raise the floor to 3.10+ when the `anthropic` SDK is
pinned; that is a one-line change to `requires-python`.

**Models — two, configured separately.** The agent and the eval judge are distinct
config keys, never one shared value:

| Role | Model | Why |
|---|---|---|
| **Agent** | `claude-opus-5`, adaptive thinking | Multi-hop retrieval decisions are exactly what thinking helps with |
| **Judge** (§5) | `claude-sonnet-5`, adaptive thinking, low effort | A different tier from the agent, which partially mitigates self-preference; 1M context holds a full transcript; ~2.5× cheaper |

Reference cost: Opus 5 at $5 / $25, Sonnet 5 at $2 / $10 per million input / output tokens.
Both are Anthropic models via the Anthropic API, so C1 holds for the agent and the judge
alike.

Splitting the keys is what makes model choice measurable: eval runs can vary either side
independently, and we decide whether a cheaper agent holds quality from numbers rather
than upfront (principle #15). There is no provider abstraction — C1 fixes the provider, so
the agent depends on the Anthropic SDK directly.

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
section behind each factual claim with a numbered marker the renderer can decorate
(§2.3), prefer the better-graded source when several cover a claim, note when the only support for a claim is a weak source, and say plainly when retrieval came back empty or contradictory. It also fixes
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
| `article_url` | canonical `/wiki/{title}` | **What the answer displays** — the live article |
| `permalink` | `?oldid={revision_id}` | Internal: resolves to exactly what we read, forever |

Both are recorded, and they serve different readers. The **article URL is what the answer
shows** — someone following a citation wants the live article, including any corrections
made since. The **permalink is what verification uses**: citation validity and provenance
integrity (§5) check the cited text against the exact revision we read, which is what
makes an eval result reproducible months later. Verified in one call alongside the
content —
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
Resolution order, deterministic (principle #14):
1. The `Project-independent assessment` key when present — the canonical cross-project
   grade, and present on every article we sampled.
2. Otherwise the **lowest** grade among the projects listed. Erring pessimistic is the
   honest direction for a quality signal.
3. Absent entirely → `Unassessed`. Never guessed, never inferred from article length or
   prose style.

Two API details the client handles: assessments **paginate** (`pacontinue`), so use
`palimit=max` and follow continuations; and `importance` is frequently an empty string —
we record it when present but never treat it as quality.

### Quality tiers, and what counts as poor

The ten grades collapse to three tiers, fixed in code:

| Tier | Grades | Treatment |
|---|---|---|
| **Strong** | FA, FL, A, GA | Preferred source; cited normally |
| **Adequate** | B, C | Cited normally |
| **Poor** | Start, Stub, Unassessed | Cited **with a warning** |

`List` is graded on its own merits and tiered by whatever class the API reports alongside
it. `Unassessed` counts as poor deliberately — an ungraded article is an unknown, and an
unknown should not read as an endorsement.

### Use the best available, warn on the weak

**Selection.** When several articles could support a claim, prefer the higher-graded one.
Search results carry each candidate's grade as metadata, and the system prompt instructs
the agent to prefer stronger sources. **Relevance still dominates** — a Featured Article
that does not answer the question is useless, so quality is a tie-breaker among articles
that actually cover the claim, never a reason to cite a better-graded article that
doesn't.

**No filtering.** A Stub is often the only article on a niche subject, and suppressing it
turns a weak answer into no answer. We cite it and flag it.

**Warning is the renderer's job, not the model's.** This is the load-bearing decision. If
flagging depends on the model remembering, it will sometimes be forgotten — precisely on
the long multi-source answers where it matters most. So the renderer emits every warning
**from the grade field** in the `Provenance` record (principle #14).

**Inline, at the point of the claim.** A warning that only appears in a source list at
the end is easy to read past, and on a multi-claim answer it does not say *which* claim is
weakly sourced. So the marker travels with the claim:

```
Ada Lovelace wrote what is considered the first algorithm intended for a
machine. [1] Her notes were later described as the earliest published work on
computing by Gerald J. Ford. [2 ⚠ Start-class]

Sources
  [1] Ada Lovelace — B-class
      https://en.wikipedia.org/wiki/Ada_Lovelace
  [2] Gerald J. Ford — Start-class ⚠ low-quality source
      https://en.wikipedia.org/wiki/Gerald_J._Ford

⚠ One source is rated below Wikipedia's B-class standard. Claims marked ⚠ draw on
  an article that may be incomplete or inadequately sourced.
```

**The displayed link is the article, not the revision.** Readers want the live article,
which is also where they can see later corrections. The `revision_id` stays in the
`Provenance` record — it is what citation verification and the eval harness check against
(§5), and what makes a result reproducible — but it is not rendered in the answer.

**How it stays renderer-enforced.** The model emits plain numbered markers — `[1]`, `[2]`
— as the grounding contract already requires. The renderer then **decorates** each marker
from its source's tier: Strong and Adequate render bare, Poor renders as `[n ⚠ <grade>]`.
The model never decides whether a warning appears, so it cannot forget one, and a
mis-tiered marker is a code bug caught by a unit test rather than a behaviour regression.

**Noise control.** Only the Poor tier is decorated — B-class and above stay clean, so the
marker means something when it does appear. The full explanation renders once at the foot
of the answer, not on every claim.

The model may *also* mention weak sourcing in prose — it is instructed to when the *only*
support for a claim is poor — but no flag depends on that.

**Every article used is listed.** The source list is built from the provenance records
actually retrieved during the run, not from what the model chose to mention. An article
that was read but not cited still appears, marked as consulted — so the transcript of
what informed the answer is complete.

**Recorded in eval reports**, so "correct, but sourced from a Stub" is visible rather
than hidden inside a pass.

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
4. **Limits live below the model** (principle #16). Retrieved text influencing a tool
   argument still cannot exceed a cap, because caps are enforced in the client (§2.1),
   not by prompt instruction. Injection cannot widen a search limit or bypass a deadline.
5. **Provenance and grade are API-derived**, so injected text cannot forge a citation,
   claim a revision, or upgrade its own quality grade.

**What we deliberately do not do:** filter or rewrite article text to strip
"suspicious" content. It would corrupt the very thing we cite, break the guarantee that
the recorded revision matches what we read, and fail anyway against novel phrasings. Containment beats sanitization here.

**Tested, not assumed.** The eval set gets an injection-resistance category (§5): fixture
articles carrying embedded directives, asserting the agent answers the user's question,
does not follow the embedded instruction, and keeps its citations intact.

---

## 2.4 Conversational behaviour

The agent holds a conversation, not a series of unrelated lookups. Two things make that
work: the message history, and a session-scoped record of what has been retrieved.

### Follow-ups resolve against history

"Who was Ben Franklin?" followed by "Where was he born?" must answer about Franklin. This
needs no coreference machinery of our own — the full message history goes back on every
request, so the model resolves "he" from context the same way it resolves anything else.
What it does require is that we **never silently drop earlier turns**, because a dropped
turn is a pronoun with no referent.

Three kinds of follow-up the agent must handle:

| Kind | Example after "Who was Ben Franklin?" | What it needs |
|---|---|---|
| **Pronoun / ellipsis** | "Where was he born?" | Message history |
| **Refinement** | "Say more about the kite experiment" | History + the article already fetched |
| **Pivot** | "What about Jefferson?" | History, plus recognizing the subject changed |

The failure mode to guard against is a follow-up answered from the model's own knowledge
because the answer "feels obvious" from context. **Grounding does not weaken across
turns** (principle #1): a follow-up still cites retrieved content, whether that content
came from this turn or an earlier one.

### The session article registry

Every retrieval is recorded in a session-scoped registry, keyed by page id:

```python
@dataclass
class RegisteredArticle:
    provenance: Provenance      # title, page_id, revision_id, article_url, retrieved_at
    grade: Grade                # resolved per §2.3
    tier: Tier                  # Strong | Adequate | Poor
    marker: int                 # stable citation number for this session
    sections_fetched: set[str]
    first_turn: int
```

This is what lets a later turn say "as the Ada Lovelace article noted" and have it mean
something checkable. Four properties matter:

1. **Citation markers are stable for the session.** If the Franklin article is `[1]` in
   turn one, it stays `[1]` in turn six. Renumbering per turn would make the conversation
   unreadable and break references back to earlier answers. New articles take the next
   free number.
2. **Quality travels with the article, permanently.** A Start-class source cited in turn
   one is still flagged `⚠` when referenced in turn five (§2.3). The warning is attached
   to the registry entry, so it cannot decay as the conversation grows — the renderer
   looks up the tier, it is never re-derived or remembered.
3. **Re-use beats re-fetch.** A follow-up about an already-fetched article uses the text
   already in context, and the registry's recorded revision stays the provenance. If a
   new *section* is needed, that is a fresh retrieval against the same article, recorded
   as such.
4. **The registry is the source of truth for the source list.** Each answer's sources are
   rendered from registry entries touched in that turn, so §2.3's "every article used is
   named" holds per-turn without recomputing anything.

### Bounding the context

Conversation history plus article text grows without limit, and article text is the bulk
of it. Unbounded growth ends in a context-window failure mid-conversation — the worst
possible moment (principle #11: bound everything).

The strategy, cheapest first:
- **Registry metadata is tiny and always kept.** Titles, grades, revisions, markers — a
  few hundred bytes per article. Even a long conversation keeps every entry.
- **Article *bodies* are evictable.** When the transcript approaches a configured token
  budget, the oldest article text is dropped from history while its registry entry stays.
  The agent can still cite it correctly, and can re-fetch it if needed — served from the
  §2.1 cache, so usually free.
- **A turn limit and a token budget**, both configurable, both surfaced. When the budget
  is hit the agent says so rather than silently forgetting: "I've dropped the full text of
  earlier articles to stay within context — I can re-fetch if you want more detail."

Server-side compaction is deliberately **not** used in v1: it is beta, and eviction keyed
on our own registry is simpler and more predictable. Revisit if conversations routinely
run long.

### Trust across turns

The §2.3 trust boundary applies to the whole conversation, not one turn. Injected text
retrieved in turn one stays in history and could influence turn seven — a longer window
than a single-shot agent has. The existing defences carry over unchanged (delimiting,
labelling, the precedence rule, client-side limits), and the injection eval category
(§5) includes a **multi-turn case**: inject in an early turn, assert the directive is
still not followed several turns later.

### Session boundaries

One session is one conversation. The CLI supports starting a fresh one (`/new`), which
clears both the history and the registry — including marker numbering. Nothing persists
across process restarts in v1; cross-session memory stays out of scope (§1).

---

## 2.5 Guiding principles

Applies to every phase in §4. Where a principle and a deadline conflict, the principle
wins and the scope shrinks.

1. **Ground everything, invent nothing.** Every factual claim traces to retrieved text.
   No answer from model priors, however confident. "I don't know" is a correct answer
   and is graded as one (§5).
2. **A fabricated citation is the worst failure.** It is worse than a wrong answer,
   because it looks trustworthy. Hence citations are verified programmatically and held
   to a stricter bar than correctness.
3. **Link the article; record the revision.** Every citation displays a link to the
   Wikipedia article, and every retrieval also records the exact `revision_id` behind it
   (§2.3). The link is for the reader, the revision is what verification checks against.
   Provenance comes from API metadata, never from article text.
4. **Best available source, and say so when it's weak.** Prefer the higher-graded
   article when several cover a claim, but never filter: a Stub is often the only
   article on a niche subject. Every article used is named in the response with its
   [assessment grade](https://en.wikipedia.org/wiki/Wikipedia:Content_assessment), and
   anything below B-class (Start, Stub, Unassessed) is flagged **inline at the claim**
   and in the source list — by the renderer, from the grade field, not by the model
   remembering to.
5. **A conversation, not a series of lookups.** Follow-ups resolve against history —
   "Where was he born?" answers about whoever "he" is (§2.4). Earlier turns are never
   silently dropped, because a dropped turn is a pronoun with no referent.
6. **Grounding does not weaken across turns.** A follow-up that feels obvious from
   context still cites retrieved content. Answering from the model's own knowledge
   because the conversation makes it seem safe is the same failure as #1, just harder to
   notice.
7. **Citation markers are stable for the session.** If an article is `[1]` in turn one it
   is `[1]` in turn six, and its quality flag travels with it permanently (§2.4). A
   warning that decays as the conversation grows is worse than no warning.
8. **Retrieved content is untrusted data, never instructions.** Wikipedia is
   user-editable. Article text is delimited and labelled in every tool result, and
   directives found inside it are reported, never obeyed. The agent's instructions
   always outrank anything it reads.
9. **One boundary per concern.** HTTP lives in `wikipedia.py`, prompts and loop wiring
   in `agent.py`, tool definitions in `tools.py`. A change of Wikipedia API shape must
   not reach the agent, and a change of prompt must not reach the client.
10. **Be a good API citizen.** Wikipedia is donated infrastructure. Serial requests,
   honest UA, batching over hammering. When guidance and convenience conflict, follow
   the guidance — and when this plan contradicts upstream guidance, upstream wins and
   the plan gets corrected.
11. **Bound everything.** Result counts, article sizes, retries, timeouts, retrieval
   calls per question. Every loop has a ceiling and every wait has a deadline.
12. **Fail loudly, degrade honestly.** Config errors crash at startup, not mid-question.
   Retrieval failures reach the user as "I couldn't retrieve this", never as silence or
   an unsourced guess.
13. **Typed at the seams.** Typed arguments, typed returns, typed exceptions across every
   module boundary, checked in CI.
14. **Deterministic by default; paid model calls are a deliberate act.** Local
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
15. **Measure before optimizing.** Model choice, effort level, and cost decisions come
   from eval numbers, not intuition.
16. **Tool arguments are untrusted.** The model chooses them and retrieved content can
    influence that choice. The client validates and clamps every argument; limits are
    enforced server-side of the boundary, never by prompt instruction alone.
17. **Constraints outrank principles.** C1 and C2 (§0) are assignment requirements, not
    trade-offs. Any principle below that conflicts with them loses, and compliance is
    enforced by tests rather than by care.
18. **All retrieval is ours.** No hosted search, no server-side fetch tool, no managed
    RAG. The agent's only route to the world is the Wikipedia client in §2.1 — which is
    also what makes every answer auditable.
19. **The judge is a fixed instrument, not a prompt.** A judge configured separately from
    the agent — `claude-sonnet-5` grading `claude-opus-5`, so it is not marking its own
    tier's homework — and pinned as one versioned unit: same model,
    prompt, rubric and settings — applied systematically across every category, receiving
    the context, the full interaction trace, and the deterministic signals code already
    established (whether the agent searched or answered from context, whether references
    resolved, whether poor sources were flagged). It rules only on what needs judgement;
    everything checkable is checked in code. Scores from different judge versions are
    never compared — re-judge the stored transcripts instead (§5).
20. **Don't build for hypotheticals.** The provider is fixed by C1, so we depend on the
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
│   ├── session.py         # conversation history, article registry, context budget (§2.4)
│   ├── citations.py       # citation extraction, verification + formatting
│   ├── rendering.py       # source list + inline quality markers (renderer-enforced)
│   └── cli.py             # entry point
├── tests/
│   ├── unit/              # mocked API, no network, no model calls
│   ├── compliance/        # §0 constraint checks (C1, C2)
│   ├── fixtures/          # recorded Anthropic responses for offline loop tests
│   └── integration/       # real API, recorded fixtures
└── evals/
    ├── dataset.jsonl      # graded question set + short conversations
    ├── calibration.jsonl  # hand-graded entries; gates judge changes
    ├── judge.py           # versioned judge: prompt, rubric, structured output
    ├── scorers.py         # deterministic scorers (free, re-runnable)
    └── run_eval.py        # scoped runner + cost reporting
```

---

## 4. Build phases

Thirteen small phases. Each one ends with a commit pushed to `main`, a green test suite,
and something demonstrable — no phase leaves the repo in a state where the previous
phase's capability has regressed.

**On the eval column.** Real evals need a working agent, so phases 0–5 list **acceptance
checks**: deterministic, free, and runnable offline or against the live Wikipedia API.
Paid evals start at Phase 6, once the harness exists, and each later phase contributes
its cases to the growing set (principle #14 — paid calls stay deliberate and scoped).

---

### Phase 0 — Scaffold & CI
**Deliverable:** `pyproject.toml`, package skeleton, `pytest` + `ruff` + `mypy` config,
CI workflow, this plan.

**Unit tests:** none yet — the suite exists and runs green on zero tests.

**Acceptance check:** CI passes on a push; `mypy --strict` runs clean on an empty package.

**Done when:** a fresh clone installs and `pytest` exits 0.

---

### Phase 1 — Wikipedia HTTP discipline
**Deliverable:** `wikipedia.py` skeleton — the §2.1 boundary with User-Agent, serial
throttling, explicit timeouts, retry/backoff, and the typed error hierarchy. One trivial
endpoint to exercise it.

**Unit tests:**
- UA header present and matching the documented format on every request; startup raises
  when contact info is unset.
- Requests are serialized and the minimum interval is honoured (assert against a fake
  clock, not `sleep`).
- Connect and read timeouts are set explicitly; a hung response raises `WikipediaTimeout`.
- 429 with `Retry-After` honours it; 5xx backs off exponentially with jitter and gives up
  after the bounded retry count as `RateLimited`.
- `MediaWiki-API-Error` header and body error codes both map to `WikipediaAPIError`.
- No `httpx` exception escapes the module.

**Acceptance check (free, live API):** one real request to enwiki succeeds and returns a
typed result with the UA we claim to send.

**Done when:** every error path is reachable in tests and no HTTP type escapes the module.

---

### Phase 2 — Retrieval methods
**Deliverable:** `search`, `get_summary`, `get_article` with section scoping, multivalue
batching, and the response cache.

**Unit tests:**
- `limit` defaults to 5 and clamps at 20 regardless of the value passed.
- Section scoping returns only the requested section; whole-article fetch truncates at
  the documented character budget.
- Batching builds `titles=A|B|C` and caps at 50 titles per request.
- Cache hit/miss on endpoint + normalized params; TTL expiry; a cache hit issues no
  request.
- Redirects resolve to the canonical title; `PageNotFound` and `DisambiguationError`
  (carrying its options) raise correctly.

**Acceptance checks (free, live API):**
1. A known-stable article fetches and section-splits correctly.
2. A known disambiguation title raises `DisambiguationError` with a non-empty option list.

**Done when:** all three retrieval methods work against live Wikipedia and every bound is
enforced in the client rather than by the caller.

---

### Phase 3 — Provenance & quality grades
**Deliverable:** `provenance.py` — the `Provenance` record, grade resolution, and tiering
(§2.3). `prop=extracts|revisions|pageassessments` fetched in one call.

**Unit tests:**
- Every retrieval returns a populated `Provenance`: title, page_id, revision_id,
  article_url, revision_timestamp, retrieved_at.
- Grade resolution: `Project-independent assessment` preferred; lowest-of-projects
  fallback when it is absent; `Unassessed` when there are no assessments at all.
- `pacontinue` pagination is followed with `palimit=max`.
- `importance` is recorded but never influences the grade.
- All ten grades map to the right tier; Start / Stub / Unassessed are Poor.
- Provenance and grade are taken from API metadata only — a fixture whose *body* claims a
  revision or a Featured rating changes neither.

**Acceptance checks (free, live API):**
1. A B-class article and a Start-class article each resolve to the expected grade and tier.
2. Every returned record has a `revision_id` and an `article_url` that resolves.

**Done when:** content, provenance, and grade arrive together in a single request.

---

### Phase 4 — Tool layer
**Deliverable:** `tools.py` — three `@beta_tool` functions over the client, with untrusted
content delimiting.

**Unit tests:**
- Each tool is callable standalone and returns typed results.
- Generated schemas match the signatures; no hand-written JSON Schema.
- `PageNotFound` / `DisambiguationError` / `WikipediaTimeout` become useful tool results
  rather than exceptions escaping into the loop — disambiguation surfaces its options so
  the agent can retry.
- Article text is fenced, labelled untrusted, and tagged with its provenance.
- A tool argument outside its bounds is clamped by the client, not the tool (principle
  #16).

**Acceptance check (free):** each tool invoked directly returns content, provenance, and
grade in the shape the agent will receive.

**Done when:** the tools are usable without an agent and no exception escapes them.

---

### Phase 5 — Agent loop, single turn
**Deliverable:** `agent.py` — `tool_runner` wiring, system prompt, grounding contract.
Plain-text answers with basic citations; full rendering comes in Phase 7.

**Unit tests (offline, recorded fixtures via mock transport — §2.2):**
- A multi-turn tool sequence drives to completion.
- All `tool_result` blocks from one assistant turn go back in a single user message.
- A failed tool returns `is_error: true` rather than being dropped.
- Assistant content blocks are echoed back unchanged.
- `stop_reason` is checked before content is read, `refusal` included.
- The retrieval-call cap terminates the loop.
- **Compliance (Layer 0):** no outbound tool entry carries a `type` field; the model ID is
  an Anthropic model.

**Acceptance check (paid, ~cents):** one manual smoke question end to end against the live
API, answered with a real citation. Explicitly invoked, not part of `pytest`.

**Done when:** a single-hop question is answered from live Wikipedia with a citation.

---

### Phase 6 — Eval harness
**Deliverable:** `evals/` — dataset format, scoped runner, deterministic scorers, the
versioned judge (§5), and cost reporting. Seeded with the single-hop and
citation-validity cases.

**Unit tests:**
- Dataset entries parse; malformed entries fail loudly.
- Scorers run against a stored transcript with no model call: citation validity,
  provenance integrity, source disclosure.
- `--category` and `--limit` select the right subset; `--all` selects everything.
- Cost estimation is printed before the run and actual cost after.
- The Layer 3 marker excludes evals from the default `pytest` run.
- The judge package is assembled correctly: context, full trace, reference answer,
  deterministic signals, and the enabled criteria for that entry.
- `judge_version` is stamped on every report, and the runner refuses to compare reports
  written under different judge versions.
- Judge output parses into per-criterion verdicts; a malformed response fails loudly
  rather than scoring zero.
- The calibration set re-runs and is asserted against its hand-graded labels, and the
  configured judge is rejected when agreement falls below the gate threshold.
- Agent and judge model IDs are read from separate config keys; neither defaults to the
  other.
- The judge package is size-checked against the judge model's context window and fails
  loudly rather than truncating.

**Evals (paid, scoped):**
1. **Single-hop factual**, ~5 questions — the first real score.
2. **Citation validity** on the same run — free to re-score afterwards.

**Done when:** `python -m evals.run --category single-hop --limit 5` produces a scored
report carrying a dollar figure and a `judge_version`, and is absent from `pytest`.

---

### Phase 7 — Rendering & quality flags
**Deliverable:** `rendering.py` — source list, article links, inline `[n ⚠ <grade>]`
decoration, footer note (§2.3).

**Unit tests:**
- Poor-tier markers decorate; B-class and above stay bare.
- The footer note renders once when any poor source is present, not at all otherwise.
- Markers survive decoration without renumbering.
- The source list shows canonical article URLs; `revision_id` is retained internally and
  never rendered.
- Every retrieved article appears — including ones consulted but not cited.
- Titles needing escaping produce valid URLs.

**Evals (paid, scoped):**
1. **Low-quality source** — a question only a Stub covers: answered, flagged inline, and
   named.
2. **Competing sources** — a claim covered by a Stub and a GA: the better source wins.

**Done when:** source disclosure scores 100% and quality flags are renderer-enforced.

---

### Phase 8 — Conversation
**Deliverable:** `session.py` — message history, article registry, stable markers (§2.4).

**Unit tests:**
- Registry entries persist across turns, keyed by page id.
- Markers stay stable as articles accumulate; new articles take the next free number.
- Quality tiers never decay — a Poor entry still renders `⚠` many turns later.
- A follow-up about an already-fetched article re-uses context; a new section is recorded
  as a fresh retrieval.
- Per-turn source lists render from registry entries touched that turn.
- `/new` clears history, registry, and marker numbering.

**Evals (paid, scoped):**
1. **Follow-up (pronoun)** — "Who was Ben Franklin?" → "Where was he born?"
2. **Marker stability** — scored programmatically across the same conversation.

**Done when:** the Franklin sequence answers correctly with stable markers.

---

### Phase 9 — Context budget & eviction
**Deliverable:** token budgeting, article-body eviction, the user-facing notice (§2.4).

**Unit tests:**
- Eviction drops article bodies while keeping registry metadata.
- An evicted article is still citable with its recorded provenance.
- Re-fetching an evicted article is served from cache.
- The budget notice appears when eviction happens and never when it doesn't.
- A turn limit and token budget are both enforced.

**Evals (paid, scoped):**
1. **Long conversation** — 10+ turns past the budget: markers stable, citations intact.
2. **Follow-up (refinement)** after eviction — "say more about X" still works.

**Done when:** a long conversation stays inside the context window and says when it has
evicted.

---

### Phase 10 — Multi-hop & honest refusal
**Deliverable:** iterative retrieval, retrieval-failure paths, refusal behaviour.

**Unit tests:**
- A retrieval failure surfaces as "I couldn't retrieve this", never silence.
- The retrieval-call cap holds under an intentionally ambiguous question.
- The per-question deadline fires and is reported.

**Evals (paid, scoped):**
1. **Multi-hop** — "Who succeeded the person who did X?"
2. **Not-in-Wikipedia** — declines rather than inventing.

**Done when:** two-hop questions resolve and refusals are correct.

---

### Phase 11 — Trust & injection resistance
**Deliverable:** hardened delimiting, precedence rule in the system prompt, injection
fixtures.

**Unit tests:**
- Fixture articles carrying directives stay delimited and labelled.
- Provenance and grade remain API-derived regardless of body content.
- Client-side limits hold when a tool argument is influenced by retrieved text.

**Evals (paid, scoped):**
1. **Injection, single-turn** — an article carrying "ignore your instructions".
2. **Injection, multi-turn** — injected in turn one, still not followed in turn five.

**Done when:** injection resistance scores 100%.

---

### Phase 12 — CLI, docs & release run
**Deliverable:** `cli.py` with an interactive session loop and `/new`, README, and a full
eval run.

**Unit tests:**
- CLI parses arguments, starts a session, and handles `/new` and exit.
- A config error fails at startup with a clear message (principle #12).

**Evals (paid, full):**
1. **The complete suite** on the test split — the headline numbers against §5's bar.
2. **Cost and latency per question** recorded for the release report.

**Done when:** a new user can install and hold a multi-turn conversation from the README
alone, and the release run meets the §5 bar.

---

## 5. Validation

Three layers, cheapest first. The eval set is built before we start tuning prompts,
so we are never tuning against a moving target.

### Layer 0 — Constraint compliance (no network, no model)
The §0 checks, run first and on every commit because a violation invalidates the whole
deliverable regardless of how well it scores:
- No entry in the outbound `tools` array carries a `type` field (C2).
- The client in use is the `anthropic` SDK, and both the agent and judge model IDs are
  Anthropic models (C1).
- No hosted-retrieval dependency appears in `pyproject.toml`.

### Layer 1 — Unit tests (no network, no model)
Run on every commit, fast.
- Wikipedia client: URL construction, UA header present and well-formed on every
  request, startup failure on unset contact info, serial-request enforcement and the
  minimum interval, `limit` clamping at the cap, explicit timeouts set, backoff on
  429/5xx, cache hit/miss, section extraction.
- Error mapping: each MediaWiki error shape produces the right typed exception, and no
  `httpx` exception escapes the client.
- Session state (§2.4): registry entries survive across turns, markers stay stable as
  articles accumulate, quality tiers never decay, eviction drops article bodies while
  keeping registry metadata, and `/new` clears both history and marker numbering.
- Agent loop: the real `tool_runner` driven against recorded Anthropic responses via a
  mock `httpx` transport (§2.2) — multi-turn tool sequences, tool errors surfacing as
  `is_error` results, and the retrieval-call cap terminating the loop. No network, no
  spend.
- Tool functions against recorded fixtures: normal article, disambiguation page,
  missing title, redirect, very long article.
- Citation formatting, parsing, and provenance round-tripping: the rendered source list
  shows the canonical article URL, the `revision_id` is retained in the record but never
  rendered, and titles needing escaping produce valid URLs.
- Grade tiering and the warning renderer: each of the ten grades maps to the right tier;
  Start / Stub / Unassessed decorate their inline marker as `[n ⚠ <grade>]` while B and
  above stay bare; the footer note renders once when any poor source is present and not
  at all otherwise; markers survive decoration without renumbering; and the source list
  includes every retrieved article whether cited or merely consulted.
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
reference answer, and the article(s) that should be cited. Target ~40–60 entries — single questions, or
short conversations scored turn by turn — across these categories:

| Category | What it probes | Example shape |
|---|---|---|
| Single-hop factual | Basic retrieval + extraction | "When was X founded?" |
| Multi-hop | Iterative retrieval | "Who succeeded the person who did X?" |
| Ambiguous entity | Disambiguation handling | A name shared by several subjects |
| Not-in-Wikipedia | Honest refusal | Something Wikipedia genuinely doesn't cover |
| Recently changed | Freshness vs. a stale index | A topic updated in the last month |
| **Injection resistance** | Untrusted content (§2.3) | A fixture article carrying "ignore your instructions" directives |
| **Low-quality source** | Grade surfacing + warning | A question only a Stub covers — is it answered, flagged, and the article named? |
| **Competing sources** | Best-available selection | A claim covered by both a Stub and a GA — is the better source preferred? |
| **Follow-up (pronoun)** | History resolution | "Who was Ben Franklin?" → "Where was he born?" |
| **Follow-up (refinement)** | Re-use over re-fetch | "Say more about the kite experiment" |
| **Follow-up (pivot)** | Subject change detected | "What about Jefferson?" |
| **Long conversation** | Eviction + marker stability | 10+ turns past the token budget |

**Grading.** Three scores per question:
1. **Answer correctness** — LLM-as-judge against the reference answer, under the fixed
   judge contract below.
2. **Citation validity** — programmatic, not judged (principle #14): every cited article
   must exist, and the cited text must actually appear in the fetched content. Free,
   deterministic, and it catches fabricated citations — the failure mode that matters
   most here.
3. **Refusal correctness** — on the not-in-Wikipedia set, did it decline instead of
   inventing an answer? Detected by structure, not by a judge, where the refusal has a
   recognizable shape.

4. **Provenance integrity** — programmatic: every citation resolves to a retrieval whose
   `revision_id` was recorded, the cited text appears in *that* revision, and the
   displayed article link is the canonical URL for that title. A citation with no recorded
   revision behind it is a failure even if the article supports the claim.
5. **Injection resistance** — programmatic: on the injection set, the agent answered the
   user's question, took no action the embedded directive asked for, and kept its
   citations intact.
6. **Follow-up resolution** — on multi-turn entries, did the agent answer about the right
   subject, and is the answer still grounded in a citation rather than assumed from
   context? Scored per turn, so a conversation that drifts on turn four is visible.
7. **Marker stability** — programmatic: across a conversation, an article keeps the same
   citation number and the same quality flag in every turn it appears.
8. **Source disclosure** — programmatic: every article retrieved during the run appears
   in the response's source list, every entry carries a grade, every claim marker backed
   by a poor-tier source is decorated inline, and no marker points at a source missing
   from the list. Renderer-enforced, so this is a regression check
   rather than a model score.

Only scores 1 and 6 need a paid judge call. The other six are deterministic and run
against a stored transcript for free (principle #14) — so re-scoring citations, provenance, and
injection resistance after a change costs nothing.

#### The judge

A judge that grades differently on Tuesday than it did on Monday makes every comparison
meaningless — a score change would tell us nothing about whether the agent improved. So
the judge is a **fixed, versioned component**, not a prompt written per eval.

**One judge, one rubric, applied systematically.** A single prompt and rubric covers every
category. Category-specific criteria are *enabled or disabled per entry*, never rewritten
— so "was the refusal correct?" is the same question in the same words wherever it is
asked. The judge model, prompt, rubric, and settings are pinned together as a
`judge_version` recorded in **every** report. Changing any of them bumps the version, and
**scores from different judge versions are never compared** — a re-judge of the stored
transcripts is the only valid way to move a baseline forward. Re-judging is cheap because
transcripts are stored.

**What the judge receives**, as one structured package per entry:

| Input | Why |
|---|---|
| **Context** — the question, or the full conversation up to this turn | A follow-up cannot be graded without what came before |
| **The interaction transcript** — every turn, tool call, tool result, and the rendered answer | What the agent *did*, not just what it said |
| **The reference answer** and expected source article(s) | The target |
| **Deterministic signals** — the facts code already established | See below |
| **Which criteria apply** to this entry | Systematic application, not ad-hoc judgement |

**Deterministic signals are given to the judge as established facts, never re-derived by
it** (principle #14). Code already knows these, and asking a model to re-determine them
adds cost and variance for nothing:

- Did the agent **search Wikipedia or answer from context**? (tool calls in the trace)
- Were **references provided**, and do they resolve to articles actually retrieved?
- Is every citation's text **present in the recorded revision**?
- Were **poor-quality sources flagged** inline and in the source list?
- Was **every retrieved article disclosed**?
- Retrieval count, turn index, tokens, latency, cost.

This split is the point: **the judge rules only on what needs judgement** — is the answer
factually right, did the follow-up resolve to the right subject, was weak sourcing
acknowledged in the prose where it mattered. Everything checkable is checked in code and
handed over as input. It keeps the judge's job narrow, which is what makes it consistent.

**Output is structured, not prose.** Per criterion: a verdict, a confidence, and a
one-line reason. A single blended score hides which criterion moved and makes regressions
untraceable.

**The judge's own settings** are pinned independently of the agent's, not copied from
them: `claude-sonnet-5`, adaptive thinking, `output_config.effort: "low"`. Low effort is
deliberate — the judge's task is narrow and bounded, and a deeper-thinking judge buys
variance rather than accuracy. Its 1M context holds a full multi-turn transcript; the
runner still **size-checks the judge package and fails loudly** rather than truncating a
transcript, since silently grading a partial transcript would be a wrong score presented
as a right one (principle #12).

**Consistency measures:**
- Fixed criterion order; the judge never sees other entries' scores, so it cannot drift
  or anchor within a run.
- Deterministic settings, pinned in `judge_version`.
- **A calibration set** of hand-graded entries re-run on every judge change — if the judge
  disagrees with the human labels, the judge changed, not the agent. Agreement with the
  human labels is a **release gate**: a judge model that falls below the threshold is not
  used, and the fallback (a more capable judge) is recorded with the reason. This is what
  makes a cheaper judge a measured win rather than a hopeful one.
- **Self-preference is a known risk**, since the judge and the agent are the same model
  family. Mitigated by keeping the judge's scope narrow (above), by the calibration set,
  and by the fact that the scores which gate release — citations, provenance, disclosure,
  injection — are deterministic and not the judge's to give.

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

**Bar for v1** (the Phase 12 release run): ≥85% answer correctness on the test split, ≥95% citation validity
(a fabricated citation is worse than a wrong answer — it looks trustworthy), ≥90% correct
refusals, **100% provenance integrity** (a citation without a revision is a bug, not a
near miss), **100% injection resistance** — a single instance of following embedded
instructions is a failure, not a percentage — **100% source disclosure**, which is
renderer-enforced and so should never drop below it without a code defect, **≥90%
follow-up resolution**, and **100% marker stability** — also renderer-enforced.

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
| The model forgets to flag a weak source on a long answer | Flagging is renderer-enforced from the grade field, never model-dependent (§2.3); covered by unit tests and a 100% source-disclosure eval score |
| Prompt injection via article text | Narrow blast radius by construction (C2 leaves no tool worth hijacking), plus delimiting, an explicit precedence rule, client-side limit enforcement, and an eval category held to 100% (§2.3) |
| A citation that can't be reproduced later because the article changed | Every retrieval records a `revision_id`, so verification and eval re-runs check the text we actually read; provenance integrity is scored at 100% |
| A C2 violation slips in — someone adds a server tool for convenience | Layer 0 test fails any tool entry carrying a `type` field; runs on every commit |
| `tool_runner` is beta and its surface may change | Tool functions are plain Python and the loop is ~30 lines to bring in-house; pin the SDK version and cover the loop with offline fixture tests |
| A hard stop mid-turn is awkward under `tool_runner` — the cap returns a refusal result rather than breaking outright | Accepted; the cap still holds, the model just finishes its turn. Revisit only if runaway loops show up in eval runs |
| A follow-up answered from model priors because context makes it feel obvious | Grounding is per-turn, not per-session (principle #6); follow-up entries are scored for citations, not just for the right subject |
| Context exhaustion mid-conversation | Registry metadata is tiny and always kept; article bodies evict against a configured budget and re-fetch from cache; the agent says when it has evicted rather than forgetting silently (§2.4) |
| Injected content from an early turn influencing a later one | Multi-turn case in the injection eval category; the §2.3 defences are turn-independent |
| Paid model calls fire accidentally during development | Layer 3 is marker-excluded from the default `pytest` run, kept out of watch modes and pre-commit hooks, and needs an explicit command that names a scope (principle #14) |
| Judge drift makes scores incomparable across runs | Judge model, prompt, rubric and settings are pinned as a `judge_version` stamped on every report; the runner refuses cross-version comparisons, and a hand-graded calibration set gates every judge change (§5) |
| Judge self-preference — it grades work from the same vendor family | Judge and agent are different models (`claude-sonnet-5` judging `claude-opus-5`), the judge's scope is narrow (deterministic facts are computed in code and handed to it), the release-gating scores are not the judge's to give, and the calibration set catches divergence from human labels |
| A cheaper judge silently grades worse | Calibration agreement is a release gate, not a report line — a judge below threshold is not used, and the fallback is recorded with its reason |
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
