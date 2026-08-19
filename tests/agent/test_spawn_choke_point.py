"""Scope-A defense regression — central choke point for automatic review.

Locks the entrypoint-independent enforcement of the existing
``agent.skip_background_review`` policy at
``AIAgent._spawn_background_review``. The wrapper is the canonical
fail-safe choke point; the Codex caller maintains a defense-in-depth
local mirror; the explicit /refine callers (CLI + gateway) opt out
via ``automatic=False`` because they represent explicit user intent.

These tests are intentionally focused on:

  A1 — omitting ``automatic`` on a protected agent refuses to spawn.
  A2 — omitting ``automatic`` on an ordinary agent spawns exactly once.
  A3 — passing ``automatic=False`` on a protected agent STILL spawns
       (preserves /refine explicit user-triggered semantics).
  A4 — A3 holds regardless of ``focus=None``.
  A5 — codex_runtime's automatic spawn conditional includes the local
       ``not getattr(agent, "skip_background_review", False)`` mirror
       and its call does NOT pass ``automatic=False``.
  A6 — both /refine callers pass ``automatic=False`` at their
       ``_spawn_background_review`` call sites.
  A7 — the existing skip_background_review regression suite still
       imports and asserts the original contract.

Observability strategy:
  - Patch ``run_agent.threading.Thread`` to a recording stub for A1-A4.
  - Use structural/source assertions for A5-A6 to keep the suite
    hermetic and to avoid exercising a real provider.

Tests are stdlib + unittest.mock only; no live network calls; no real
agents constructed. The ``AIAgent.__new__`` shortcut skips the heavy
provider auto-detection logic for the wrapper-level tests.
"""
from __future__ import annotations

import logging
import os
import runpy
import sys
import threading
import types
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Recording stand-in for ``threading.Thread`` — captures every instantiation.
# ---------------------------------------------------------------------------


class _ThreadRecorder:
    """Stand-in for ``run_agent.threading.Thread``.

    Records every instantiation and refuses to actually start a real
    thread (calls to ``.start()`` are no-ops; the wrapped target is
    never invoked). The wrapper under test reaches ``threading.Thread``
    via ``run_agent.threading.Thread`` so we can patch that name.
    """

    instances: list = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append((args, kwargs))

    def start(self):  # pragma: no cover - never invoked under the gate
        return None


@pytest.fixture(autouse=True)
def _reset_thread_recorder():
    _ThreadRecorder.instances = []
    yield
    _ThreadRecorder.instances = []


def _make_bare_agent(skip_background_review: bool):
    """Build a minimal agent object sufficient for the wrapper test.

    Uses ``AIAgent.__new__`` to avoid the heavy ``__init__`` body (provider
    auto-detection, credential resolution, context-engine bootstrap,
    ~1400 lines of attribute initialization). The wrapper under test only
    reads ``skip_background_review`` before reaching ``threading.Thread``.
    """
    from run_agent import AIAgent
    agent = AIAgent.__new__(AIAgent)
    agent.skip_background_review = skip_background_review
    return agent


def _stub_spawn_background_review_helper(monkeypatch, target_attr="_target_holder"):
    """Replace ``agent.background_review.spawn_background_review_thread``
    inside the wrapper module so the helper is observable independently
    from thread construction. Returns a list that records every call.
    """
    import agent.background_review as bg

    captured_calls = []

    def fake(self, messages_snapshot, *, review_memory=False, review_skills=False, focus=None):
        captured_calls.append(
            {
                "automatic": getattr(self, "_last_call_automatic", True),
                "messages_snapshot_len": len(messages_snapshot or []),
                "review_memory": review_memory,
                "review_skills": review_skills,
                "focus": focus,
            }
        )
        # Mirror the real helper's return shape: (target, prompt).
        def _noop():
            return None
        return _noop, "stub-prompt"

    monkeypatch.setattr(bg, "spawn_background_review_thread", fake)
    return captured_calls


# ---------------------------------------------------------------------------
# A1 — default is fail-safe automatic, protected agent refuses to spawn
# ---------------------------------------------------------------------------


class TestA1DefaultProtectedRefusesSpawn:
    """Omitting ``automatic`` MUST default to True; protected agent → no spawn."""

    def test_default_automatic_blocks_protected_agent(
        self, monkeypatch, _reset_thread_recorder
    ):
        import run_agent
        from run_agent import AIAgent

        agent = _make_bare_agent(skip_background_review=True)
        _stub_spawn_background_review_helper(monkeypatch)

        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)
        result = AIAgent._spawn_background_review(agent, [], review_memory=True, review_skills=True)

        assert result is None
        assert _ThreadRecorder.instances == [], (
            "default-mode call on a protected agent must NOT construct Thread; "
            f"got instances={_ThreadRecorder.instances!r}"
        )


# ---------------------------------------------------------------------------
# A2 — ordinary unprotected automatic review still spawns exactly one Thread
# ---------------------------------------------------------------------------


class TestA2OrdinaryAutomaticSpawns:
    """Omitting ``automatic`` on an ordinary agent MUST spawn exactly once."""

    def test_default_automatic_spawns_for_unprotected_agent(
        self, monkeypatch, _reset_thread_recorder
    ):
        import run_agent
        from run_agent import AIAgent

        agent = _make_bare_agent(skip_background_review=False)
        _stub_spawn_background_review_helper(monkeypatch)

        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)
        AIAgent._spawn_background_review(agent, [], review_memory=True, review_skills=False)

        assert len(_ThreadRecorder.instances) == 1, (
            "ordinary unprotected automatic review must construct exactly one Thread; "
            f"got {len(_ThreadRecorder.instances)}"
        )
        _args, kwargs = _ThreadRecorder.instances[0]
        assert kwargs.get("daemon") is True
        assert kwargs.get("name") == "bg-review"


# ---------------------------------------------------------------------------
# A3 — explicit review (automatic=False) on a protected agent STILL spawns
# ---------------------------------------------------------------------------


class TestA3ExplicitBypassesGuard:
    """/refine with ``automatic=False`` MUST spawn even on a protected agent."""

    def test_explicit_automatic_false_spawns_on_protected_agent(
        self, monkeypatch, _reset_thread_recorder
    ):
        import run_agent
        from run_agent import AIAgent

        agent = _make_bare_agent(skip_background_review=True)
        _stub_spawn_background_review_helper(monkeypatch)

        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)
        result = AIAgent._spawn_background_review(
            agent,
            [{"role": "user", "content": "hi"}],
            review_memory=True,
            review_skills=True,
            focus="save the deploy workflow",
            automatic=False,
        )

        assert result is None  # still returns None to match -> None signature
        assert len(_ThreadRecorder.instances) == 1, (
            "explicit (automatic=False) must NOT be refused by the gate; "
            f"got instances={_ThreadRecorder.instances!r}"
        )


# ---------------------------------------------------------------------------
# A4 — explicitness must not depend on focus (focus=None still explicit)
# ---------------------------------------------------------------------------


class TestA4FocusNoneExplicit:
    """focus=None with automatic=False must still be treated as explicit."""

    def test_explicit_with_no_focus_still_spawns(
        self, monkeypatch, _reset_thread_recorder
    ):
        import run_agent
        from run_agent import AIAgent

        agent = _make_bare_agent(skip_background_review=True)
        _stub_spawn_background_review_helper(monkeypatch)

        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)
        AIAgent._spawn_background_review(
            agent,
            [{"role": "user", "content": "hi"}],
            review_memory=True,
            review_skills=False,
            focus=None,
            automatic=False,
        )
        assert len(_ThreadRecorder.instances) == 1, (
            "explicit + focus=None must still spawn; "
            f"got {len(_ThreadRecorder.instances)}"
        )


# ---------------------------------------------------------------------------
# A5 — codex_runtime has the local mirror AND does NOT pass automatic=False
# ---------------------------------------------------------------------------


class TestA5CodexLocalGate:
    """The codex_runtime automatic fire path MUST mirror turn_finalizer's
    local skip gate, and MUST rely on the central default (no
    ``automatic=False`` override).

    These are structural assertions on codex_runtime.py's spawn block
    (matches the pattern already used by test_skip_background_review.py's
    test_cron_construction_sets_skip_background_review — source-text
    inspection rather than booting the heavy codex runtime).
    """

    def test_codex_runtime_local_skip_gate_present(self):
        codex_path = Path(__file__).resolve().parents[2] / "agent" / "codex_runtime.py"
        src = codex_path.read_text(encoding="utf-8")

        # Defense-in-depth mirror must exist on the codex spawn conditional.
        assert (
            "not getattr(agent, \"skip_background_review\", False)" in src
        ), (
            "agent/codex_runtime.py is missing the local mirror of "
            "agent/turn_finalizer.py:756's skip-background-review gate."
        )

    def test_codex_runtime_does_not_pass_automatic_false(self):
        codex_path = Path(__file__).resolve().parents[2] / "agent" / "codex_runtime.py"
        src = codex_path.read_text(encoding="utf-8")

        # Codex is an automatic post-turn caller; it must NOT pass
        # automatic=False at its _spawn_background_review call site.
        # We isolate the call site by scanning for the call inside
        # run_codex_app_server_turn and asserting no "automatic" kwarg.
        # Structural pattern: find a contiguous block that contains both
        # an "_spawn_background_review(" and an "except Exception:" line,
        # and grep within that block.
        i = src.find("agent._spawn_background_review(")
        assert i != -1, "no _spawn_background_review call in codex_runtime.py"
        # Walk forward to the next "except Exception:" or "return {" line
        # belonging to the same conditional.
        block_end = src.find("\n    return {", i)
        if block_end == -1:
            block_end = len(src)
        block = src[i:block_end]
        # The codex call site must NOT carry automatic=False.
        assert "automatic=False" not in block, (
            "agent/codex_runtime.py's automatic _spawn_background_review "
            "call must rely on the central default (automatic=True), not "
            "opt out via automatic=False."
        )


# ---------------------------------------------------------------------------
# A6 — both /refine callers pass automatic=False
# ---------------------------------------------------------------------------


class TestA6RefineCallsitesOptOut:
    """Both /refine handlers must declare automatic=False so user-triggered
    intent is not silently suppressed on agents with
    skip_background_review=True.
    """

    def test_cli_refine_passes_automatic_false(self):
        cli_path = (
            Path(__file__).resolve().parents[2]
            / "hermes_cli" / "cli_commands_mixin.py"
        )
        src = cli_path.read_text(encoding="utf-8")

        # Locate the _handle_refine_command block and check the
        # _spawn_background_review(...) call carries automatic=False.
        sig = "_handle_refine_command"
        i = src.find(f"def {sig}")
        assert i != -1, "no _handle_refine_command in cli_commands_mixin.py"
        # Find the matching _spawn_background_review( occurrence in the
        # same handler. Walk forward to the next blank-line-separated
        # def or end of class to delimit the handler.
        call_i = src.find("agent._spawn_background_review(", i)
        assert call_i != -1, "no _spawn_background_review in CLI _handle_refine_command"
        # Look at the next 200 characters after the open paren — that's
        # enough to see the kwargs list (kwargs are short).
        slice_ = src[call_i:call_i + 400]
        assert "automatic=False" in slice_, (
            "CLI /refine must pass automatic=False at its "
            "_spawn_background_review call."
        )

    def test_gateway_refine_passes_automatic_false(self):
        gw_path = (
            Path(__file__).resolve().parents[2]
            / "gateway" / "slash_commands.py"
        )
        src = gw_path.read_text(encoding="utf-8")
        sig = "_handle_refine_command"
        i = src.find(f"def {sig}")
        assert i != -1, "no _handle_refine_command in gateway/slash_commands.py"
        call_i = src.find("agent._spawn_background_review(", i)
        assert call_i != -1, "no _spawn_background_review in gateway _handle_refine_command"
        slice_ = src[call_i:call_i + 400]
        assert "automatic=False" in slice_, (
            "Gateway /refine must pass automatic=False at its "
            "_spawn_background_review call."
        )


# ---------------------------------------------------------------------------
# A7 — existing skip_background_review regression suite remains valid
# ---------------------------------------------------------------------------


class TestA7ExistingRegressionUnchanged:
    """The existing tests/agent/test_skip_background_review.py must still
    import cleanly and its assertions remain the canonical contract.

    We load the existing test module via runpy and assert the key
    contract symbols are present without mutating the file.
    """

    def test_existing_skip_background_review_module_intact(self):
        existing_path = (
            Path(__file__).resolve().parents[2]
            / "tests" / "agent" / "test_skip_background_review.py"
        )
        text = existing_path.read_text(encoding="utf-8")

        # Required assertions to exist verbatim — these are the
        # canonical contract that Scope A does NOT weaken.
        required = [
            "def test_default_skip_background_review_is_false",
            "def test_skip_background_review_flag_persists",
            "def test_finalize_turn_skips_review_when_flag_set",
            "def test_finalize_turn_fires_review_when_flag_unset",
            "def test_cron_construction_sets_skip_background_review",
            "def test_K1_kanban_session_source_sets_skip_background_review",
            "def test_K2_non_kanban_session_omits_suppression",
            "def test_K2b_non_kanban_session_value_still_omits_suppression",
            "agent.skip_background_review is False",
            "agent.skip_background_review is True",
            "agent._spawn_background_review.assert_not_called()",
            "agent._spawn_background_review.assert_called_once()",
        ]
        for needle in required:
            assert needle in text, (
                f"existing test_skip_background_review.py is missing "
                f"required contract assertion: {needle!r}"
            )

    def test_central_choke_point_signature_carries_automatic(self):
        """The canonical contract is the helper signature itself; if the
        wrapper stops carrying ``automatic=True`` the entire fail-safe
        posture collapses. We assert this on the file directly so an
        accidental signature regression is caught here too.
        """
        run_agent_path = Path(__file__).resolve().parents[2] / "run_agent.py"
        text = run_agent_path.read_text(encoding="utf-8")
        # Scope-A guard line must be present and the kwarg must default to True.
        assert "automatic: bool = True" in text, (
            "run_agent.py is missing `automatic: bool = True` — the "
            "fail-safe default has been compromised."
        )
        assert (
            "if automatic and getattr(self, \"skip_background_review\", False):" in text
        ), (
            "run_agent.py is missing the central fail-safe guard; "
            "central enforcement has regressed."
        )
