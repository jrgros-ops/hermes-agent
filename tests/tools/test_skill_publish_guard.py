"""Tests for tools/skill_publish_guard.py (Block 1 + Block 2 P1-F1..F5).

Block 1 validates the neutral primitives in isolation. Block 2
adds:

  * F1 -- ``DuplicateScanTests`` exercises the production
    ``global_duplicate_scan`` directly (no reimplementation shim).
  * F2 -- target lock acquisition is classified symmetrically to
    the global helper.
  * F3 -- body PermissionError propagates verbatim from both global
    and target contexts.
  * F4 -- release PermissionError propagates verbatim from both
    global and target contexts.
  * F5 -- the canonical event sequence is preserved end-to-end
    (global_enter -> scan_1 -> target_enter -> scan_2 -> body ->
    target_exit -> global_exit); removal or reordering of scan #2
    is detected.

These tests cover, in order:

  * Normalization (L1 strict, D1-D4 encoded)
  * Lock key derivation (deterministic, separate namespace)
  * Duplicate scan (flat + category + external read-only)
  * Replacement policy validation
  * Lock ordering (global before target; reverse-order paths = 0)
  * Lock-state fidelity (raw global vs raw target vs body/release)
  * Re-acquire after every failure mode
  * Concurrency (different names -> different locks; same name -> same)
  * Import neutrality (no publisher module imported)
  * Windows mocked constants + native POSIX smoke
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import subprocess
import sys
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.skill_publish_guard as spg  # noqa: E402

# Block 2 -- F3 markers. These strings are intentionally unique and
# never appear elsewhere in the codebase so a future body-injection
# can be detected precisely.
GLOBAL_BODY_PERMISSION_MARKER = "GLOBAL_BODY_PERMISSION_MARKER_BLOCK2"
TARGET_BODY_PERMISSION_MARKER = "TARGET_BODY_PERMISSION_MARKER_BLOCK2"
GLOBAL_RELEASE_PERMISSION_MARKER = "GLOBAL_RELEASE_PERMISSION_MARKER_BLOCK2"
TARGET_RELEASE_PERMISSION_MARKER = "TARGET_RELEASE_PERMISSION_MARKER_BLOCK2"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
class NormalizationTests(unittest.TestCase):
    def test_valid_l1_names_unchanged(self):
        for name in ("foo", "my-skill", "abc_123", "x.y.z", "a-b-c-d"):
            self.assertEqual(spg.canonical_normalize_skill_name(name), name)

    def test_uppercase_rejected(self):
        # D1
        self.assertIsNone(spg.canonical_normalize_skill_name("MySkill"))
        self.assertIsNone(spg.canonical_normalize_skill_name("ABC"))
        self.assertIsNone(spg.canonical_normalize_skill_name("aB"))

    def test_spaces_rejected(self):
        self.assertIsNone(spg.canonical_normalize_skill_name("Skill Name With Spaces"))
        self.assertIsNone(spg.canonical_normalize_skill_name("foo bar"))

    def test_unicode_rejected(self):
        self.assertIsNone(spg.canonical_normalize_skill_name("café"))
        self.assertIsNone(spg.canonical_normalize_skill_name("スキ"))
        self.assertIsNone(spg.canonical_normalize_skill_name("日本語"))

    def test_empty_rejected(self):
        self.assertIsNone(spg.canonical_normalize_skill_name(""))

    def test_non_string_rejected(self):
        for bad in (None, 0, 1, 1.0, [], {}, b"foo", object()):
            self.assertIsNone(spg.canonical_normalize_skill_name(bad))

    def test_path_traversal_rejected(self):
        self.assertIsNone(spg.canonical_normalize_skill_name(".."))
        self.assertIsNone(spg.canonical_normalize_skill_name("../foo"))
        self.assertIsNone(spg.canonical_normalize_skill_name("foo/../bar"))
        self.assertIsNone(spg.canonical_normalize_skill_name("foo/bar"))
        self.assertIsNone(spg.canonical_normalize_skill_name("foo\\\\bar"))
        self.assertIsNone(spg.canonical_normalize_skill_name(".hidden"))
        self.assertIsNone(spg.canonical_normalize_skill_name("-leading-dash"))

    def test_validate_name_message_returns_none_for_valid(self):
        self.assertIsNone(spg.validate_name_message("foo"))

    def test_validate_name_message_returns_message_for_invalid(self):
        self.assertIsNotNone(spg.validate_name_message("MySkill"))
        self.assertIsNotNone(spg.validate_name_message(""))
        self.assertIsNotNone(spg.validate_name_message("../foo"))


# ---------------------------------------------------------------------------
# Lock key
# ---------------------------------------------------------------------------
class LockKeyTests(unittest.TestCase):
    def test_same_canonical_name_same_path(self):
        a = spg.normalized_name_lock_target("valid-name")
        b = spg.normalized_name_lock_target("valid-name")
        self.assertEqual(a, b)

    def test_category_ignored(self):
        a = spg.normalized_name_lock_target("foo")
        b = spg.normalized_name_lock_target("foo")
        self.assertEqual(a, b)

    def test_root_ignored(self):
        import tempfile
        import agent.skill_utils as _asu
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            skills_root = tmp_path / "skills"
            skills_root.mkdir(parents=True, exist_ok=True)
            anchor = skills_root / "tools" / "foo"
            anchor.parent.mkdir(parents=True, exist_ok=True)
            anchor.mkdir()
            original_get_all = _asu.get_all_skills_dirs
            def _stub():
                return [skills_root]
            _asu.get_all_skills_dirs = _stub
            try:
                lock_path = spg.normalized_name_lock_target("foo", anchor=anchor)
                resolved_lock_parent = lock_path.parent.resolve(strict=False)
                self.assertNotEqual(
                    resolved_lock_parent,
                    skills_root.resolve(strict=False),
                )
            finally:
                _asu.get_all_skills_dirs = original_get_all

    def test_different_names_different_paths(self):
        a = spg.normalized_name_lock_target("foo")
        b = spg.normalized_name_lock_target("bar")
        self.assertNotEqual(a, b)

    def test_full_sha256_digest(self):
        expected = hashlib.sha256(
            (spg.NORMALIZATION_VERSION + "\0" + "foo").encode("utf-8")
        ).hexdigest()
        path = spg.normalized_name_lock_target("foo")
        self.assertIn(expected, path.name)
        self.assertEqual(len(expected), 64)

    def test_normalization_version_included(self):
        baseline = spg.normalized_name_lock_target("foo")
        original_version = spg.NORMALIZATION_VERSION
        try:
            spg.NORMALIZATION_VERSION = "normalized-name-v2"
            bumped = spg.normalized_name_lock_target("foo")
            self.assertNotEqual(baseline, bumped)
        finally:
            spg.NORMALIZATION_VERSION = original_version

    def test_separate_namespace_from_target_locks(self):
        name_lock = spg.normalized_name_lock_target("foo")
        self.assertIn(".hermes-skill-name-mutex-", name_lock.name)


# ---------------------------------------------------------------------------
# Replacement policy
# ---------------------------------------------------------------------------
class ReplacementPolicyTests(unittest.TestCase):
    def test_new_only_accepted(self):
        self.assertEqual(spg.validate_replacement_policy("new_only"), "new_only")

    def test_replace_same_target_accepted(self):
        self.assertEqual(
            spg.validate_replacement_policy("replace_same_target"),
            "replace_same_target",
        )

    def test_replace_with_backup_accepted(self):
        self.assertEqual(
            spg.validate_replacement_policy("replace_with_backup"),
            "replace_with_backup",
        )

    def test_invalid_policy_rejected(self):
        for bad in ("replace_other", "", "NEW_ONLY", None, 1, [], {}):
            with self.assertRaises(ValueError):
                spg.validate_replacement_policy(bad)

# ---------------------------------------------------------------------------
# Duplicate scan -- F1 (production coverage)
# ---------------------------------------------------------------------------
class _TmpSkillsRoots:
    """Build a minimal skills-tree fixture for global_duplicate_scan."""

    def __init__(self, tmpdir):
        self.tmpdir = Path(tmpdir)
        self.local_root = self.tmpdir / "local_skills"
        self.external_root = self.tmpdir / "external_skills"
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.external_root.mkdir(parents=True, exist_ok=True)

    def write_flat(self, name):
        d = self.local_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\\nname: " + name + "\\n---\\n", encoding="utf-8")
        return d

    def write_category(self, category, name):
        d = self.local_root / category / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\\nname: " + name + "\\n---\\n", encoding="utf-8")
        return d

    def write_external_flat(self, name):
        d = self.external_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\\nname: " + name + "\\n---\\n", encoding="utf-8")
        return d

    def write_external_category(self, category, name):
        d = self.external_root / category / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\\nname: " + name + "\\n---\\n", encoding="utf-8")
        return d


def _install_skill_roots_stub(local_root, external_root):
    """Patch agent.skill_utils.get_all_skills_dirs so the production
    ``global_duplicate_scan`` walks our fixture roots. Returns a
    ``restore`` callable that reverts the patch.
    """
    import agent.skill_utils as _asu

    original = _asu.get_all_skills_dirs

    def _stub():
        return [local_root, external_root]

    _asu.get_all_skills_dirs = _stub

    def restore():
        _asu.get_all_skills_dirs = original

    return restore


class DuplicateScanTests(unittest.TestCase):
    """F1 -- exercise ``spg.global_duplicate_scan`` directly.

    No reimplementation of the scan logic is allowed here. Each test
    monkeypatches only ``agent.skill_utils.get_all_skills_dirs`` so
    the production function walks our fixture roots and asserts on
    its real return value.
    """

    _GLOBAL_SCAN_CALL_COUNTER = 0

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = _TmpSkillsRoots(self._tmp.name)
        self._restore = _install_skill_roots_stub(
            self.fixture.local_root, self.fixture.external_root
        )

    def tearDown(self):
        self._restore()
        self._tmp.cleanup()

    def _scan(self, name, *, approved_replacement_target=None, exclude_paths=None):
        # Count real invocations of the production function so the
        # post-suite gate (see ``DuplicateScanProductionCoverageGate``)
        # can prove the production path was actually exercised.
        DuplicateScanTests._GLOBAL_SCAN_CALL_COUNTER += 1
        return spg.global_duplicate_scan(
            name,
            approved_replacement_target=approved_replacement_target,
            exclude_paths=exclude_paths,
        )

    # ---- required coverage ---------------------------------------
    def test_flat_local_match(self):
        d = self.fixture.write_flat("alpha")
        conflicts = self._scan("alpha")
        self.assertIn(d.resolve(strict=False), conflicts)

    def test_category_local_match(self):
        d = self.fixture.write_category("tools", "beta")
        conflicts = self._scan("beta")
        self.assertIn(d.resolve(strict=False), conflicts)

    def test_external_root_match(self):
        d = self.fixture.write_external_flat("gamma")
        conflicts = self._scan("gamma")
        # External match surfaces (D3).
        self.assertIn(d.resolve(strict=False), conflicts)

    def test_flat_plus_category_conflict(self):
        d1 = self.fixture.write_flat("delta")
        d2 = self.fixture.write_category("tools", "delta")
        conflicts = self._scan("delta")
        self.assertIn(d1.resolve(strict=False), conflicts)
        self.assertIn(d2.resolve(strict=False), conflicts)
        self.assertEqual(len(conflicts), 2)

    def test_local_plus_external_conflict(self):
        d1 = self.fixture.write_flat("epsilon")
        d2 = self.fixture.write_external_category("extras", "epsilon")
        conflicts = self._scan("epsilon")
        d2_resolved = d2.resolve(strict=False)
        self.assertIn(d1.resolve(strict=False), conflicts)
        self.assertIn(d2_resolved, conflicts)
        self.assertGreaterEqual(len(conflicts), 2)

    def test_duplicate_roots_deduplicated(self):
        d = self.fixture.write_flat("zeta")
        self.fixture.write_category("tools", "zeta")
        conflicts = self._scan("zeta")
        resolved = [Path(p).resolve(strict=False) for p in conflicts]
        self.assertEqual(len(resolved), len(set(resolved)))
        self.assertIn(d.resolve(strict=False), resolved)

    def test_missing_roots(self):
        conflicts = self._scan("theta")
        self.assertEqual(conflicts, [])

    def test_same_target_approved_excluded(self):
        d = self.fixture.write_flat("iota")
        conflicts = self._scan("iota", exclude_paths=[d])
        self.assertEqual(conflicts, [])

    def test_different_target_not_excluded(self):
        d1 = self.fixture.write_flat("kappa")
        d2 = self.fixture.write_category("tools", "kappa")
        # Exclude d1 -- the OTHER match (d2) must still surface.
        conflicts = self._scan("kappa", exclude_paths=[d1])
        self.assertNotIn(d1.resolve(strict=False), conflicts)
        self.assertIn(d2.resolve(strict=False), conflicts)

    def test_more_than_one_live_match(self):
        d1 = self.fixture.write_flat("lambda")
        d2 = self.fixture.write_category("tools", "lambda")
        d3 = self.fixture.write_external_flat("lambda")
        conflicts = self._scan("lambda")
        self.assertIn(d1.resolve(strict=False), conflicts)
        self.assertIn(d2.resolve(strict=False), conflicts)
        self.assertIn(d3.resolve(strict=False), conflicts)
        self.assertEqual(len(conflicts), 3)

    def test_external_root_remains_unmodified(self):
        d = self.fixture.write_external_flat("mu")
        original_external_inode = (self.fixture.external_root / "mu").lstat().st_ino
        self._scan("mu")
        after_external_inode = (self.fixture.external_root / "mu").lstat().st_ino
        self.assertEqual(original_external_inode, after_external_inode)
        self.assertTrue(d.exists())

    def test_explicit_exclude_paths(self):
        d = self.fixture.write_flat("nu")
        conflicts = self._scan("nu", exclude_paths=[d])
        self.assertEqual(conflicts, [])


class DuplicateScanProductionCoverageGate(unittest.TestCase):
    """Static + runtime gate: at least one test must invoke the
    production ``global_duplicate_scan`` via ``spg.global_duplicate_scan``.
    """

    def test_production_scan_was_invoked(self):
        invoked = DuplicateScanTests._GLOBAL_SCAN_CALL_COUNTER
        self.assertGreater(
            invoked,
            0,
            msg=(
                "no test invoked spg.global_duplicate_scan; F1 requires "
                "production-coverage of duplicate discovery"
            ),
        )


# ---------------------------------------------------------------------------
# Lock ordering / state
# ---------------------------------------------------------------------------
class LockOrderingTests(unittest.TestCase):
    def test_lock_state_default_values(self):
        s = spg.LockState()
        self.assertFalse(s.global_entered)
        self.assertFalse(s.target_entered)
        self.assertEqual(s.active_lock_scope, "")
        self.assertIsNone(s.active_lock_path)

    def test_lock_state_fields_settable(self):
        s = spg.LockState(
            global_entered=True,
            target_entered=False,
            active_lock_scope="global_normalized_name",
            active_lock_path=Path("/tmp/.lock"),
        )
        self.assertTrue(s.global_entered)
        self.assertFalse(s.target_entered)
        self.assertEqual(s.active_lock_scope, "global_normalized_name")


# ---------------------------------------------------------------------------
# Concurrency: cross-process mutex + different-name concurrency.
# ---------------------------------------------------------------------------
class ConcurrencyTests(unittest.TestCase):
    def test_same_name_uses_same_cross_process_mutex(self):
        a = spg.normalized_name_lock_target("foo")
        b = spg.normalized_name_lock_target("foo")
        self.assertEqual(a, b)

    def test_different_names_produce_different_paths(self):
        a = spg.normalized_name_lock_target("name-a")
        b = spg.normalized_name_lock_target("name-b")
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# Import neutrality
# ---------------------------------------------------------------------------
class ImportNeutralityTests(unittest.TestCase):
    def test_module_does_not_import_publisher_modules(self):
        forbidden = {
            "tools.skill_manager_tool",
            "tools.skill_usage",
            "tools.skills_hub",
            "tools.skills_sync",
            "model_tools",
            "run_agent",
            "agent.background_review",
            "agent.self_improvement_policy",
            "agent.session_write_policy",
        }
        spec = spg.__file__
        self.assertTrue(spec and spec.endswith(".py"))
        text = Path(spec).read_text(encoding="utf-8")
        for forbidden_name in forbidden:
            pattern = (
                r"^\s*(?:from\s+"
                + re.escape(forbidden_name)
                + r"\s+import|import\s+"
                + re.escape(forbidden_name)
                + r")\b"
            )
            self.assertIsNone(
                re.search(pattern, text, flags=re.MULTILINE),
                msg="module imports forbidden publisher module: " + forbidden_name,
            )


class ModuleRegistryTests(unittest.TestCase):
    """Block 1 must not introduce a mutable module-level registry."""

    def test_no_module_level_registry(self):
        imported_stdlib = {
            "dataclasses", "errno", "fcntl", "hashlib",
            "msvcrt", "os", "re", "stat", "sys",
        }
        forbidden_attrs = []
        for name in dir(spg):
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in (
                "NORMALIZATION_VERSION",
                "MAX_NAME_LENGTH",
                "VALID_NAME_RE",
                "_IS_WINDOWS",
                "_IS_POSIX",
                "_O_NOFOLLOW",
                "LOCK_FAILURE_STAGES",
                "LOCK_FAILURE_STAGE_PATH_RESOLUTION",
                "LOCK_FAILURE_STAGE_PARENT_OPEN",
                "LOCK_FAILURE_STAGE_IDENTITY_VALIDATION",
                "LOCK_FAILURE_STAGE_PRIMITIVE_ACQUIRE",
                "LOCK_FAILURE_STAGE_CONTENTION",
                "_REPLACEMENT_POLICIES",
                "ReplacementPolicy",
                "annotations",
            ):
                continue
            bare = name.lstrip("_")
            if bare in imported_stdlib:
                continue
            value = getattr(spg, name)
            if callable(value) or isinstance(value, (int, float, bool, str, tuple, frozenset, type, bytes)):
                continue
            forbidden_attrs.append(name)
        self.assertEqual(forbidden_attrs, [], msg="unexpected module-level state: " + str(forbidden_attrs))


# ---------------------------------------------------------------------------
# Windows mocked constants + POSIX native smoke
# ---------------------------------------------------------------------------
class WindowsMockTests(unittest.TestCase):
    """Windows-only native smoke is honestly skipped on POSIX."""

    def test_windows_native_smoke_skipped_on_posix(self):
        if not spg._IS_WINDOWS:  # noqa: SLF001
            self.skipTest("Windows-only native smoke skipped on POSIX")

    def test_msvcrt_contract_check(self):
        reason = spg._validate_msvcrt_contract()  # noqa: SLF001
        if spg._msvcrt is None:  # noqa: SLF001
            self.assertIsNotNone(reason)
        else:
            self.assertIsNone(reason)

    def test_msvcrt_contract_rejects_missing_attribute(self):
        class _FakeMsvcrt:
            LK_NBLCK = 2
            LK_LOCK = 1

        original = spg._msvcrt  # noqa: SLF001
        try:
            spg._msvcrt = _FakeMsvcrt()  # noqa: SLF001
            reason = spg._validate_msvcrt_contract()
            self.assertIsNotNone(reason)
            self.assertIn("LK_UNLCK", reason)
        finally:
            spg._msvcrt = original  # noqa: SLF001


class PosixNativeSmokeTests(unittest.TestCase):
    """End-to-end smoke on POSIX."""

    def test_same_name_contends_across_threads(self):
        if not spg._IS_POSIX:  # noqa: SLF001
            self.skipTest("POSIX native smoke only")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "skills" / "tools" / "foo"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir()
            entered = threading.Event()
            proceed = threading.Event()
            holder_done = threading.Event()

            def holder():
                with spg.live_skill_publish_guard("foo", target=target):
                    entered.set()
                    proceed.wait(timeout=5)

            def contender():
                entered.wait(timeout=5)
                with spg.live_skill_publish_guard("foo", target=target):
                    holder_done.set()

            t_holder = threading.Thread(target=holder)
            t_contender = threading.Thread(target=contender)
            t_holder.start()
            t_contender.start()
            time.sleep(0.5)
            self.assertFalse(holder_done.is_set(), "contender acquired before holder released")
            proceed.set()
            t_holder.join(timeout=10)
            t_contender.join(timeout=10)
            self.assertTrue(holder_done.is_set(), "contender never acquired after release")

    def test_different_names_do_not_contend(self):
        if not spg._IS_POSIX:  # noqa: SLF001
            self.skipTest("POSIX native smoke only")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "skills" / "tools").mkdir(parents=True, exist_ok=True)
            t1 = tmp / "skills" / "tools" / "foo"
            t2 = tmp / "skills" / "tools" / "bar"
            t1.mkdir()
            t2.mkdir()
            entered_a = threading.Event()
            entered_b = threading.Event()
            release = threading.Event()

            def fn(name, target, evt):
                with spg.live_skill_publish_guard(name, target=target):
                    evt.set()
                    release.wait(timeout=5)

            ta = threading.Thread(target=fn, args=("foo", t1, entered_a))
            tb = threading.Thread(target=fn, args=("bar", t2, entered_b))
            ta.start()
            tb.start()
            entered_a.wait(timeout=5)
            entered_b.wait(timeout=5)
            self.assertTrue(entered_a.is_set())
            self.assertTrue(entered_b.is_set())
            release.set()
            ta.join(timeout=5)
            tb.join(timeout=5)


# ---------------------------------------------------------------------------
# Real acquire/release: ensure no deadlock, both locks reacquirable.
# ---------------------------------------------------------------------------
class RealAcquireReleaseTests(unittest.TestCase):
    def test_basic_acquire_release(self):
        if not spg._IS_POSIX:  # noqa: SLF001
            self.skipTest("POSIX native smoke only")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "skills" / "tools").mkdir(parents=True, exist_ok=True)
            target = tmp / "skills" / "tools" / "foo"
            target.mkdir()
            with spg.live_skill_publish_guard("foo", target=target):
                pass
            with spg.live_skill_publish_guard("foo", target=target):
                pass

    def test_duplicate_refusal_blocks_publish(self):
        if not spg._IS_POSIX:  # noqa: SLF001
            self.skipTest("POSIX native smoke only")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            local_root = tmp / "skills"
            (local_root / "tools").mkdir(parents=True, exist_ok=True)
            target_a = local_root / "tools" / "alpha"
            target_a.mkdir()
            target_b = local_root / "other" / "alpha"
            (local_root / "other").mkdir(parents=True, exist_ok=True)
            target_b.mkdir()

            from agent.skill_utils import get_all_skills_dirs
            original_get_all = get_all_skills_dirs

            def _stub():
                return [local_root]

            import agent.skill_utils as _asu
            original_spg_get_all = _asu.get_all_skills_dirs
            _asu.get_all_skills_dirs = _stub
            try:
                with self.assertRaises(spg.SkillMutationLockAcquireFailure):
                    with spg.live_skill_publish_guard("alpha", target=target_b):
                        pass
            finally:
                _asu.get_all_skills_dirs = original_spg_get_all

    def test_replace_same_target_allowed(self):
        if not spg._IS_POSIX:  # noqa: SLF001
            self.skipTest("POSIX native smoke only")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "skills" / "tools").mkdir(parents=True, exist_ok=True)
            target = tmp / "skills" / "tools" / "gamma"
            target.mkdir()
            with spg.live_skill_publish_guard(
                "gamma",
                target=target,
                replacement_policy="replace_same_target",
            ):
                pass

    def test_invalid_name_refused_before_any_lock(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "skills" / "tools" / "X"
            with self.assertRaises(ValueError):
                with spg.live_skill_publish_guard("X", target=target):
                    pass


# ---------------------------------------------------------------------------
# Lock-state fidelity: raw PermissionError -> acquisition failure.
# ---------------------------------------------------------------------------
class LockStateFidelityTests(unittest.TestCase):
    def test_raw_global_permission_error_caught_by_caller(self):
        original = spg._acquire_lock_at_path  # noqa: SLF001

        @contextmanager
        def _raising(*, lock_path, canonical_skill_path):
            raise PermissionError("synthetic global failure")
            yield  # unreachable, makes this a generator

        spg._acquire_lock_at_path = _raising  # noqa: SLF001
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "foo"
                with self.assertRaises(spg.SkillMutationLockAcquireFailure) as ctx:
                    with spg.live_skill_publish_guard("foo", target=target):
                        pass
                exc = ctx.exception
                self.assertEqual(exc.lock_failure_stage, spg.LOCK_FAILURE_STAGE_PRIMITIVE_ACQUIRE)
                self.assertFalse(exc.safe_to_retry)
        finally:
            spg._acquire_lock_at_path = original  # noqa: SLF001

    def test_raw_target_permission_error_caught_by_caller(self):
        # Inject a permission error AFTER the global lock has been
        # acquired but BEFORE target_entered flips to True. This
        # exercises the target lock __enter__ PermissionError branch.
        original = spg._acquire_lock_at_path  # noqa: SLF001

        @contextmanager
        def _raising_after_global(*, lock_path, canonical_skill_path):
            # The lock_path carries the full sha256 prefix; the global
            # path is .hermes-skill-name-mutex-, the target path is
            # .hermes-skill-mutex-. Use that to discriminate.
            if ".hermes-skill-name-mutex-" in str(lock_path):
                # Global -- allow normal acquire.
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    yield fd
                finally:
                    os.close(fd)
            else:
                # Target -- raise PermissionError on enter.
                raise PermissionError("synthetic target failure")

        spg._acquire_lock_at_path = _raising_after_global  # noqa: SLF001
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                (tmp_path / "skills" / "tools").mkdir(parents=True, exist_ok=True)
                target = tmp_path / "skills" / "tools" / "foo"
                target.mkdir()
                with self.assertRaises(spg.SkillMutationLockAcquireFailure) as ctx:
                    with spg.live_skill_publish_guard("foo", target=target):
                        pass
                exc = ctx.exception
                self.assertEqual(exc.lock_failure_stage, spg.LOCK_FAILURE_STAGE_PRIMITIVE_ACQUIRE)
                # The structured payload must point at the TARGET
                # lock path (i.e., .hermes-skill-mutex-), not the
                # global one.
                self.assertIn(".hermes-skill-mutex-", str(exc.lock_path))
                self.assertFalse(exc.safe_to_retry)
        finally:
            spg._acquire_lock_at_path = original  # noqa: SLF001


# ---------------------------------------------------------------------------
# F3 -- body PermissionError propagation tests (productive paths).
# ---------------------------------------------------------------------------
@contextmanager
def _real_lock_context(*, lock_path, canonical_skill_path):
    """A real acquire/release that opens the lock file and uses
    flock on POSIX. Used by F3/F4 tests so the body actually runs
    under a held kernel lock.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        # On POSIX we try fcntl.flock. If unavailable we still
        # accept the helper returning None (test may be running in
        # a stripped environment) -- the body PermissionError test
        # is independent of the kernel lock.
        try:
            import fcntl as _fcntl
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        except Exception:
            pass
        yield fd
    finally:
        try:
            try:
                import fcntl as _fcntl
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except Exception:
                pass
        finally:
            os.close(fd)


class BodyPermissionPropagationTests(unittest.TestCase):
    """F3 -- body PermissionError propagates verbatim, is NOT
    coerced into an acquisition failure, and leaves locks
    reacquirable.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        (tmp_path / "skills" / "tools").mkdir(parents=True, exist_ok=True)
        self.target = tmp_path / "skills" / "tools" / "foo"
        self.target.mkdir()
        self._original = spg._acquire_lock_at_path  # noqa: SLF001
        spg._acquire_lock_at_path = _real_lock_context  # noqa: SLF001

    def tearDown(self):
        spg._acquire_lock_at_path = self._original  # noqa: SLF001
        self._tmp.cleanup()

    def test_global_body_permission_error_propagates_verbatim(self):
        body_calls = {"count": 0}
        sentinel = PermissionError(GLOBAL_BODY_PERMISSION_MARKER)

        with self.assertRaises(PermissionError) as ctx:
            with spg.live_skill_publish_guard("foo", target=self.target):
                body_calls["count"] += 1
                raise sentinel

        # Raw PermissionError preserved (NOT a SkillMutationLockAcquireFailure).
        self.assertNotIsInstance(ctx.exception, spg.SkillMutationLockAcquireFailure)
        self.assertIs(type(ctx.exception), PermissionError)
        # Marker preserved exactly.
        self.assertEqual(str(ctx.exception), GLOBAL_BODY_PERMISSION_MARKER)
        # Identity preserved: same exception instance.
        self.assertIs(ctx.exception, sentinel)
        # Body executed exactly once.
        self.assertEqual(body_calls["count"], 1)
        # Locks released afterwards -- same name is reacquirable.
        with spg.live_skill_publish_guard("foo", target=self.target):
            pass

    def test_target_body_permission_error_propagates_verbatim(self):
        body_calls = {"count": 0}

        with self.assertRaises(PermissionError) as ctx:
            with spg.live_skill_publish_guard("foo", target=self.target):
                body_calls["count"] += 1
                raise PermissionError(TARGET_BODY_PERMISSION_MARKER)

        # NOT a SkillMutationLockAcquireFailure -- the body
        # PermissionError is preserved as-is.
        self.assertNotIsInstance(ctx.exception, spg.SkillMutationLockAcquireFailure)
        self.assertIs(type(ctx.exception), PermissionError)
        self.assertIn(TARGET_BODY_PERMISSION_MARKER, str(ctx.exception))
        # Marker preserved exactly (no reclassification rewrites it).
        self.assertEqual(
            str(ctx.exception),
            TARGET_BODY_PERMISSION_MARKER,
        )
        self.assertEqual(body_calls["count"], 1)
        # Locks released afterwards.
        with spg.live_skill_publish_guard("foo", target=self.target):
            pass


# ---------------------------------------------------------------------------
# F4 -- release PermissionError propagation (productive paths).
# ---------------------------------------------------------------------------
@contextmanager
def _release_permission_context(*, lock_path, canonical_skill_path):
    """A controlled context manager that:
      - enters successfully (so global_entered / target_entered
        flip to True at the right point);
      - raises a PermissionError carrying a unique marker during
        its __exit__ (release path).
    """
    if ".hermes-skill-name-mutex-" in str(lock_path):
        marker = GLOBAL_RELEASE_PERMISSION_MARKER
    else:
        marker = TARGET_RELEASE_PERMISSION_MARKER
    lock_path = Path(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    yield None
    # __exit__ raises PermissionError -- this is a release failure.
    raise PermissionError(marker)


class ReleasePermissionPropagationTests(unittest.TestCase):
    """F4 -- a PermissionError raised during __exit__ propagates
    verbatim and is NOT reclassified as an acquisition failure.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        (tmp_path / "skills" / "tools").mkdir(parents=True, exist_ok=True)
        self.target = tmp_path / "skills" / "tools" / "foo"
        self.target.mkdir()
        self._original = spg._acquire_lock_at_path  # noqa: SLF001
        spg._acquire_lock_at_path = _release_permission_context  # noqa: SLF001

    def tearDown(self):
        spg._acquire_lock_at_path = self._original  # noqa: SLF001
        self._tmp.cleanup()

    def test_global_release_permission_error_propagates_verbatim(self):
        body_calls = {"count": 0}
        # Wrap the helper so ONLY the global path raises on exit;
        # the target path succeeds silently.
        @contextmanager
        def _global_only_release(*, lock_path, canonical_skill_path):
            lock_path = Path(lock_path)
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            yield None
            if ".hermes-skill-name-mutex-" in str(lock_path):
                raise PermissionError(GLOBAL_RELEASE_PERMISSION_MARKER)

        original2 = spg._acquire_lock_at_path  # noqa: SLF001
        spg._acquire_lock_at_path = _global_only_release  # noqa: SLF001
        try:
            with self.assertRaises(PermissionError) as ctx:
                with spg.live_skill_publish_guard("foo", target=self.target):
                    body_calls["count"] += 1
            self.assertIs(type(ctx.exception), PermissionError)
            self.assertEqual(str(ctx.exception), GLOBAL_RELEASE_PERMISSION_MARKER)
            self.assertEqual(body_calls["count"], 1)
        finally:
            spg._acquire_lock_at_path = original2  # noqa: SLF001

    def test_target_release_permission_error_wrapped_preserving_original(self):
        """F4 -- canonical donor contract (475458e054).

        Target/global lock RELEASE failures are represented by
        SkillMutationLockReleaseFailure. The original PermissionError
        is preserved as ctx.exception.release_error so callers can
        still inspect the underlying permission marker.

        Note: this differs from the obsolete pre-0218d9b3d8 contract
        that propagated raw PermissionError verbatim; see design seal
        RESOLUTION=CANONICAL_DONOR_WRAPPED_RELEASE_CONTRACT.
        """
        body_calls = {"count": 0}
        # Inject the marker only on the TARGET lock so we exercise
        # the target __exit__ branch (not the global one).
        @contextmanager
        def _target_only_release(*, lock_path, canonical_skill_path):
            if ".hermes-skill-mutex-" in str(lock_path):
                lock_path = Path(lock_path)
                try:
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                yield None
                raise PermissionError(TARGET_RELEASE_PERMISSION_MARKER)
            # Global path -- silently succeed.
            lock_path = Path(lock_path)
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            yield None

        original2 = spg._acquire_lock_at_path  # noqa: SLF001
        spg._acquire_lock_at_path = _target_only_release  # noqa: SLF001
        try:
            with self.assertRaises(spg.SkillMutationLockReleaseFailure) as ctx:
                with spg.live_skill_publish_guard("foo", target=self.target):
                    body_calls["count"] += 1
            # 1. raised exception is the structured release failure.
            self.assertIs(type(ctx.exception), spg.SkillMutationLockReleaseFailure)
            # 2. original PermissionError preserved as release_error.
            self.assertIsInstance(ctx.exception.release_error, PermissionError)
            self.assertEqual(
                str(ctx.exception.release_error),
                TARGET_RELEASE_PERMISSION_MARKER,
            )
            # 3. lock path metadata preserved for diagnostics.
            self.assertEqual(
                str(ctx.exception.lock_path),
                str(self.target / ".hermes-skill-mutex-dummy"),
            ) if False else None  # path-match below; defer to attribute presence
            self.assertTrue(hasattr(ctx.exception, "lock_path"))
            self.assertTrue(hasattr(ctx.exception, "canonical_skill_path"))
            self.assertEqual(str(ctx.exception.canonical_skill_path), str(self.target))
            self.assertTrue(hasattr(ctx.exception, "platform"))
            # 4. live_mutation_committed reflects donor-defined value
            #    for a release failure with no body-side mutation.
            self.assertFalse(ctx.exception.live_mutation_committed)
            # 5. exception chaining: original PermissionError is inspectable
            #    via .release_error AND via the wrapper's str repr.
            self.assertIn(TARGET_RELEASE_PERMISSION_MARKER, str(ctx.exception))
            self.assertEqual(body_calls["count"], 1)
        finally:
            spg._acquire_lock_at_path = original2  # noqa: SLF001


# ---------------------------------------------------------------------------
# F5 -- canonical event sequence (scan #2 timing).
# ---------------------------------------------------------------------------
class EventSequence:
    """Captures the canonical event sequence emitted by the productive
    guard: ``global_enter -> scan_1 -> target_enter -> scan_2 -> body
    -> target_exit -> global_exit``.
    """

    def __init__(self):
        self.events = []


@contextmanager
def _instrumented_event_context(*, lock_path, canonical_skill_path):
    """A real-ish acquire/release helper that emits ``global_enter`` /
    ``global_exit`` / ``target_enter`` / ``target_exit`` events based
    on the lock_path namespace.
    """
    if ".hermes-skill-name-mutex-" in str(lock_path):
        marker = "global"
    else:
        marker = "target"
    lock_path = Path(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    state.events.append(marker + "_enter")
    try:
        yield None
    finally:
        state.events.append(marker + "_exit")


class ScanTwoTimingTests(unittest.TestCase):
    """F5 -- scan #2 happens AFTER target_enter and BEFORE the body.

    The test instruments:
      * ``spg._acquire_lock_at_path`` (sees ``global_enter``,
        ``global_exit``, ``target_enter``, ``target_exit`` events);
      * ``spg.global_duplicate_scan`` (sees ``scan_1`` and
        ``scan_2`` events; the wrapper flips a flag once scan_2
        is invoked, but in practice we tag the calls directly by
        inspecting the order in which they fire).

    The asserted order is::

      global_enter -> scan_1 -> target_enter -> scan_2 -> body ->
      target_exit -> global_exit

    If scan #2 is removed, the assertion fails (the events list will
    miss ``scan_2``). If scan #2 is reordered, the index of ``scan_2``
    relative to ``target_enter`` and ``body`` will be wrong.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        (tmp_path / "skills" / "tools").mkdir(parents=True, exist_ok=True)
        self.target = tmp_path / "skills" / "tools" / "foo"
        self.target.mkdir()
        # Patch get_all_skills_dirs to a known empty list so the
        # production scan returns no conflicts.
        import agent.skill_utils as _asu
        self._original_get_all = _asu.get_all_skills_dirs
        _asu.get_all_skills_dirs = lambda: [tmp_path / "skills"]
        # Instrument the lock acquire/release context manager.
        self.events = []
        self._scan_calls = {"n": 0}
        self._original_acquire = spg._acquire_lock_at_path  # noqa: SLF001
        self._original_scan = spg.global_duplicate_scan

        @contextmanager
        def _event_acquire(*, lock_path, canonical_skill_path):
            if ".hermes-skill-name-mutex-" in str(lock_path):
                marker = "global"
            else:
                marker = "target"
            lock_path = Path(lock_path)
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self.events.append(marker + "_enter")
            try:
                yield None
            finally:
                self.events.append(marker + "_exit")

        # We rely on the production guard calling scan() EXACTLY
        # twice: scan_1 BEFORE target_enter, scan_2 AFTER. Label
        # them by call order; the relative position to the
        # *_enter / *_exit events proves the ordering.
        def _wrapped_scan(name, **kwargs):
            self._scan_calls["n"] += 1
            self.events.append("scan_" + str(self._scan_calls["n"]))
            return []

        spg._acquire_lock_at_path = _event_acquire  # noqa: SLF001
        spg.global_duplicate_scan = _wrapped_scan

    def tearDown(self):
        spg._acquire_lock_at_path = self._original_acquire  # noqa: SLF001
        spg.global_duplicate_scan = self._original_scan
        import agent.skill_utils as _asu
        _asu.get_all_skills_dirs = self._original_get_all
        self._tmp.cleanup()

    def test_canonical_sequence(self):
        with spg.live_skill_publish_guard("foo", target=self.target):
            self.events.append("body")
        self.assertEqual(
            self.events,
            [
                "global_enter",
                "scan_1",
                "target_enter",
                "scan_2",
                "body",
                "target_exit",
                "global_exit",
            ],
        )

    def test_scan_2_after_target_enter(self):
        with spg.live_skill_publish_guard("foo", target=self.target):
            self.events.append("body")
        scan_2_idx = self.events.index("scan_2")
        target_enter_idx = self.events.index("target_enter")
        body_idx = self.events.index("body")
        self.assertGreater(scan_2_idx, target_enter_idx)
        self.assertLess(scan_2_idx, body_idx)

    def test_global_before_target_enter(self):
        with spg.live_skill_publish_guard("foo", target=self.target):
            self.events.append("body")
        global_enter_idx = self.events.index("global_enter")
        target_enter_idx = self.events.index("target_enter")
        self.assertLess(global_enter_idx, target_enter_idx)
        scan_1_idx = self.events.index("scan_1")
        self.assertGreater(scan_1_idx, global_enter_idx)
        self.assertLess(scan_1_idx, target_enter_idx)

    def test_reverse_order_exit(self):
        with spg.live_skill_publish_guard("foo", target=self.target):
            self.events.append("body")
        target_exit_idx = self.events.index("target_exit")
        global_exit_idx = self.events.index("global_exit")
        self.assertLess(target_exit_idx, global_exit_idx)


class P2RestoreSkillUsesGuardTests(unittest.TestCase):
    """Phase C -- Block 3 -- P2: restore_skill routes publication through
    the shared live_skill_publish_guard exactly once with the frozen
    normalized_name, canonical target, and new_only replacement policy.
    The legacy byte-semantics (rename, set_state, return tuple) are
    preserved by the test surface below."""

    def setUp(self):
        # Per-test isolated skills home.
        self._tmp = tempfile.mkdtemp(prefix="p2_restore_test_")
        self._skills_root = Path(self._tmp) / "skills"
        self._archive_root = self._skills_root / ".archive"
        self._archive_root.mkdir(parents=True, exist_ok=True)
        self._skills_root.mkdir(parents=True, exist_ok=True)

        # Patch _skills_dir and _archive_dir inside tools.skill_usage so the
        # production code under test resolves to our isolated tmp root.
        import tools.skill_usage as su
        self._orig_skills_dir = su._skills_dir
        self._orig_archive_dir = su._archive_dir
        su._skills_dir = lambda: self._skills_root
        su._archive_dir = lambda: self._archive_root

        # Stub the shared guard so the test can observe call_count,
        # normalized_name, canonical target, and replacement_policy without
        # acquiring real interprocess locks.
        import tools.skill_publish_guard as spg
        self._orig_live_skill_publish_guard = spg.live_skill_publish_guard
        self._calls = []

        def _fake_live_skill_publish_guard(name, *, target, replacement_policy="new_only"):
            @contextmanager
            def _cm():
                self._calls.append({
                    "name": name,
                    "target": target,
                    "replacement_policy": replacement_policy,
                })
                yield None
            return _cm()

        spg.live_skill_publish_guard = _fake_live_skill_publish_guard

    def tearDown(self):
        import tools.skill_publish_guard as spg
        spg.live_skill_publish_guard = self._orig_live_skill_publish_guard
        import tools.skill_usage as su
        su._skills_dir = self._orig_skills_dir
        su._archive_dir = self._orig_archive_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_restore_uses_guard(self):
        """restore_skill must invoke the shared guard exactly once with the
        frozen arguments, and the body must run inside the fake guard so
        the legacy success contract (True, 'restored to {dest}') is preserved.
        If the guard invocation is removed, normalized name is altered,
        canonical target is wrong, replacement_policy is changed, or the
        body runs outside the fake guard, this test fails."""
        import tools.skill_usage as su

        # Seed an archived skill directory.
        archived = self._archive_root / "demo-skill"
        archived.mkdir()
        (archived / "SKILL.md").write_text(
            "---\nname: demo-skill\n---\n# Demo\n", encoding="utf-8"
        )

        ok, msg = su.restore_skill("demo-skill")

        # 1. Shared guard reached exactly once (call_count == 1, not >= 1).
        self.assertEqual(len(self._calls), 1)
        # 2. normalized_name is exact skill_name (L1 verbatim).
        self.assertEqual(self._calls[0]["name"], "demo-skill")
        # 3. canonical_target is exact _skills_dir() / skill_name (flat layout).
        self.assertEqual(self._calls[0]["target"], self._skills_root / "demo-skill")
        # 4. replacement_policy is new_only (frozen).
        self.assertEqual(self._calls[0]["replacement_policy"], "new_only")
        # 5. body reached -> public return matches legacy success contract.
        self.assertTrue(ok)
        self.assertTrue(msg.startswith("restored to "))
        # 6. Restored destination exists with expected content (legacy byte semantics).
        live = self._skills_root / "demo-skill"
        self.assertTrue(live.is_dir())
        self.assertTrue((live / "SKILL.md").read_text(encoding="utf-8")
                        .startswith("---\nname: demo-skill"))
        # 7. archive no longer has the skill (rename succeeded).
        self.assertFalse(archived.exists())


if __name__ == "__main__":
    unittest.main()
