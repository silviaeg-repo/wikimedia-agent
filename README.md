# wikimedia-agent

A question-answering agent grounded in live Wikipedia content. Every answer cites the
articles behind it, carries each source's [Wikipedia quality
grade](https://en.wikipedia.org/wiki/Wikipedia:Content_assessment), and flags weak
sources inline.

> **Status: in development.** The Wikipedia client is built and tested through Phase 2 —
> search, summaries, section-scoped articles, batching and caching. The agent itself
> arrives in Phase 5; see [project-plan.md](project-plan.md) for the full plan and the
> thirteen build phases.

## What it does

- Answers natural-language questions from **English Wikipedia**, retrieved live.
- **Cites its sources** — article, section, and a link — and says "I don't know" rather
  than inventing an answer.
- **Grades every source** and flags anything below B-class inline, so a claim resting on
  a Stub is visibly different from one resting on a Featured Article.
- **Holds a conversation** — "Who was Ben Franklin?" then "Where was he born?" resolves
  correctly, with sources carried forward.
- **Asks rather than guesses.** When a subject is ambiguous and nothing in the
  conversation settles it, the agent asks which you meant instead of picking one and
  sounding confident.
- Uses **no hosted search or RAG tools**. Retrieval is a plain HTTP client against the
  public Wikipedia API.

## Requirements

- **Python 3.9 or newer.** Check with `python3 --version`.
  (Phase 5 adds the `anthropic` SDK, which may raise this floor to 3.10+.)
- An **Anthropic API key** — not needed yet; required from Phase 5 onward.
- Network access to `en.wikipedia.org`.

## Getting started

### 1. Clone

```bash
git clone https://github.com/silviaeg-repo/wikimedia-agent.git
cd wikimedia-agent
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv && source .venv/bin/activate
```

On Windows, activate with `.venv\Scripts\activate` instead.

### 3. Install

```bash
pip install -e ".[dev]"
```

### 4. Set your contact address

The Wikimedia API policy **requires** a descriptive `User-Agent` including a contact
address, and Wikimedia blocks non-compliant clients by IP without notice. The client
refuses to start without one — a placeholder counts as non-compliant:

```bash
export WIKIMEDIA_AGENT_CONTACT="you@example-domain.org"
```

Use a real address or project URL you actually monitor.

## Running the tests

The default run is **offline and free** — no network, no API calls, no cost:

```bash
pytest
```

Live Wikipedia checks are opt-in (still free, but they hit the real API):

```bash
WIKIMEDIA_AGENT_CONTACT="you@example-domain.org" pytest -m integration
```

Lint and type checks:

```bash
ruff check . && mypy
```

Evals cost money and **never** run as part of `pytest`. They arrive in Phase 6 and are
invoked explicitly with a scope:

```bash
python -m evals.run --category single-hop --limit 5
```

## Trying it out so far

The agent CLI lands in Phase 12. Today you can drive the Wikipedia client directly:

```python
from wikimedia_agent.wikipedia import WikipediaClient
from wikimedia_agent.errors import DisambiguationError, PageNotFound

with WikipediaClient(contact="you@example-domain.org") as client:
    # Search for candidate articles (limit is capped at 20 inside the client).
    for hit in client.search("first computer programmer", limit=3):
        print(hit.title, "--", hit.snippet[:60])

    # A cheap lead extract, following redirects.
    summary = client.get_summary("Ada Byron")
    print(summary.title, "<- redirected from", summary.redirected_from)

    # A whole article (truncated at a budget), or one section of it.
    article = client.get_article("Ada Lovelace")
    print(article.section_titles[:5], "truncated:", article.truncated)
    print(client.get_article("Ada Lovelace", section="Death").text[:200])

    # Several titles in one request -- batching, not concurrency.
    print(sorted(client.get_summaries(["Ada Lovelace", "Charles Babbage"])))

    # Ambiguous titles raise, carrying described candidates in the page's own
    # order -- which is what lets the agent ask the user a useful question
    # rather than guessing which Mercury you meant.
    try:
        client.get_article("Mercury")
    except DisambiguationError as exc:
        for option in exc.options[:3]:
            print(f"  {option.title} -- {option.description}")

    try:
        client.get_summary("Not A Real Page Xyzzy")
    except PageNotFound as exc:
        print(exc)
```

## How it's built

| Area | Where |
|---|---|
| Wikipedia client — User-Agent, serial throttling, timeouts, retrieval | `src/wikimedia_agent/wikipedia.py` |
| Typed results — search hits, articles, sections, summaries | `src/wikimedia_agent/models.py` |
| Typed error hierarchy | `src/wikimedia_agent/errors.py` |
| TTL response cache | `src/wikimedia_agent/cache.py` |
| Offline unit tests | `tests/unit/` |
| Live API checks (opt-in) | `tests/integration/` |

Two design decisions worth knowing before reading the code:

1. **All Wikipedia access is behind one client.** Nothing above `wikipedia.py` knows HTTP
   exists — callers get typed results and typed exceptions, never a response object.
2. **Requests are strictly serial.** The [MediaWiki API
   etiquette](https://www.mediawiki.org/wiki/API:Etiquette) asks clients to wait for one
   request to finish before sending the next, so there is no concurrency here by design.
   Batching multiple titles into one request is the sanctioned way to go faster.
3. **Ambiguity is surfaced, never resolved silently.** A disambiguation page raises
   rather than returning content, carrying described candidates in the page's own order
   so the agent can ask a question a user can actually answer.
4. **Bounds live in the client, not the caller.** Search limits, article size and batch
   size are clamped inside `wikipedia.py`, so nothing above it — including, later, a
   model choosing tool arguments — can widen them.

The full rationale, the 20 guiding principles, and the validation strategy are in
[project-plan.md](project-plan.md).

## A note on Wikipedia content

Wikipedia is user-editable, and this agent treats every retrieved byte as untrusted data
— quoted and cited, never obeyed as an instruction. It also cannot verify that Wikipedia
is *correct*: it grounds answers in what articles say, cites them so you can check, and
surfaces each article's quality grade so you can judge how much weight to give it.

## License

MIT
