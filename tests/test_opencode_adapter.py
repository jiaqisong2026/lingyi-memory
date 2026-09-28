# -*- coding: utf-8 -*-
"""Tests for the opencode -> LingYi memory adapter."""

from __future__ import annotations
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.integrations.opencode_adapter.reader import (
    OpencodeSession, OpencodeMessage, OpencodeToolCall, read_session_json,
)
from src.integrations.opencode_adapter.indexer import (
    OpencodeIndexer, index_session,
)


# ----------------------------------------------------------------------
# Fixtures: synthetic opencode session JSON
# ----------------------------------------------------------------------


SAMPLE_SESSION = [
    {
        "id": "msg_001",
        "role": "user",
        "sessionID": "sess_abc",
        "time": {"created": 1700000000},
        "parts": [
            {"type": "text", "text": "deploy the kylin service and check the port"}
        ],
    },
    {
        "id": "msg_002",
        "role": "assistant",
        "sessionID": "sess_abc",
        "time": {"created": 1700000010},
        "parts": [
            {"type": "text", "text": "I will deploy and verify the port is listening."},
            {"type": "tool", "name": "shell", "args": ["systemctl", "status", "kylin"]},
        ],
    },
    {
        "id": "msg_003",
        "role": "assistant",
        "sessionID": "sess_abc",
        "time": {"created": 1700000020},
        "parts": [
            {"type": "tool_result", "output": "active (running) since Mon 2024-01-01", "error": ""},
        ],
    },
    {
        "id": "msg_004",
        "role": "assistant",
        "sessionID": "sess_abc",
        "time": {"created": 1700000030},
        "parts": [
            {"type": "tool", "name": "shell", "args": ["ss", "-tlnp"]},
        ],
    },
    {
        "id": "msg_005",
        "role": "assistant",
        "sessionID": "sess_abc",
        "time": {"created": 1700000040},
        "parts": [
            {"type": "tool_result", "output": "", "error": "command not permitted"},
        ],
    },
]


@pytest.fixture
def session_file(tmp_path):
    p = tmp_path / "session.json"
    p.write_text(json.dumps(SAMPLE_SESSION), encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# Reader tests
# ----------------------------------------------------------------------


class TestReader:
    def test_reads_list_shape(self, session_file):
        sess = read_session_json(session_file)
        assert sess.session_id == "sess_abc"
        assert len(sess.messages) == 5

    def test_user_message_is_text_only(self, session_file):
        sess = read_session_json(session_file)
        m = sess.messages[0]
        assert m.role == "user"
        assert "deploy the kylin service" in m.text
        assert m.tool_call is None

    def test_assistant_message_with_tool_call(self, session_file):
        sess = read_session_json(session_file)
        m = sess.messages[1]
        assert m.role == "assistant"
        assert m.tool_call is not None
        assert m.tool_call.name == "shell"
        assert m.tool_call.argv == ["systemctl", "status", "kylin"]

    def test_tool_result_attached_to_message(self, session_file):
        sess = read_session_json(session_file)
        m = sess.messages[2]
        assert "active (running)" in m.tool_stdout

    def test_error_attaches_to_stderr(self, session_file):
        sess = read_session_json(session_file)
        m = sess.messages[4]
        assert "command not permitted" in m.tool_stderr

    def test_reads_object_shape(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text(json.dumps({
            "id": "sess_xyz",
            "messages": [
                {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}
            ],
        }), encoding="utf-8")
        sess = read_session_json(p)
        assert sess.session_id == "sess_xyz"
        assert sess.messages[0].text == "hi"

    def test_to_lingyi_event_shape(self, session_file):
        sess = read_session_json(session_file)
        m = sess.messages[2]  # the tool_result message
        evt = m.to_lingyi_event("sess_abc")
        assert "tool_result" in evt["content"] or "active (running)" in evt["content"]
        assert evt["session_id"] == "sess_abc"

    def test_unknown_fields_ignored(self, tmp_path):
        """Extra fields in the input must not raise."""
        p = tmp_path / "noise.json"
        p.write_text(json.dumps([
            {
                "id": "m1", "role": "user", "sessionID": "s",
                "exotic_field": {"foo": [1, 2, 3]},
                "parts": [{"type": "text", "text": "ok"}],
            }
        ]), encoding="utf-8")
        sess = read_session_json(p)
        assert sess.messages[0].text == "ok"


# ----------------------------------------------------------------------
# Indexer tests
# ----------------------------------------------------------------------


class _FakeMemory:
    """In-memory stand-in for MemorySystemV2.

    Implements just the surface the indexer uses, plus a tiny recall
    that returns rows whose content contains the query substring.
    """

    def __init__(self):
        self.rows: List[Dict[str, Any]] = []
        self._next_id = 0

    def store(self, content, category="interaction", importance=0.5, **kwargs):
        if not content:
            return None
        self._next_id += 1
        row = {
            "id": f"mem_{self._next_id}",
            "content": content,
            "category": category,
            "importance": importance,
            **kwargs,
        }
        self.rows.append(row)
        return row["id"]

    def recall(self, query, top_k=5):
        q = query.lower()
        scored = [
            (r, sum(1 for w in q.split() if w in r["content"].lower()))
            for r in self.rows
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [r for r, s in scored[:top_k] if s > 0]


class TestIndexer:
    def test_indexes_every_message(self, session_file):
        mem = _FakeMemory()
        idx = OpencodeIndexer(mem)
        n = idx.index_file(session_file)
        assert n == 5
        assert len(mem.rows) == 5

    def test_recall_finds_relevant_session(self, session_file):
        mem = _FakeMemory()
        idx = OpencodeIndexer(mem)
        idx.index_file(session_file)
        hits = idx.recall("kylin port", top_k=3)
        assert len(hits) >= 1
        # The user message about "kylin service" must be in the top hits.
        assert any("deploy the kylin service" in r["content"] for r in hits)

    def test_stderr_lowers_importance(self, session_file):
        mem = _FakeMemory()
        idx = OpencodeIndexer(mem, importance_scale=0.5)
        idx.index_file(session_file)
        # The last message has stderr; its importance should be lower
        # than a clean message of similar length.
        last = mem.rows[-1]
        first_user = mem.rows[0]
        assert last["importance"] < first_user["importance"]

    def test_session_id_propagated(self, session_file):
        mem = _FakeMemory()
        idx = OpencodeIndexer(mem)
        idx.index_file(session_file)
        assert all(r["session_id"] == "sess_abc" for r in mem.rows)

    def test_tool_call_argv_preserved(self, session_file):
        mem = _FakeMemory()
        idx = OpencodeIndexer(mem)
        idx.index_file(session_file)
        # Tool-call argv now lives inside metadata (the v2 store API does
        # not accept arbitrary kwargs; the adapter packs them in).
        tool_rows = [r for r in mem.rows
                     if r.get("metadata", {}).get("argv")]
        assert any(
            r["metadata"]["argv"] == ["systemctl", "status", "kylin"]
            for r in tool_rows
        )

    def test_functional_index_session(self, session_file):
        mem = _FakeMemory()
        n = index_session(mem, str(session_file))
        assert n == 5


# ----------------------------------------------------------------------
# Integration with the real LingYi memory v2 (no network, in-temp-dir)
# ----------------------------------------------------------------------


class TestIntegrationWithLingYiV2:
    def test_real_memory_v2_indexes_and_recalls(self, session_file, tmp_path):
        from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig

        db = str(tmp_path / "opencode_ingest.db")
        mem = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            idx = OpencodeIndexer(mem)
            stored = idx.index_file(session_file)
            assert stored == 5
            hits = idx.recall("kylin service deployment", top_k=3)
            assert len(hits) >= 1
            # The first hit should reference the user message.
            assert any("deploy" in h.get("content", "").lower() for h in hits)
        finally:
            mem.close()


class TestRealSessionFile:
    '''Round-6 deepening: load a committed real-shape opencode session.'''

    SESSION_FIXTURE = 'tests/data/sample_opencode_session.json'

    def test_fixture_file_parses(self):
        from src.integrations.opencode_adapter.reader import read_session_json
        from pathlib import Path
        p = Path(self.SESSION_FIXTURE)
        assert p.exists(), 'session fixture must ship with the test suite'
        sess = read_session_json(p)
        assert sess.session_id == 'sess_demo_001'
        assert len(sess.messages) == 6
        # Two user messages, four assistant.
        roles = [m.role for m in sess.messages]
        assert roles.count('user') == 2
        assert roles.count('assistant') == 4

    def test_fixture_indexes_into_real_memory_v2(self, tmp_path):
        from src.integrations.opencode_adapter.indexer import OpencodeIndexer
        from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig
        mem = MemorySystemV2(MemoryConfig(db_path=str(tmp_path / 'real.db')))
        try:
            idx = OpencodeIndexer(mem)
            n = idx.index_file(self.SESSION_FIXTURE)
            assert n == 6
            # Three different queries, each must recall at least one row.
            for q_text in ('restart kylin', 'port 8080', 'tail error log'):
                hits = idx.recall(q_text, top_k=3)
                assert hits, 'no recall hits for ' + q_text
        finally:
            mem.close()
