# wikimedia-agent

A question-answering agent grounded in live Wikipedia content. Every answer cites the
articles behind it, carries each source's [Wikipedia quality
grade](https://en.wikipedia.org/wiki/Wikipedia:Content_assessment), and flags weak
sources inline.

> **Status: in development.** The Wikipedia client (Phase 1) is built and tested. The
> agent itself arrives in Phase 5 — see [project-plan.md](project-plan.md) for the full
> plan and the thirteen build phases.

## What it does

- Answers natural-language questions from **English Wikipedia**, retrieved live.
- **Cites its sources** — article, section, and a link — and says "I don't know" rather
  than inventing an answer.
- **Grades every source** and flags anything below B-class inline, so a claim resting on
  a Stub is visibly different from one resting on a Featured Article.
- **Holds a conversation** — "Who was Ben Franklin?" then "Where was he born?" resolves
  correctly, with sources carried forward.
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

with WikipediaClient(contact="you@example-domain.org") as client:
    print(client.site_name())  # -> "Wikipedia"
```

## How it's built

| Area | Where |
|---|---|
| Wikipedia client — User-Agent, serial throttling, timeouts, typed errors | `src/wikimedia_agent/wikipedia.py` |
| Typed error hierarchy | `src/wikimedia_agent/errors.py` |
| Offline unit tests | `tests/unit/` |
| Live API checks (opt-in) | `tests/integration/` |

Two design decisions worth knowing before reading the code:

1. **All Wikipedia access is behind one client.** Nothing above `wikipedia.py` knows HTTP
   exists — callers get typed results and typed exceptions, never a response object.
2. **Requests are strictly serial.** The [MediaWiki API
   etiquette](https://www.mediawiki.org/wiki/API:Etiquette) asks clients to wait for one
   request to finish before sending the next, so there is no concurrency here by design.
   Batching multiple titles into one request is the sanctioned way to go faster.

The full rationale, the 20 guiding principles, and the validation strategy are in
[project-plan.md](project-plan.md).

## A note on Wikipedia content

Wikipedia is user-editable, and this agent treats every retrieved byte as untrusted data
— quoted and cited, never obeyed as an instruction. It also cannot verify that Wikipedia
is *correct*: it grounds answers in what articles say, cites them so you can check, and
surfaces each article's quality grade so you can judge how much weight to give it.

## License

MIT
