from __future__ import annotations

import json

import pytest

from pipeline.state import (
    BuildIdentity,
    StaleBuildError,
    append_translation,
    load_translations,
    open_state,
    save_state,
)


def identity(**changes):
    values = {
        "book_slug": "book",
        "source_sha256": "source-v1",
        "glossary_version": "glossary-v1",
        "style_version": "style-v1",
        "provider": "https://relay.example/v1/messages",
        "model": "model-v1",
        "chunk_plan": "plan-v1",
    }
    values.update(changes)
    return BuildIdentity(**values)


CHUNKS = {"ch/c01": ("p1", "p2"), "ch/c02": ("p3",)}


def test_new_state_starts_pending_and_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = open_state(path, identity(), CHUNKS)
    assert state.pending_chunk_ids == ("ch/c01", "ch/c02")
    assert json.loads(path.read_text())["schema_version"] == 1
    assert open_state(path, identity(), CHUNKS).pending_chunk_ids == state.pending_chunk_ids


def test_done_chunk_is_not_pending_after_save(tmp_path):
    path = tmp_path / "state.json"
    state = open_state(path, identity(), CHUNKS)
    state.mark_done("ch/c01", attempts=2, usage={"input_tokens": 10})
    save_state(state, path)
    loaded = open_state(path, identity(), CHUNKS)
    assert loaded.done_count == 1
    assert loaded.pending_chunk_ids == ("ch/c02",)


def test_failed_chunk_remains_retryable(tmp_path):
    path = tmp_path / "state.json"
    state = open_state(path, identity(), CHUNKS)
    state.mark_failed("ch/c01", "upstream failed", attempts=4)
    save_state(state, path)
    assert "ch/c01" in open_state(path, identity(), CHUNKS).pending_chunk_ids


def test_identity_change_refuses_stale_cache(tmp_path):
    path = tmp_path / "state.json"
    open_state(path, identity(), CHUNKS)
    with pytest.raises(StaleBuildError, match="model"):
        open_state(path, identity(model="model-v2"), CHUNKS)


def test_chunk_plan_change_refuses_stale_cache(tmp_path):
    path = tmp_path / "state.json"
    open_state(path, identity(), CHUNKS)
    changed = {**CHUNKS, "ch/c03": ("p4",)}
    with pytest.raises(StaleBuildError, match="chunk"):
        open_state(path, identity(), changed)


def test_translation_log_is_append_only_and_latest_record_wins(tmp_path):
    path = tmp_path / "translations.jsonl"
    append_translation(path, "c1", {"p1": "旧译"}, {"cost": 1}, 1)
    append_translation(path, "c2", {"p2": "第二段"}, {}, 1)
    append_translation(path, "c1", {"p1": "新译"}, {"cost": 2}, 2)
    translations, records = load_translations(path)
    assert translations == {"p1": "新译", "p2": "第二段"}
    assert records["c1"]["attempts"] == 2
