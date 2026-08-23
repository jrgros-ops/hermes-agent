"""Donor-extracted A1G integration tests for P3 install_from_quarantine.

Recovered from donor commit 475458e0546548761c40a232f834b11f14fc0acc
(test_install_from_quarantine_guard_*). Test bodies preserved unchanged
from donor; only wrapped in a class to make them discoverable and the
import surface adapted to the candidate's CURRENT_MAIN structure.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.skills_hub as hub
from tools.skill_publish_guard import SkillMutationLockAcquireFailure
from tools.skills_guard import ScanResult


class TestA1GInstallFromQuarantineGuard:
    def test_install_from_quarantine_guard_wraps_rmtree_and_move(self, tmp_path):
        skills_dir = tmp_path / "skills"
        existing = skills_dir / "guarded-skill"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("---\nname: guarded-skill\n---\nold\n")
        quarantine_root = skills_dir / ".hub" / "quarantine"
        q_dir = quarantine_root / "pending"
        q_dir.mkdir(parents=True)
        (q_dir / "SKILL.md").write_text("---\nname: guarded-skill\n---\nnew\n")
        bundle = hub.SkillBundle(
            name="guarded-skill",
            files={"SKILL.md": "---\nname: guarded-skill\n---\nnew\n"},
            source="community",
            identifier="x",
            trust_level="community",
        )
        scan_result = ScanResult(
            skill_name="guarded-skill",
            source="community",
            trust_level="community",
            verdict="safe",
        )
        events = []
        active = {"guard": False}

        @contextmanager
        def fake_publish_guard(name, *, target, replacement_policy):
            events.append(("enter", name, target, replacement_policy, existing.exists()))
            active["guard"] = True
            try:
                yield
            finally:
                events.append(("exit", existing.exists(), q_dir.exists()))
                active["guard"] = False

        real_rmtree = hub.shutil.rmtree
        real_move = hub.shutil.move

        def checked_rmtree(path, *args, **kwargs):
            assert active["guard"] is True
            events.append(("rmtree", Path(path)))
            return real_rmtree(path, *args, **kwargs)

        def checked_move(src, dst, *args, **kwargs):
            assert active["guard"] is True
            events.append(("move", Path(src), Path(dst)))
            return real_move(src, dst, *args, **kwargs)

        with patch.object(hub, "SKILLS_DIR", skills_dir), \
             patch.object(hub, "QUARANTINE_DIR", quarantine_root), \
             patch.object(hub, "live_skill_publish_guard", side_effect=fake_publish_guard), \
             patch.object(hub.shutil, "rmtree", side_effect=checked_rmtree), \
             patch.object(hub.shutil, "move", side_effect=checked_move), \
             patch("tools.skill_usage.record_installed"):
            installed = hub.install_from_quarantine(q_dir, "guarded-skill", "", bundle, scan_result)

        assert installed == existing
        assert (existing / "SKILL.md").read_text().endswith("new\n")
        assert events[0] == ("enter", "guarded-skill", existing, "replace_same_target", True)
        assert ("rmtree", existing) in events
        assert ("move", q_dir, existing) in events

    def test_install_from_quarantine_guard_failure_preserves_source_and_target(self, tmp_path):
        skills_dir = tmp_path / "skills"
        existing = skills_dir / "blocked-skill"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("---\nname: blocked-skill\n---\nold\n")
        quarantine_root = skills_dir / ".hub" / "quarantine"
        q_dir = quarantine_root / "pending"
        q_dir.mkdir(parents=True)
        (q_dir / "SKILL.md").write_text("---\nname: blocked-skill\n---\nnew\n")
        bundle = hub.SkillBundle(
            name="blocked-skill",
            files={"SKILL.md": "---\nname: blocked-skill\n---\nnew\n"},
            source="community",
            identifier="x",
            trust_level="community",
        )
        scan_result = ScanResult(
            skill_name="blocked-skill",
            source="community",
            trust_level="community",
            verdict="safe",
        )
        refusal = SkillMutationLockAcquireFailure(
            canonical_skill_path=existing,
            lock_path=tmp_path / "guard.lock",
            platform="test",
            lock_failure_stage="lock_primitive_acquire",
            cause=ValueError("blocked"),
        )

        with patch.object(hub, "SKILLS_DIR", skills_dir), \
             patch.object(hub, "QUARANTINE_DIR", quarantine_root), \
             patch.object(hub, "live_skill_publish_guard", side_effect=refusal), \
             patch("tools.skill_usage.record_installed") as record_installed, \
             pytest.raises(SkillMutationLockAcquireFailure):
            hub.install_from_quarantine(q_dir, "blocked-skill", "", bundle, scan_result)

        assert (q_dir / "SKILL.md").read_text().endswith("new\n")
        assert (existing / "SKILL.md").read_text().endswith("old\n")
        record_installed.assert_not_called()