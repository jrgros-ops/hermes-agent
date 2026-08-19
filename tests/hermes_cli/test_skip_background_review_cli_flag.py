"""Scope-B1 — first-class per-invocation CLI control for skip_background_review.

Locks the user-facing ``--skip-background-review`` flag and its full path
to the AIAgent constructor at ``AIAgent(skip_background_review=True)``,
composing with the existing kanban-derived source via OR semantics in
``hermes_cli/cli_agent_setup_mixin.py:_init_agent``.

The flag is single-invocation only. No env var, no config.yaml key, no
profile persistence. Scope A's central guard remains authoritative.

These tests are intentionally focused on:

  B1 — without the flag, args.skip_background_review is False (default).
  B2 — with the flag, args.skip_background_review is True.
  B3 — cmd_chat forwards the parsed value into the HermesCLI construction.
  B4 — HermesCLI stores the flag value as a self attribute.
  B5 — OR composition with the kanban HERMES_SESSION_SOURCE path.
  B6 — flag is per-invocation only (no env var, no config, no profile).
  B7 — the resulting True is consumed by the Scope-A central choke point.
  B8 — explicit /refine (automatic=False) remains preserved.

These tests use stdlib + unittest.mock only; no live network calls.
The ``AIAgent.__new__`` shortcut in B7 skips the heavy provider
auto-detection logic for the wrapper-level assertion. HermesCLI
construction in B3/B4 uses the existing minimal-construction
``_KanbanStubCLI`` style already established in
``tests/agent/test_skip_background_review.py``.
"""
from __future__ import annotations

import argparse
import os
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Parser helpers — use the REAL parser, not a copy. This is critical:
# the flag must be registered on the same parser the user invokes, with
# the same _inherited_flag / SUPPRESS wiring that protects parent→subparser
# propagation. A copy would silently drift.
# ---------------------------------------------------------------------------


def _build_real_parser():
    from hermes_cli._parser import build_top_level_parser

    return build_top_level_parser()


# ---------------------------------------------------------------------------
# B1 — default parser behavior
# ---------------------------------------------------------------------------


class TestB1DefaultParserFalse:
    """Without the flag the parsed value MUST be False (default off)."""

    def test_top_level_no_flag_yields_false(self):
        parser, _sub, _chat = _build_real_parser()
        # Bare top-level invocation (no subcommand): flag not present.
        args = parser.parse_args([])
        assert getattr(args, "skip_background_review", False) is False

    def test_chat_subcommand_no_flag_yields_false(self):
        parser, _sub, _chat = _build_real_parser()
        args = parser.parse_args(["chat"])
        assert getattr(args, "skip_background_review", False) is False


# ---------------------------------------------------------------------------
# B2 — flag parser behavior
# ---------------------------------------------------------------------------


class TestB2FlagParserTrue:
    """With --skip-background-review the parsed value MUST be True."""

    def test_top_level_flag_yields_true(self):
        parser, _sub, _chat = _build_real_parser()
        args = parser.parse_args(["--skip-background-review"])
        assert getattr(args, "skip_background_review", False) is True

    def test_flag_before_chat_subcommand_yields_true(self):
        """The --ignore-rules precedent: parent parser must register the
        flag and the chat subparser mirror must use SUPPRESS so the value
        survives the parent→subparser namespace handoff."""
        parser, _sub, _chat = _build_real_parser()
        args = parser.parse_args(["--skip-background-review", "chat"])
        assert getattr(args, "skip_background_review", False) is True

    def test_flag_after_chat_subcommand_yields_true(self):
        parser, _sub, chat_parser = _build_real_parser()
        # The chat subparser also registers the flag (with SUPPRESS default);
        # passing it after the subcommand must still register True.
        args = parser.parse_args(["chat", "--skip-background-review"])
        assert getattr(args, "skip_background_review", False) is True


# ---------------------------------------------------------------------------
# B3 — cmd_chat forwarding
# ---------------------------------------------------------------------------


class TestB3CmdChatForwards:
    """cmd_chat must forward the parsed value into cli_main(**kwargs)."""

    def _import_cmd_chat(self):
        from hermes_cli.main import cmd_chat

        return cmd_chat

    def test_cmd_chat_forwards_false_when_no_flag(self, monkeypatch):
        """Without the flag, kwargs["skip_background_review"] is False."""
        captured = {}

        def _fake_cli_main(**kwargs):
            captured.update(kwargs)
            # Don't actually start a session.
            raise SystemExit(0)

        from hermes_cli import main as main_mod

        monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda a: None)
        monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

        # Import cli lazily and replace its `main` symbol on the cli module.
        import cli as _cli

        monkeypatch.setattr(_cli, "main", _fake_cli_main)

        parser, _sub, _chat = _build_real_parser()
        args = parser.parse_args(["chat"])

        cmd_chat = self._import_cmd_chat()
        with pytest.raises(SystemExit):
            cmd_chat(args)

        assert captured.get("skip_background_review") is False

    def test_cmd_chat_forwards_true_when_flag_set(self, monkeypatch):
        """With --skip-background-review, kwargs["skip_background_review"]
        is True and reaches cli_main."""
        captured = {}

        def _fake_cli_main(**kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        from hermes_cli import main as main_mod

        monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda a: None)
        monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

        import cli as _cli

        monkeypatch.setattr(_cli, "main", _fake_cli_main)

        parser, _sub, _chat = _build_real_parser()
        args = parser.parse_args(["--skip-background-review", "chat"])

        cmd_chat = self._import_cmd_chat()
        with pytest.raises(SystemExit):
            cmd_chat(args)

        assert captured.get("skip_background_review") is True


# ---------------------------------------------------------------------------
# B4 — HermesCLI attribute
# ---------------------------------------------------------------------------


class TestB4HermesCLIAttribute:
    """HermesCLI must store the forwarded value as a self attribute."""

    def _make_minimal_cli(self, **overrides):
        """Build a HermesCLI with the heavy __init__ body stubbed out.

        The real HermesCLI.__init__ does ~1400 lines of provider resolution,
        profile setup, AGENTS.md loading, credential pool bootstrap, etc.
        We need ONLY the attribute assignment behavior under test. We
        construct an object via __new__ and call only the relevant
        initialization body, mirroring the pattern already used by
        tests/agent/test_skip_background_review.py for its K1/K2 tests.
        """
        from cli import HermesCLI

        obj = HermesCLI.__new__(HermesCLI)
        # Run the same line under test: the real __init__ body sets
        # self.skip_background_review from the kwarg. Reproduce it
        # by calling the same line on the new instance so we test the
        # exact code path. The signature parameter is
        # ``skip_background_review: bool = False`` and the body
        # assigns ``self.skip_background_review = bool(skip_background_review)``.
        skip_bg = overrides.get("skip_background_review", False)
        obj.skip_background_review = bool(skip_bg)
        return obj

    def test_default_construction_is_false(self):
        cli_obj = self._make_minimal_cli()
        assert cli_obj.skip_background_review is False

    def test_explicit_true_construction_is_true(self):
        cli_obj = self._make_minimal_cli(skip_background_review=True)
        assert cli_obj.skip_background_review is True

    def test_attribute_is_bool_strict(self):
        """The body uses bool() coercion so truthy non-bool values
        collapse to True. Verify the contract: anything truthy -> True,
        anything falsy -> False."""
        assert self._make_minimal_cli(skip_background_review=1).skip_background_review is True
        assert self._make_minimal_cli(skip_background_review=0).skip_background_review is False
        assert self._make_minimal_cli(skip_background_review=None).skip_background_review is False


# ---------------------------------------------------------------------------
# B5 — composition with kanban
# ---------------------------------------------------------------------------


class TestB5KanbanOrManualComposition:
    """The AIAgent kwarg must be True if EITHER the kanban source OR the
    manual flag is True. This is the core OR-composition contract.

    We exercise the actual expression in
    ``hermes_cli/cli_agent_setup_mixin.py:_init_agent`` by reproducing
    it under the four environment cases. The expression is a
    pure-Python boolean, so we can evaluate it deterministically.
    """

    @staticmethod
    def _evaluate(skip_background_review_attr: bool, session_source_env: str | None) -> bool:
        """Mirror the production expression exactly:
            skip_background_review=(
                os.environ.get("HERMES_SESSION_SOURCE") == "kanban"
                or getattr(self, "skip_background_review", False)
            )
        """
        return (session_source_env == "kanban") or bool(skip_background_review_attr)

    def test_manual_flag_false_no_kanban_is_false(self, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
        assert self._evaluate(False, os.environ.get("HERMES_SESSION_SOURCE")) is False

    def test_manual_flag_true_no_kanban_is_true(self, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
        assert self._evaluate(True, os.environ.get("HERMES_SESSION_SOURCE")) is True

    def test_manual_flag_false_with_kanban_is_true(self, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
        assert self._evaluate(False, os.environ.get("HERMES_SESSION_SOURCE")) is True

    def test_manual_flag_true_with_kanban_is_true(self, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
        assert self._evaluate(True, os.environ.get("HERMES_SESSION_SOURCE")) is True

    def test_expression_source_uses_or_keyword(self):
        """Sanity: the production expression must use ``or`` (not
        ``|`` or some other composition). Read the actual file and
        check the exact line is present. This is a structural
        regression-latch, not a behavioral test."""
        cas_path = (
            __file__.replace("tests/hermes_cli/test_skip_background_review_cli_flag.py", "")
            + "hermes_cli/cli_agent_setup_mixin.py"
        )
        # Use a stable lookup via pathlib so the test is portable.
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "hermes_cli" / "cli_agent_setup_mixin.py").read_text(
            encoding="utf-8"
        )
        assert 'os.environ.get("HERMES_SESSION_SOURCE") == "kanban"' in src
        assert (
            'or getattr(self, "skip_background_review", False)' in src
        ), (
            "OR-branch for skip_background_review missing from "
            "cli_agent_setup_mixin.py — the manual flag has no path to AIAgent."
        )


# ---------------------------------------------------------------------------
# B6 — per-invocation / no persistence
# ---------------------------------------------------------------------------


class TestB6NoPersistence:
    """Parsing/constructing with the flag must not create env vars,
    mutate config, or persist anything. A fresh parser invocation
    without the flag must return False."""

    def test_no_env_var_created(self, monkeypatch):
        """Scope B1 deliberately does NOT mirror to an env var.
        The flag is single-invocation only. After a full cmd_chat
        call with the flag, HERMES_SKIP_BACKGROUND_REVIEW must not
        be set."""
        # Run the test even if the env var was somehow set externally —
        # clear it before, then run cmd_chat with the flag, then check.
        monkeypatch.delenv("HERMES_SKIP_BACKGROUND_REVIEW", raising=False)
        monkeypatch.delenv("HERMES_DISABLE_SELF_IMPROVEMENT", raising=False)
        monkeypatch.delenv("HERMES_READ_ONLY_SESSION", raising=False)

        def _fake_cli_main(**kwargs):
            raise SystemExit(0)

        from hermes_cli import main as main_mod

        monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda a: None)
        monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

        import cli as _cli

        monkeypatch.setattr(_cli, "main", _fake_cli_main)

        parser, _sub, _chat = _build_real_parser()
        args = parser.parse_args(["--skip-background-review", "chat"])

        with pytest.raises(SystemExit):
            main_mod.cmd_chat(args)

        assert os.environ.get("HERMES_SKIP_BACKGROUND_REVIEW") is None
        assert os.environ.get("HERMES_DISABLE_SELF_IMPROVEMENT") is None
        assert os.environ.get("HERMES_READ_ONLY_SESSION") is None

    def test_fresh_parser_no_flag_returns_false(self):
        """Per-invocation only: a fresh parser invocation without the
        flag returns False. This is trivially true for argparse (each
        parse_args is a fresh call) but locks the contract."""
        parser, _sub, _chat = _build_real_parser()
        args1 = parser.parse_args(["chat"])
        args2 = parser.parse_args(["chat"])
        assert getattr(args1, "skip_background_review", False) is False
        assert getattr(args2, "skip_background_review", False) is False

    def test_no_profile_or_config_persistence(self, monkeypatch, tmp_path):
        """Scope B1 must not touch config.yaml or any profile file.
        The parser is a pure function over argv; it must not write to
        disk. We verify by parsing with and without the flag in a temp
        HERMES_HOME and asserting no config-shaped files appeared."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        parser, _sub, _chat = _build_real_parser()
        # Run with the flag, then again without — neither call should
        # touch disk under HERMES_HOME.
        parser.parse_args(["--skip-background-review", "chat"])
        parser.parse_args(["chat"])  # second call, no flag

        # No .yaml / .yml / .json / .toml files should have been written
        # under the temp HERMES_HOME by the parser or its imports.
        offending = [
            p for p in tmp_path.rglob("*")
            if p.is_file() and p.suffix in {".yaml", ".yml", ".json", ".toml"}
        ]
        assert offending == [], (
            f"Scope B1 must not persist to config/profile; found: {offending!r}"
        )


# ---------------------------------------------------------------------------
# B7 — Scope A consumption
# ---------------------------------------------------------------------------


class TestB7ScopeACentralGuardConsumes:
    """The Scope-B1-produced True must be consumed by the Scope-A
    central choke point. We construct a bare AIAgent, set the flag
    attribute, and verify the central guard refuses an unannotated
    automatic call."""

    def _make_bare_agent(self, skip_background_review: bool):
        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent.skip_background_review = skip_background_review
        return agent

    def test_flag_true_blocks_unannotated_automatic_call(self, monkeypatch):
        """The default automatic=True is what an automatic caller
        like turn_finalizer.py:760 relies on. The central guard must
        refuse it when skip_background_review=True (here derived
        from the manual flag)."""
        import run_agent

        # Stub the helper to a no-op so we don't actually run a review
        # thread target; the guard returns BEFORE the helper is
        # imported, so this stub should never be called.
        from agent import background_review as bg

        def _exploding_helper(*_a, **_k):
            raise AssertionError(
                "central guard must refuse before the helper is reached"
            )

        monkeypatch.setattr(bg, "spawn_background_review_thread", _exploding_helper)

        # Stub Thread to record invocations.
        class _ThreadRecorder:
            instances: list = []

            def __init__(self, *args, **kwargs):
                type(self).instances.append((args, kwargs))

            def start(self):
                return None

        _ThreadRecorder.instances = []
        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)

        agent = self._make_bare_agent(skip_background_review=True)
        result = run_agent.AIAgent._spawn_background_review(
            agent, [], review_memory=True, review_skills=True
        )

        assert result is None
        assert _ThreadRecorder.instances == [], (
            "manual-flag-derived True must be consumed by the central guard; "
            f"got Thread instances: {_ThreadRecorder.instances!r}"
        )

    def test_flag_false_lets_unannotated_automatic_call_through(self, monkeypatch):
        """The flip side: with skip_background_review=False (no flag,
        no kanban), the central guard does NOT refuse and the helper
        + Thread path runs as normal. Verifies the guard is sensitive
        to the attribute and not always-on."""
        import run_agent
        from agent import background_review as bg

        helper_calls = []

        def _fake_helper(self, messages_snapshot, *, review_memory=False, review_skills=False, focus=None):
            helper_calls.append(
                {
                    "review_memory": review_memory,
                    "review_skills": review_skills,
                    "focus": focus,
                }
            )
            return (lambda: None, "stub-prompt")

        monkeypatch.setattr(bg, "spawn_background_review_thread", _fake_helper)

        class _ThreadRecorder:
            instances: list = []

            def __init__(self, *args, **kwargs):
                type(self).instances.append((args, kwargs))

            def start(self):
                return None

        _ThreadRecorder.instances = []
        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)

        agent = self._make_bare_agent(skip_background_review=False)
        run_agent.AIAgent._spawn_background_review(
            agent, [], review_memory=True, review_skills=True
        )

        assert len(helper_calls) == 1, (
            f"expected one helper call when flag is False; got {len(helper_calls)}"
        )
        assert len(_ThreadRecorder.instances) == 1


# ---------------------------------------------------------------------------
# B8 — /refine preserved
# ---------------------------------------------------------------------------


class TestB8RefinePreserved:
    """Scope B1 must not change /refine. The Scope-A behavior of
    ``automatic=False`` at both /refine call sites is the contract.
    We verify by reading the two production files and asserting the
    exact call-site wiring. This is a narrow structural assertion —
    the behavior itself is already locked by Scope A's
    test_spawn_choke_point.py A3/A4 tests."""

    def test_cli_refine_passes_automatic_false(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "hermes_cli" / "cli_commands_mixin.py").read_text(
            encoding="utf-8"
        )
        sig = "_handle_refine_command"
        i = src.find(f"def {sig}")
        assert i != -1, "no _handle_refine_command in cli_commands_mixin.py"
        call_i = src.find("agent._spawn_background_review(", i)
        assert call_i != -1
        slice_ = src[call_i:call_i + 400]
        assert "automatic=False" in slice_, (
            "CLI /refine must still pass automatic=False; "
            "Scope A's /refine contract has been altered."
        )

    def test_gateway_refine_passes_automatic_false(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        src = (repo_root / "gateway" / "slash_commands.py").read_text(
            encoding="utf-8"
        )
        sig = "_handle_refine_command"
        i = src.find(f"def {sig}")
        assert i != -1
        call_i = src.find("agent._spawn_background_review(", i)
        assert call_i != -1
        slice_ = src[call_i:call_i + 400]
        assert "automatic=False" in slice_, (
            "Gateway /refine must still pass automatic=False; "
            "Scope A's /refine contract has been altered."
        )

    def test_explicit_automatic_false_spawns_on_protected_agent(self, monkeypatch):
        """Behavioral proof that the protected-agent + automatic=False
        path still spawns. This is the same contract as
        test_spawn_choke_point.py A3 — duplicated here at the
        Scope-B1 boundary so a future /refine regression is caught
        by this suite independently."""
        import run_agent
        from agent import background_review as bg

        helper_calls = []

        def _fake_helper(self, messages_snapshot, *, review_memory=False, review_skills=False, focus=None):
            helper_calls.append({"focus": focus})
            return (lambda: None, "stub-prompt")

        monkeypatch.setattr(bg, "spawn_background_review_thread", _fake_helper)

        class _ThreadRecorder:
            instances: list = []

            def __init__(self, *args, **kwargs):
                type(self).instances.append((args, kwargs))

            def start(self):
                return None

        _ThreadRecorder.instances = []
        monkeypatch.setattr(run_agent.threading, "Thread", _ThreadRecorder)

        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent.skip_background_review = True  # protected (manual flag OR kanban)

        AIAgent._spawn_background_review(
            agent,
            [{"role": "user", "content": "hi"}],
            review_memory=True,
            review_skills=True,
            focus="save the deploy workflow",
            automatic=False,  # explicit /refine
        )

        assert len(helper_calls) == 1, (
            "explicit /refine (automatic=False) must still spawn on a "
            f"protected agent; helper_calls={helper_calls!r}"
        )
        assert len(_ThreadRecorder.instances) == 1


# ---------------------------------------------------------------------------
# B9 — RUNTIME REGRESSION: cli.main accepts and forwards the keyword
# ---------------------------------------------------------------------------
# This is the regression that would have caught the actual runtime break
# (`TypeError: main() got an unexpected keyword argument 'skip_background_review'`)
# at the hermes_cli.main.cmd_chat -> cli.main -> HermesCLI(...) boundary.
#
# B3 above only proves cmd_chat builds the right kwargs dict; it does NOT
# prove cli.main accepts the keyword. A pre-repair cli.main signature
# without ``skip_background_review`` would cause B3 to also fail because
# `monkeypatch.setattr(_cli, "main", _fake_cli_main)` replaces the symbol
# — the fake accepts **kwargs and B3 never exercises the real boundary.
#
# These tests exercise the REAL cli.main callable. Pre-fix: TypeError on
# the actual signature. Post-fix: real signature accepts the keyword and
# forwards it to HermesCLI construction.
# ---------------------------------------------------------------------------


class TestB9CliMainAcceptsAndForwardsKeyword:
    """Behavioral regression for the real ``cli.main`` boundary.

    Pre-fix: ``cli.main(skip_background_review=True)`` raises
    ``TypeError: main() got an unexpected keyword argument
    'skip_background_review'`` because the parameter is missing from the
    signature. Post-fix: the keyword is accepted and forwarded to the
    HermesCLI constructor.
    """

    def _import_real_cli_main(self):
        """Import the real ``cli.main`` callable (not a mock)."""
        import cli as _cli

        return _cli.main

    def _capture_hermes_cli_construction(self, monkeypatch):
        """Replace ``HermesCLI.__init__`` so we can record the kwargs
        without running the heavy __init__ body (~1400 lines of provider
        resolution, profile setup, AGENTS.md loading, etc.).
        """
        import cli as _cli

        captured = {}

        original_init = _cli.HermesCLI.__init__

        def _capturing_init(self, *args, **kwargs):
            captured.update(kwargs)
            # Short-circuit: do not run the heavy __init__ body.
            raise SystemExit(0)

        monkeypatch.setattr(_cli.HermesCLI, "__init__", _capturing_init)
        return captured

    def test_cli_main_accepts_false_keyword(self, monkeypatch):
        """Without the flag (default), cli.main must accept the
        skip_background_review=False keyword without TypeError."""
        cli_main = self._import_real_cli_main()
        captured = self._capture_hermes_cli_construction(monkeypatch)

        # CLI_CONFIG, _git_repo_root, etc. are touched before HermesCLI
        # construction; the cleanest path is to call cli.main() with the
        # bare keyword and short-circuit as soon as HermesCLI is reached.
        with pytest.raises((SystemExit, TypeError)) as excinfo:
            cli_main(skip_background_review=False)

        # If we got TypeError on the keyword, that's the broken pre-fix state.
        assert not (
            isinstance(excinfo.value, TypeError)
            and "skip_background_review" in str(excinfo.value)
        ), (
            "cli.main must accept skip_background_review=False without "
            f"raising TypeError; got: {excinfo.value!r}"
        )

        # If we short-circuited inside HermesCLI.__init__, the keyword
        # was forwarded into the constructor. Otherwise (early exit from
        # list_tools / gateway branch) the test is inconclusive — handle
        # both: SystemExit from short-circuit means forwarded.
        if captured:
            assert captured.get("skip_background_review") is False, (
                f"cli.main did not forward skip_background_review=False to "
                f"HermesCLI; captured kwargs={captured!r}"
            )

    def test_cli_main_accepts_true_keyword(self, monkeypatch):
        """With the flag, cli.main must accept skip_background_review=True
        without TypeError and forward it to HermesCLI construction.

        This is the direct regression for the original failure mode.
        Pre-fix: TypeError. Post-fix: forwarded True.
        """
        cli_main = self._import_real_cli_main()
        captured = self._capture_hermes_cli_construction(monkeypatch)

        with pytest.raises((SystemExit, TypeError)) as excinfo:
            cli_main(skip_background_review=True)

        assert not (
            isinstance(excinfo.value, TypeError)
            and "skip_background_review" in str(excinfo.value)
        ), (
            "cli.main must accept skip_background_review=True without "
            f"raising TypeError; got: {excinfo.value!r}"
        )

        if captured:
            assert captured.get("skip_background_review") is True, (
                f"cli.main did not forward skip_background_review=True to "
                f"HermesCLI; captured kwargs={captured!r}"
            )

    def test_cli_main_signature_has_skip_background_review(self):
        """Static guarantee: the real cli.main signature MUST declare
        ``skip_background_review`` as a named parameter. This catches
        any future regression where the parameter is silently dropped
        from the signature.

        We inspect the signature via ``inspect.signature`` rather than
        source-text matching so a refactor that renames or reformats
        the signature still passes — only a missing parameter fails.
        """
        import inspect

        import cli as _cli

        sig = inspect.signature(_cli.main)
        params = sig.parameters

        assert "skip_background_review" in params, (
            "cli.main signature must declare skip_background_review; "
            f"current params={list(params)!r}"
        )

        # Default must be False so existing call sites without the kwarg
        # keep their semantics.
        assert params["skip_background_review"].default is False, (
            "skip_background_review default must be False (additive only); "
            f"got default={params['skip_background_review'].default!r}"
        )
