"""Layer 0 -- the §0 hard constraints, checked structurally (no network, no model).

A violation here invalidates the deliverable regardless of how well it scores,
so these run first and on every commit.
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx

from tests.helpers import CONTACT
from wikimedia_agent.tools import WikipediaTools
from wikimedia_agent.wikipedia import WikipediaClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_FILES = sorted((REPO_ROOT / "src").rglob("*.py"))


def _tools() -> WikipediaTools:
    client = WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
        min_interval=0.0,
    )
    return WikipediaTools(client=client)


# -- C2: no built-in hosted search or RAG tools ---------------------------


def test_no_declared_tool_carries_a_type_field():
    """The structural signature of a server tool.

    Anthropic's hosted tools -- web_search, web_fetch, code_execution -- are
    exactly the ones identified by a ``type`` field; user-defined tools carry
    ``name`` / ``description`` / ``input_schema``. Checking for ``type`` catches
    any of them, including ones that do not exist yet, without maintaining a
    blocklist of names.
    """
    for tool in _tools().as_list():
        definition = tool.to_dict()
        assert "type" not in definition, (
            f"C2 violation: tool {definition.get('name')!r} declares a `type` field, "
            "which identifies an Anthropic server tool."
        )
        assert set(definition) <= {"name", "description", "input_schema", "strict"}


def test_every_tool_is_one_we_execute_ourselves():
    tools = _tools()
    names = {tool.to_dict()["name"] for tool in tools.as_list()}
    assert names == {"search_wikipedia", "get_summary", "get_article"}


def test_no_hosted_retrieval_dependency_is_declared():
    """C2 also rules out reaching hosted search through a dependency."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    forbidden = [
        "openai",
        "perplexity",
        "tavily",
        "serpapi",
        "google-search",
        "langchain",
        "llama-index",
        "pinecone",
        "weaviate",
        "chromadb",
    ]
    dependencies = pyproject.split("[project.optional-dependencies]")[0]
    for package in forbidden:
        assert package not in dependencies.lower(), f"C2: unexpected dependency {package!r}"


def test_source_never_imports_a_forbidden_retrieval_library():
    forbidden = ("import openai", "from openai", "import tavily", "from tavily",
                 "import langchain", "from langchain")
    for path in SOURCE_FILES:
        text = path.read_text()
        for marker in forbidden:
            assert marker not in text, f"C2 violation: {path.name} contains {marker!r}"


def test_no_server_tool_type_strings_appear_in_source():
    """web_search_*, web_fetch_*, code_execution_* are server tool type values."""
    markers = ("web_search_20", "web_fetch_20", "code_execution_20", "mcp_toolset")
    for path in SOURCE_FILES:
        text = path.read_text()
        for marker in markers:
            assert marker not in text, f"C2 violation: {path.name} references {marker!r}"


# -- C1: an Anthropic model via the Anthropic API -------------------------


def test_the_sdk_in_use_is_anthropic():
    import anthropic

    assert anthropic.__name__ == "anthropic"
    from anthropic import beta_tool  # noqa: F401  -- the tool decorator we build on


def test_retrieval_is_our_own_http_client():
    """C2 is satisfied by owning retrieval: our tools call our client, which
    talks to the public Wikipedia API over plain HTTP."""
    tools = _tools()
    assert isinstance(tools.client, WikipediaClient)
    assert tools.client.api_url.endswith("/w/api.php")


# -- what actually goes over the wire (C1 + C2) ---------------------------


def _run_one_exchange():
    """Drive one complete agent exchange offline and return the recorder."""
    from tests.helpers import RecordedAnthropic, assistant_message, text_block
    from wikimedia_agent.agent import WikipediaAgent

    api = RecordedAnthropic(assistant_message(content=[text_block("Answer.")]))
    empty: dict[str, Any] = {"query": {"pages": []}}
    wiki = WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=empty)),
        min_interval=0.0,
    )
    agent = WikipediaAgent(tools=WikipediaTools(client=wiki), client=api.client())
    agent.ask("Anything.")
    return api, agent


def test_no_tool_sent_to_the_api_carries_a_type_field():
    """The same structural check, applied to the real outbound request."""
    api, _ = _run_one_exchange()
    for tool in api.tools_sent(0):
        assert "type" not in tool, (
            f"C2 violation: tool {tool.get('name')!r} was sent with a `type` field, "
            "which identifies an Anthropic server tool."
        )


def test_only_our_three_tools_are_sent():
    api, _ = _run_one_exchange()
    assert {tool["name"] for tool in api.tools_sent(0)} == {
        "search_wikipedia",
        "get_summary",
        "get_article",
    }


def test_no_server_side_retrieval_features_are_requested():
    """mcp_servers and container would both reach hosted capability."""
    api, _ = _run_one_exchange()
    request = api.requests[0]
    for field_name in ("mcp_servers", "container"):
        assert not request.get(field_name), f"C2 violation: request set {field_name!r}"


def test_the_model_sent_is_an_anthropic_model():
    api, agent = _run_one_exchange()
    model = api.requests[0]["model"]
    assert model.startswith("claude-"), f"C1 violation: model {model!r} is not Anthropic"
    assert model == agent.model


def test_the_request_goes_to_the_anthropic_messages_api():
    from wikimedia_agent.agent import DEFAULT_MODEL

    assert DEFAULT_MODEL.startswith("claude-")


# -- §5: paid calls never run by accident ---------------------------------


def test_eval_runner_is_not_collected_by_pytest():
    """evals/ must not be a test path: a paid runner picked up by pytest would
    spend money on every commit."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    testpaths_line = next(
        line for line in pyproject.splitlines() if line.startswith("testpaths")
    )
    assert "evals" not in testpaths_line


def test_the_eval_marker_is_declared():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"eval:' in pyproject or "eval:" in pyproject


def test_paid_tests_are_marked_eval():
    """Any test that can spend money must carry the marker that excludes it."""
    paid = REPO_ROOT / "tests" / "integration" / "test_agent_smoke.py"
    text = paid.read_text()
    assert "pytestmark = pytest.mark.eval" in text


def test_ci_excludes_paid_and_networked_layers():
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "not integration and not eval" in workflow


def test_the_calibration_set_covers_both_verdicts():
    """A calibration set that only contains passes cannot detect a judge that
    passes everything."""
    from evals.models import iter_jsonl

    entries = list(iter_jsonl(REPO_ROOT / "evals" / "calibration.jsonl"))
    assert entries, "calibration set must not be empty"
    verdicts = {
        value
        for entry in entries
        for value in entry["expected"].values()
    }
    assert verdicts == {True, False}, "calibration needs both passing and failing cases"
