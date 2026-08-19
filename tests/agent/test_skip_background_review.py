"""Tests for the skip_background_review constructor flag.

Verifies that AIAgent can be instructed to skip the end-of-turn
_spawn_background_review fork (~30K tokens / event), which is essential
on cron sessions that have no human-in-the-loop value from skill/memory
review forks.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from run_agent import AIAgent
from agent.turn_finalizer import finalize_turn


def _make_agent(skip_background_review: bool = False) -> AIAgent:
    """Construct a minimally-configured AIAgent for unit testing."""
    return AIAgent(
        model="openai/gpt-4o-mini",
        provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=skip_background_review,
        platform="cli",
    )


def _stub_agent_for_finalize(agent: AIAgent) -> None:
    """Stub the heavy finalizer dependencies to isolate the review gate."""
    agent._spawn_background_review = MagicMock()
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    agent._persist_session = MagicMock()
    agent._session_messages = []
    agent._file_mutation_verifier_enabled = lambda: False
    agent.clear_interrupt = MagicMock()
    agent._stream_callback = None
    agent._sync_external_memory_for_turn = MagicMock()
    agent._skill_nudge_interval = 10
    agent._iters_since_skill = 20  # exceeds nudge interval → _should_review_skills = True
    agent.valid_tool_names = {"skill_manage"}
    agent.iteration_budget = MagicMock()
    agent.iteration_budget.remaining = 100
    agent.iteration_budget.used = 5
    agent.iteration_budget.max_total = 100
    agent.max_iterations = 50
    agent._emit_status = MagicMock()
    agent._safe_print = MagicMock()
    agent._apply_persist_user_message_override = MagicMock()
    agent.context_compressor = None
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    agent.model = "test-model"
    agent.session_id = "test-session"
    agent.quiet_mode = True
    agent._turn_failed_file_mutations = {}
    agent._db_flush_scan_prefix = None


def _run_finalize(agent: AIAgent) -> None:
    """Call finalize_turn with conditions that would trigger background review."""
    finalize_turn(
        agent,
        final_response="ok",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "assistant", "content": "ok"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="test-turn",
        user_message="test",
        original_user_message="test",
        _should_review_memory=True,
        _turn_exit_reason="text_response(1)",
    )


def test_default_skip_background_review_is_false() -> None:
    """Without an explicit override, AIAgent does NOT skip background review."""
    agent = _make_agent()
    assert agent.skip_background_review is False


def test_skip_background_review_flag_persists() -> None:
    """Passing skip_background_review=True records the flag on the instance."""
    agent = _make_agent(skip_background_review=True)
    assert agent.skip_background_review is True


def test_finalize_turn_skips_review_when_flag_set() -> None:
    """finalize_turn must NOT call _spawn_background_review when skip_background_review=True.

    Exercises the actual finalizer call path (not a duplicated guard expression)
    so it catches divergence between the production guard and the test.
    """
    agent = _make_agent(skip_background_review=True)
    _stub_agent_for_finalize(agent)
    _run_finalize(agent)
    agent._spawn_background_review.assert_not_called()


def test_finalize_turn_fires_review_when_flag_unset() -> None:
    """Counterpart: with the flag off, finalize_turn DOES call _spawn_background_review."""
    agent = _make_agent(skip_background_review=False)
    _stub_agent_for_finalize(agent)
    _run_finalize(agent)
    agent._spawn_background_review.assert_called_once()


def test_cron_construction_sets_skip_background_review() -> None:
    """The cron scheduler MUST construct AIAgent with skip_background_review=True.

    Verified via source-text inspection — the cron scheduler is heavy to
    boot in tests, so we assert that the source declares the flag rather
    than running the scheduler. This catches accidental removal.
    """
    import pathlib

    scheduler_src = pathlib.Path(__file__).resolve().parents[2] / "cron" / "scheduler.py"
    text = scheduler_src.read_text(encoding="utf-8")

    assert "skip_background_review=True" in text, (
        "cron/scheduler.py must construct AIAgent with skip_background_review=True."
    )


# ----------------------------------------------------------------------
# V1.3-B — Kanban worker background-review suppression
#
# K1: HERMES_SESSION_SOURCE=kanban -> AIAgent(skip_background_review=True)
#     reaches the production AIAgent(...) kwargs at HermesCLI._init_agent
#     in hermes_cli/cli_agent_setup_mixin.py.
# K2: HERMES_SESSION_SOURCE absent (or non-kanban) -> ordinary CLI
#     construction does NOT receive the Kanban suppression semantics.
#
# These tests exercise the real production constructor boundary. The
# ``HermesCLI.__init__`` is heavy (rich banners, profile resolution,
# credential pool, AGENTS.md loader, etc.) so we instantiate a minimal
# stub class that includes CLIAgentSetupMixin, monkey-patch the heavy
# bootstrap helpers inside ``_init_agent``, and monkey-patch
# ``cli.AIAgent`` (the wrapper imported lazily inside the mixin) so we
# can record the kwargs reaching the real AIAgent(...) call site without
# touching a real provider.
# ----------------------------------------------------------------------


class _KanbanStubCLI:  # noqa: D401 - test stub, not production
    """Minimal stub carrying the attributes ``_init_agent`` reads.

    Only the attributes referenced by the AIAgent(...) call site are
    populated. ``_init_agent`` is the bound method on CLIAgentSetupMixin,
    so it sees this stub as ``self``.
    """

    # Bootstrap state
    agent = None
    _session_db = None
    _resumed = False
    conversation_history = []

    # AIAgent kwargs
    model = ""
    api_key = ""
    base_url = ""
    provider = ""
    requested_provider = ""
    api_mode = ""
    acp_command = ""
    acp_args = None
    max_tokens = None
    max_turns = None
    enabled_toolsets = None
    disabled_toolsets = None
    verbose = False
    tool_progress_mode = "all"
    system_prompt = ""
    prefill_messages = None
    reasoning_config = None
    service_tier = None
    _providers_only = None
    _providers_ignore = None
    _providers_order = None
    _provider_sort = None
    _provider_require_params = None
    _provider_data_collection = None
    _openrouter_min_coding_score = None
    session_id = None
    _clarify_callback = None
    _current_reasoning_callback = None
    _fallback_model = None
    _on_thinking = None
    checkpoints_enabled = False
    checkpoint_max_snapshots = None
    checkpoint_max_total_size_mb = None
    checkpoint_max_file_size_mb = None
    pass_session_id = False
    ignore_rules = False
    _on_tool_progress = None
    _on_tool_start = None
    _inline_diffs_enabled = False
    _on_tool_complete = None
    _stream_delta = None
    streaming_enabled = False
    _on_tool_gen_start = None
    _on_notice = None
    _on_notice_clear = None
    _on_reaction = None

    # HermesCLI-bound bootstrap methods referenced by _init_agent. These
    # normally live on HermesCLI in cli.py; we provide no-op equivalents
    # on the stub so the production constructor path runs end-to-end
    # without invoking the real bootstrap (Tirith, plugin loader, etc.).
    def _install_tool_callbacks(self):  # noqa: D401 - stub no-op
        return None

    def _ensure_tirith_security(self):  # noqa: D401 - stub no-op
        return None

    def _ensure_runtime_credentials(self):  # noqa: D401 - stub no-op
        return True

    def _current_reasoning_callback(self):  # noqa: D401 - stub no-op
        return None


def _run_init_agent(stub):
    """Invoke the production ``_init_agent`` and return the captured kwargs.

    Some post-construction bookkeeping (credits seeding, pending-title
    application) may raise inside the ``except Exception`` block of
    ``_init_agent`` — these raise after the AIAgent(...) call has
    already returned and its kwargs have already been captured by the
    ``_fake_aiagent`` stub. We swallow any exception so the test can
    inspect ``captured`` regardless of which post-construction branch
    fired; the canonical contract under test is the AIAgent kwarg shape,
    not the post-construction bookkeeping.
    """
    try:
        stub._init_agent()
    except Exception:
        # Already captured at this point; surface the captured kwargs.
        pass


def _build_kanban_stub() -> _KanbanStubCLI:
    """Return a ``_KanbanStubCLI`` instance that carries CLIAgentSetupMixin."""
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    # Mixin methods (``self._init_agent`` etc.) are bound to ``self`` on
    # first attribute access; we don't need a full HermesCLI instance.
    stub = _KanbanStubCLI()
    stub.__class__ = type(
        "KanbanStubWithSetup",
        (CLIAgentSetupMixin,),
        dict(_KanbanStubCLI.__dict__),
    )
    return stub


def _patch_init_agent_dependencies(monkeypatch, captured_aiagent_kwargs):
    """Stub the heavy bootstrap helpers so ``_init_agent`` can run end-to-end.

    Captures the kwargs of the AIAgent(...) call into the provided dict,
    keyed by ``"kwargs"``. All helpers are no-ops or harmless stubs.

    The mixin imports every helper it calls lazily via
    ``from cli import ...`` at the top of ``_init_agent`` (see line 342).
    We therefore patch the symbols on the ``cli`` module, which is where
    the lazy ``from cli import ...`` will resolve them at call time.
    """
    import cli as cli_mod
    from hermes_cli import mcp_startup as mcp_startup_mod

    def _fake_aiagent(*args, **kwargs):
        captured_aiagent_kwargs["args"] = args
        captured_aiagent_kwargs["kwargs"] = kwargs
        return object()  # never used; caller stores it on self.agent

    monkeypatch.setattr(cli_mod, "AIAgent", _fake_aiagent)

    # The mixin's ``from cli import ...`` resolves _prepare_deferred_agent_startup
    # at call time. Patch the symbol at the ``cli`` module level so the lazy
    # import picks up the stub. The other bootstrap helpers
    # (``_install_tool_callbacks``, ``_ensure_tirith_security``,
    # ``_ensure_runtime_credentials``, ``_current_reasoning_callback``) are
    # HermesCLI instance methods and are provided as no-ops on the stub
    # class itself, so they do not need module-level patching.
    monkeypatch.setattr(
        cli_mod, "_prepare_deferred_agent_startup", lambda: None, raising=False
    )

    monkeypatch.setattr(
        mcp_startup_mod,
        "ensure_mcp_discovery_before_agent_build",
        lambda **kwargs: None,
    )

    # Stub out the lazy-imported display helpers used inside the
    # resumed-session branch. We do not enter that branch (self._resumed
    # is False), but the symbols must be resolvable when the method runs
    # ``from cli import AIAgent, ChatConsole, _DIM, _RST, _accent_hex,
    # _cprint, _prepare_deferred_agent_startup, logger``. Patch them on
    # the cli module to harmless no-ops so any branch that DOES touch
    # them still works.
    monkeypatch.setattr(cli_mod, "_DIM", "", raising=False)
    monkeypatch.setattr(cli_mod, "_RST", "", raising=False)
    monkeypatch.setattr(
        cli_mod, "_accent_hex", lambda: "", raising=False
    )
    monkeypatch.setattr(
        cli_mod, "_cprint", lambda *a, **kw: None, raising=False
    )
    monkeypatch.setattr(cli_mod, "ChatConsole", object, raising=False)


def test_K1_kanban_session_source_sets_skip_background_review(monkeypatch):
    """K1: HERMES_SESSION_SOURCE=kanban -> AIAgent(skip_background_review=True).

    Drives the production ``HermesCLI._init_agent`` constructor boundary
    inside ``hermes_cli/cli_agent_setup_mixin.py``. The captured kwargs
    reaching the AIAgent(...) call site MUST include
    ``skip_background_review=True`` exactly when the dispatcher-set
    environment provenance is ``kanban``.
    """
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    # Pin HERMES_HOME so the bootstrap does not touch a real profile.
    import tempfile
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="k1-test-"))

    captured: dict = {}
    stub = _build_kanban_stub()
    _patch_init_agent_dependencies(monkeypatch, captured)

    _run_init_agent(stub)

    assert "kwargs" in captured, (
        "AIAgent(...) was not invoked by HermesCLI._init_agent under "
        "HERMES_SESSION_SOURCE=kanban; the production constructor path "
        "bypassed the canonical call site."
    )
    kwargs = captured["kwargs"]
    assert kwargs.get("skip_background_review") is True, (
        f"expected AIAgent(skip_background_review=True) under "
        f"HERMES_SESSION_SOURCE=kanban; got skip_background_review="
        f"{kwargs.get('skip_background_review')!r}; full kwargs: {kwargs!r}"
    )


def test_K2_non_kanban_session_omits_suppression(monkeypatch):
    """K2: ordinary non-Kanban CLI does NOT receive the Kanban suppression.

    With HERMES_SESSION_SOURCE absent (or any value other than 'kanban'),
    the production ``HermesCLI._init_agent`` MUST construct AIAgent
    WITHOUT the Kanban skip_background_review=True gate. The invariant
    is that ordinary human CLI sessions are never silently downgraded
    into skipping background review.
    """
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    import tempfile
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="k2-test-"))

    captured: dict = {}
    stub = _build_kanban_stub()
    _patch_init_agent_dependencies(monkeypatch, captured)

    _run_init_agent(stub)

    assert "kwargs" in captured, (
        "AIAgent(...) was not invoked by HermesCLI._init_agent under "
        "absent HERMES_SESSION_SOURCE; the production constructor path "
        "bypassed the canonical call site."
    )
    kwargs = captured["kwargs"]
    # The contract is "ordinary CLI sessions must NOT become
    # skip_background_review=True". The current implementation passes
    # the explicit expression ``os.environ.get(...) == 'kanban'`` which
    # yields False here. Accept any value that is not truthy.
    flag = kwargs.get("skip_background_review")
    assert not flag, (
        f"ordinary non-Kanban CLI must NOT receive "
        f"skip_background_review=True; got skip_background_review="
        f"{flag!r}; full kwargs: {kwargs!r}"
    )


def test_K2b_non_kanban_session_value_still_omits_suppression(monkeypatch):
    """K2b: any non-'kanban' HERMES_SESSION_SOURCE keeps the suppression off.

    Defends against the gate being accidentally broadened to any
    truthy value. A 'cli' source — what a normal chat -q run produces —
    must NOT inherit the Kanban suppression semantics either.
    """
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cli")
    import tempfile
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="k2b-test-"))

    captured: dict = {}
    stub = _build_kanban_stub()
    _patch_init_agent_dependencies(monkeypatch, captured)

    _run_init_agent(stub)

    assert "kwargs" in captured
    kwargs = captured["kwargs"]
    flag = kwargs.get("skip_background_review")
    assert not flag, (
        f"HermesCLI._init_agent must NOT raise skip_background_review "
        f"for HERMES_SESSION_SOURCE='cli'; got {flag!r}"
    )
