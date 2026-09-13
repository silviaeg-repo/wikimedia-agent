"""Dataset parsing and scoping (§5, Phase 6)."""

from __future__ import annotations

import json

import pytest
from evals.models import DatasetError, EvalEntry, load_dataset, select

VALID = {
    "id": "sh-001",
    "category": "single-hop",
    "turns": ["Who was Ada Lovelace?"],
    "reference_answer": "An English mathematician.",
    "expected_articles": ["Ada Lovelace"],
    "criteria": ["answer_correctness"],
    "split": "train",
}


def write(tmp_path, *entries):
    path = tmp_path / "dataset.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries))
    return path


def test_a_valid_entry_parses(tmp_path):
    entries = load_dataset(write(tmp_path, VALID))
    assert len(entries) == 1
    assert entries[0].question == "Who was Ada Lovelace?"
    assert entries[0].is_conversation is False


def test_a_conversation_entry_keeps_its_turns(tmp_path):
    entry = {**VALID, "turns": ["Who was Ben Franklin?", "Where was he born?"]}
    parsed = load_dataset(write(tmp_path, entry))[0]
    assert parsed.is_conversation is True
    assert len(parsed.turns) == 2


def test_a_single_question_field_is_accepted(tmp_path):
    entry = {k: v for k, v in VALID.items() if k != "turns"}
    entry["question"] = "Who was Ada Lovelace?"
    assert load_dataset(write(tmp_path, entry))[0].turns == ("Who was Ada Lovelace?",)


# -- malformed entries fail loudly (principle #13) ------------------------


def test_missing_id_fails_loudly(tmp_path):
    entry = {k: v for k, v in VALID.items() if k != "id"}
    with pytest.raises(DatasetError, match="missing required field 'id'"):
        load_dataset(write(tmp_path, entry))


def test_unknown_category_fails_loudly(tmp_path):
    with pytest.raises(DatasetError, match="unknown category"):
        load_dataset(write(tmp_path, {**VALID, "category": "made-up"}))


def test_unknown_criterion_fails_loudly(tmp_path):
    with pytest.raises(DatasetError, match="unknown criteria"):
        load_dataset(write(tmp_path, {**VALID, "criteria": ["vibes"]}))


def test_invalid_split_fails_loudly(tmp_path):
    with pytest.raises(DatasetError, match="invalid split"):
        load_dataset(write(tmp_path, {**VALID, "split": "holdout"}))


def test_duplicate_ids_fail_loudly(tmp_path):
    """A duplicate silently overwrites, which makes a score look better than it is."""
    with pytest.raises(DatasetError, match="duplicate entry id"):
        load_dataset(write(tmp_path, VALID, VALID))


def test_invalid_json_names_the_line(tmp_path):
    path = tmp_path / "dataset.jsonl"
    path.write_text(json.dumps(VALID) + "\n{not json}")
    with pytest.raises(DatasetError, match=":2:"):
        load_dataset(path)


def test_empty_dataset_fails_loudly(tmp_path):
    path = tmp_path / "dataset.jsonl"
    path.write_text("// only a comment\n\n")
    with pytest.raises(DatasetError, match="no entries"):
        load_dataset(path)


def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "absent.jsonl")


def test_comments_and_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "dataset.jsonl"
    path.write_text("// a note\n\n" + json.dumps(VALID) + "\n")
    assert len(load_dataset(path)) == 1


# -- scoping ---------------------------------------------------------------


def entries():
    return [
        EvalEntry(id="a", category="single-hop", turns=("q",), split="train"),
        EvalEntry(id="b", category="single-hop", turns=("q",), split="test"),
        EvalEntry(id="c", category="not-in-wikipedia", turns=("q",), split="test"),
    ]


def test_category_selects_a_subset():
    assert [e.id for e in select(entries(), category="single-hop")] == ["a", "b"]


def test_split_selects_a_subset():
    assert [e.id for e in select(entries(), split="test")] == ["b", "c"]


def test_limit_caps_the_selection():
    assert len(select(entries(), limit=2)) == 2


def test_filters_combine():
    assert [e.id for e in select(entries(), category="single-hop", split="test")] == ["b"]


def test_no_filters_selects_everything():
    assert len(select(entries())) == 3


# -- the shipped dataset itself -------------------------------------------


def test_the_shipped_dataset_is_valid():
    from evals.run import DATASET_PATH

    entries_ = load_dataset(DATASET_PATH)
    assert entries_, "the seeded dataset must parse"
    assert {e.split for e in entries_} <= {"train", "validation", "test"}
    assert any(e.split == "test" for e in entries_), "a test split must exist"
