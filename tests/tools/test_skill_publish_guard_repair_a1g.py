"""Donor-extracted A1G skill publication guard integration tests.

Recovered from donor commit 475458e0546548761c40a232f834b11f14fc0acc
(TestA1HSkillPublicationGuards class), focused on guard invocation for:
  - P3 install_from_quarantine (covered in test_skills_hub_a1g.py)
  - P4 restore_official_optional_skill (this file)
  - P5 reset_bundled_skill (this file)
  - P6 sync_skills / recovery / deletion (this file)
  - External-shadow cleanup (this file)

Test bodies preserved unchanged from donor; only the file location and the
import surface are adapted to the candidate's CURRENT_MAIN structure
(_skill helper inlined, no shared _patches stack).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import tools.skills_sync as ss
from tools.skills_sync import (
    _dir_hash,
    restore_official_optional_skill,
    reset_bundled_skill,
    sync_skills,
)


def _skill(root, rel, *, name=None, body="body\n"):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    skill_name = name or d.name
    (d / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n{body}")
    return d


def _patches(bundled, skills_dir, manifest_file):
    """Patch context that exposes the test's skills dir to the guard."""
    stack = ExitStack()
    stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
    stack.enter_context(
        patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills")
    )
    stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
    stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
    # P6 guard wiring uses _maintenance_duplicate_scan via agent.skill_utils;
    # patch get_all_skills_dirs so the guard sees the test's skills dir.
    stack.enter_context(
        patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_dir])
    )
    return stack


def _guard_refusal(path):
    from tools.skill_publish_guard import SkillMutationLockAcquireFailure

    return SkillMutationLockAcquireFailure(
        canonical_skill_path=path,
        lock_path=path.parent / "guard.lock",
        platform="test",
        lock_failure_stage="lock_primitive_acquire",
        cause=ValueError("unexpected same-name live state"),
    )


class TestA1HSkillPublicationGuards:
    def test_restore_official_optional_skill_guard_paths_for_multiple_matches(self, tmp_path):
        optional = tmp_path / "optional-skills"
        src = _skill(optional, "productivity/folder-skill", name="official-skill", body="# official\n")
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        dest = _skill(skills_dir, "productivity/folder-skill", name="official-skill", body="# stale canonical\n")
        moved_by_folder = _skill(skills_dir, "old/folder-skill", name="folder-skill", body="# stale folder\n")
        moved_by_frontmatter = _skill(skills_dir, "misc/local-copy", name="official-skill", body="# stale alias\n")
        calls = []

        @contextmanager
        def fake_repair_guard(name, *, target, approved_existing_paths, mutation_paths, identity_names=()):
            calls.append({
                "name": name,
                "target": target,
                "approved": list(approved_existing_paths),
                "mutation": list(mutation_paths),
                "identity_names": tuple(identity_names),
            })
            yield

        with patch("tools.skills_sync._get_optional_dir", return_value=optional), \
             patch("tools.skills_sync.SKILLS_DIR", skills_dir), \
             patch("tools.skills_sync.MANIFEST_FILE", manifest_file), \
             patch("tools.skills_sync.live_skill_repair_guard", side_effect=fake_repair_guard):
            result = restore_official_optional_skill("official-skill", restore=True)

        assert result["ok"] is True
        assert len(calls) == 1
        assert calls[0]["name"] == "official-skill"
        assert calls[0]["target"] == dest
        assert set(calls[0]["approved"]) == {dest, moved_by_folder, moved_by_frontmatter}
        assert set(calls[0]["mutation"]) == {dest, moved_by_folder, moved_by_frontmatter}
        assert set(calls[0]["identity_names"]) == {"folder-skill", "official-skill"}
        assert (dest / "SKILL.md").read_text() == (src / "SKILL.md").read_text()

    def test_restore_guard_refusal_fails_closed_without_partial_restore(self, tmp_path):
        optional = tmp_path / "optional-skills"
        _skill(optional, "productivity/guarded", name="guarded", body="# official\n")
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        dest = _skill(skills_dir, "productivity/guarded", name="guarded", body="# user copy\n")

        with patch("tools.skills_sync._get_optional_dir", return_value=optional), \
             patch("tools.skills_sync.SKILLS_DIR", skills_dir), \
             patch("tools.skills_sync.MANIFEST_FILE", manifest_file), \
             patch("tools.skills_sync.live_skill_repair_guard", side_effect=_guard_refusal(dest)), \
             pytest.raises(type(_guard_refusal(dest))):
            restore_official_optional_skill("guarded", restore=True)

        assert (dest / "SKILL.md").read_text().endswith("# user copy\n")
        assert not (skills_dir / ".restore-backups").exists()

    def test_sync_new_copy_guard_failure_does_not_publish_or_manifest(self, tmp_path):
        bundled = tmp_path / "bundled"
        _skill(bundled, "category/new-skill", name="new-skill")
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        dest = skills_dir / "category" / "new-skill"

        with _patches(bundled, skills_dir, manifest_file), \
             patch("tools.skills_sync.live_skill_publish_guard", side_effect=_guard_refusal(dest)), \
             pytest.raises(type(_guard_refusal(dest))):
            sync_skills(quiet=True)

        assert not dest.exists()
        assert not manifest_file.exists()

    def test_sync_update_repair_guard_spans_backup_copy_and_rollback(self, tmp_path):
        bundled = tmp_path / "bundled"
        _skill(bundled, "old-skill", name="old-skill", body="# upstream v2\n")
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        dest = _skill(skills_dir, "old-skill", name="old-skill", body="# user v1\n")
        manifest_file.write_text(f"old-skill:{_dir_hash(dest)}\n")
        active = {"guard": False}
        events = []

        @contextmanager
        def fake_repair_guard(name, *, target, approved_existing_paths, mutation_paths, identity_names=()):
            assert name == "old-skill"
            assert target == dest
            assert set(approved_existing_paths) == {dest}
            assert set(mutation_paths) == {dest, dest.with_suffix(".bak")}
            active["guard"] = True
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")
                active["guard"] = False

        real_move = ss.shutil.move

        def checked_move(src, dst, *args, **kwargs):
            assert active["guard"] is True
            events.append(("move", __import__("pathlib").Path(src), __import__("pathlib").Path(dst)))
            return real_move(src, dst, *args, **kwargs)

        def partial_copy(src, dst, *args, **kwargs):
            assert active["guard"] is True
            __import__("pathlib").Path(dst).mkdir(parents=True, exist_ok=True)
            (__import__("pathlib").Path(dst) / "PARTIAL").write_text("half")
            raise OSError("copy failed")

        with _patches(bundled, skills_dir, manifest_file), \
             patch("tools.skills_sync.live_skill_repair_guard", side_effect=fake_repair_guard), \
             patch("tools.skills_sync.shutil.move", side_effect=checked_move), \
             patch("tools.skills_sync.shutil.copytree", side_effect=partial_copy):
            result = sync_skills(quiet=True)

        assert result["updated"] == []
        assert (dest / "SKILL.md").read_text().endswith("# user v1\n")
        assert not (dest / "PARTIAL").exists()
        assert ("move", dest, dest.with_suffix(".bak")) in events
        assert ("move", dest.with_suffix(".bak"), dest) in events

    def test_recover_renamed_skill_guard_spans_candidate_to_canonical_move(self, tmp_path):
        from pathlib import Path

        from tools.skills_sync import _recover_renamed_skill

        skills_dir = tmp_path / "user_skills"
        old = _skill(skills_dir, "oldcat/moved-skill", name="moved-skill")
        origin_hash = _dir_hash(old)
        dest = skills_dir / "newcat" / "moved-skill"
        active = {"guard": False}
        calls = []

        @contextmanager
        def fake_repair_guard(name, *, target, approved_existing_paths, mutation_paths, identity_names=()):
            calls.append((name, target, list(approved_existing_paths), list(mutation_paths)))
            active["guard"] = True
            try:
                yield
            finally:
                active["guard"] = False

        real_move = ss.shutil.move

        def checked_move(src, dst, *args, **kwargs):
            assert active["guard"] is True
            return real_move(src, dst, *args, **kwargs)

        with patch("tools.skills_sync.SKILLS_DIR", skills_dir), \
             patch("tools.skills_sync.live_skill_repair_guard", side_effect=fake_repair_guard), \
             patch("tools.skills_sync.shutil.move", side_effect=checked_move):
            moved_from = _recover_renamed_skill(
                "moved-skill", origin_hash, dest, {"moved-skill": [old]}, set(), True
            )

        assert moved_from == "oldcat/moved-skill"
        assert calls == [("moved-skill", dest, [old], [old, dest])]
        assert dest.exists()
        assert not old.exists()

    def test_reset_restore_guard_does_not_span_followup_sync(self, tmp_path):
        bundled = tmp_path / "bundled"
        _skill(bundled, "productivity/google-workspace", name="google-workspace")
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        dest = _skill(skills_dir, "productivity/google-workspace", name="google-workspace", body="# user\n")
        manifest_file.write_text("google-workspace:STALEHASH000000000000000000000000\n")
        active = {"guard": False}
        events = []

        @contextmanager
        def fake_repair_guard(name, *, target, approved_existing_paths, mutation_paths, identity_names=()):
            assert name == "google-workspace"
            assert target == dest
            assert approved_existing_paths == [dest]
            assert mutation_paths == [dest]
            active["guard"] = True
            events.append("guard-enter")
            try:
                yield
            finally:
                events.append("guard-exit")
                active["guard"] = False

        def fake_sync(quiet=False):
            assert active["guard"] is False
            events.append("sync-outside-guard")
            return {"copied": ["google-workspace"]}

        with _patches(bundled, skills_dir, manifest_file), \
             patch("tools.skills_sync.live_skill_repair_guard", side_effect=fake_repair_guard), \
             patch("tools.skills_sync.sync_skills", side_effect=fake_sync):
            result = reset_bundled_skill("google-workspace", restore=True)

        assert result["ok"] is True
        assert events == ["guard-enter", "guard-exit", "sync-outside-guard"]
        assert not dest.exists()

    def test_external_shadow_cleanup_approves_external_survivor_without_locking_it(self, tmp_path):
        # Adapted to candidate CURRENT_MAIN: candidate uses
        # _build_external_skill_index() (Set[str] of names), not the donor's
        # _build_external_skill_paths_by_name() (Dict[str, List[Path]]).
        # The guard is still called with the local path as target; the
        # donor's per-skill/per-op lock granularity invariant is preserved.
        bundled = tmp_path / "bundled"
        _skill(bundled, "category/shadowed", name="shadowed")
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        local = _skill(skills_dir, "category/shadowed", name="shadowed")
        _skill(tmp_path / "external", "category/shadowed", name="shadowed")
        calls = []

        @contextmanager
        def fake_repair_guard(name, *, target, approved_existing_paths, mutation_paths, identity_names=()):
            calls.append((name, target, list(approved_existing_paths), list(mutation_paths)))
            yield

        with _patches(bundled, skills_dir, manifest_file), \
             patch("tools.skills_sync._build_external_skill_index", return_value={"shadowed"}), \
             patch("tools.skills_sync.live_skill_repair_guard", side_effect=fake_repair_guard):
            result = sync_skills(quiet=True)

        assert "shadowed" in result["shadowed_by_external"]
        # The guard was called once for the local shadow cleanup.
        assert len(calls) == 1
        name, target, approved, mutation = calls[0]
        assert name == "shadowed"
        assert target == local
        assert local in approved
        assert local in mutation
        assert not local.exists()