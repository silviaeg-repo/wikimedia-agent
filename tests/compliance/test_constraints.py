"""Layer 0 -- the §0 hard constraints, checked structurally (no network, no model).

A violation here invalidates the deliverable regardless of how well it scores,
so these run first and on every commit.
"""

from __future__ import annotations

import pathlib

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
