"""Cross-process regression tests for public skill creation."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

_SKILL_NAME = "cross-process-create"
_SKILL_CONTENT = """---
name: cross-process-create
description: Verify cross-process skill publication.
---
# Cross-Process Create

Test fixture for concurrent publication.
"""
_CATEGORIES = ("cat-a", "cat-b")
_MODE_POSITIVE = "positive"
_MODE_TARGETED_MUTATION = "targeted_mutation"
_SYNC_TIMEOUT_SECONDS = 15
_HOLDER_RELEASE_TIMEOUT_SECONDS = 30
_JOIN_TIMEOUT_SECONDS = 30


def _create_skill_in_process(
    worker_id: int,
    hermes_home: str,
    home: str,
    result_path: str,
    mode: str,
    holder_entered,
    contender_attempting,
    release_holder,
    second_scan_barrier,
    second_scan_completed,
) -> None:
    """Run one public create call in a fresh interpreter process."""
    os.environ["HERMES_HOME"] = hermes_home
    os.environ["HOME"] = home
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    public_create_called = False
    guard_attempted = False
    guard_entered = False
    duplicate_scan_count = 0
    second_scan_conflict_count = None
    second_scan_rendezvous_passed = False
    global_lock_bypass_count = 0
    global_lock_paths = []
    target_lock_delegate_count = 0
    target_lock_paths = []
    replacement_policy_seen = None
    try:
        from tools import skill_publish_guard as publish_guard

        real_guard = publish_guard.live_skill_publish_guard

        if mode == _MODE_TARGETED_MUTATION:
            # Mutation-only instrumentation point 1: replace the shared lock
            # acquisition primitive, but bypass only the lock path derived by
            # normalized_name_lock_target. Every other acquisition delegates
            # to the real primitive, preserving each distinct target lock.
            real_acquire = publish_guard._acquire_lock_at_path
            normalized_name = publish_guard.canonical_normalize_skill_name(
                _SKILL_NAME
            )
            if normalized_name != _SKILL_NAME:
                raise AssertionError("fixture name did not retain canonical identity")

            @contextmanager
            def targeted_acquire(*, lock_path, canonical_skill_path):
                nonlocal global_lock_bypass_count, target_lock_delegate_count
                expected_global_lock = publish_guard.normalized_name_lock_target(
                    normalized_name,
                    anchor=canonical_skill_path,
                ).resolve(strict=False)
                observed_lock = Path(lock_path).resolve(strict=False)
                if observed_lock == expected_global_lock:
                    global_lock_bypass_count += 1
                    global_lock_paths.append(str(observed_lock))
                    yield None
                    return

                target_lock_delegate_count += 1
                target_lock_paths.append(str(observed_lock))
                with real_acquire(
                    lock_path=lock_path,
                    canonical_skill_path=canonical_skill_path,
                ) as state:
                    yield state

            # Mutation-only instrumentation point 2: observe the real duplicate
            # scans without changing their result. On scan #2, each process has
            # already acquired its real target lock. The barrier returns only
            # after both scan snapshots exist, while neither guard has yielded
            # to _create_skill publication.
            real_duplicate_scan = publish_guard.global_duplicate_scan

            def synchronized_duplicate_scan(*args, **kwargs):
                nonlocal duplicate_scan_count
                nonlocal second_scan_conflict_count
                nonlocal second_scan_rendezvous_passed
                conflicts = real_duplicate_scan(*args, **kwargs)
                duplicate_scan_count += 1
                if duplicate_scan_count == 2:
                    second_scan_conflict_count = len(conflicts)
                    second_scan_completed[worker_id].set()
                    try:
                        second_scan_barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
                    except Exception as exc:
                        raise TimeoutError(
                            "workers did not rendezvous after duplicate scan #2"
                        ) from exc
                    second_scan_rendezvous_passed = True
                return conflicts

            publish_guard._acquire_lock_at_path = targeted_acquire
            publish_guard.global_duplicate_scan = synchronized_duplicate_scan
        elif mode != _MODE_POSITIVE:
            raise ValueError(f"unknown race mode: {mode}")

        @contextmanager
        def observed_guard(
            name,
            *,
            target,
            replacement_policy="new_only",
        ):
            nonlocal guard_attempted, guard_entered, replacement_policy_seen
            guard_attempted = True
            replacement_policy_seen = replacement_policy
            if mode == _MODE_POSITIVE and worker_id == 1:
                contender_attempting.set()

            with real_guard(
                name,
                target=target,
                replacement_policy=replacement_policy,
            ) as state:
                guard_entered = True
                if mode == _MODE_POSITIVE and worker_id == 0:
                    holder_entered.set()
                    if not release_holder.wait(
                        timeout=_HOLDER_RELEASE_TIMEOUT_SECONDS
                    ):
                        raise TimeoutError("parent did not release the holder process")
                yield state

        # Both modes retain the public skill_manage(create) -> _create_skill
        # path. This public-boundary wrapper delegates to the captured real
        # guard; only the mutation mode's two instrumentation points above
        # alter/observe shared-guard internals.
        publish_guard.live_skill_publish_guard = observed_guard

        from tools.skill_manager_tool import skill_manage

        try:
            public_create_called = True
            payload = json.loads(
                skill_manage(
                    action="create",
                    name=_SKILL_NAME,
                    category=_CATEGORIES[worker_id],
                    content=_SKILL_CONTENT,
                )
            )
            result = {
                "worker_id": worker_id,
                "kind": "return",
                "payload": payload,
            }
        except publish_guard.SkillMutationLockAcquireFailure as exc:
            cause = exc.cause_exception
            result = {
                "worker_id": worker_id,
                "kind": "duplicate_refusal",
                "exception_type": type(exc).__name__,
                "cause_type": type(cause).__name__ if cause is not None else None,
                "error": str(cause if cause is not None else exc),
            }
    except BaseException as exc:  # pragma: no cover - surfaced by parent assertions
        result = {
            "worker_id": worker_id,
            "kind": "worker_error",
            "exception_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }

    result["public_create_called"] = public_create_called
    result["guard_attempted"] = guard_attempted
    result["guard_entered"] = guard_entered
    result["duplicate_scan_count"] = duplicate_scan_count
    result["second_scan_conflict_count"] = second_scan_conflict_count
    result["second_scan_rendezvous_passed"] = second_scan_rendezvous_passed
    result["global_lock_bypass_count"] = global_lock_bypass_count
    result["global_lock_paths"] = global_lock_paths
    result["target_lock_delegate_count"] = target_lock_delegate_count
    result["target_lock_paths"] = target_lock_paths
    result["replacement_policy_seen"] = replacement_policy_seen
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


def _run_create_race(root: Path, *, mode: str) -> dict:
    """Run an isolated two-process public-create race."""
    home = root / "home"
    hermes_home = root / "hermes"
    skills_root = hermes_home / "skills"
    result_dir = root / "results"
    for directory in (home, skills_root, result_dir):
        directory.mkdir(parents=True)

    context = mp.get_context("spawn")
    holder_entered = context.Event()
    contender_attempting = context.Event()
    release_holder = context.Event()
    second_scan_barrier = context.Barrier(2)
    second_scan_completed = (context.Event(), context.Event())
    result_paths = [result_dir / f"worker-{worker_id}.json" for worker_id in range(2)]
    processes = [
        context.Process(
            target=_create_skill_in_process,
            args=(
                worker_id,
                str(hermes_home),
                str(home),
                str(result_paths[worker_id]),
                mode,
                holder_entered,
                contender_attempting,
                release_holder,
                second_scan_barrier,
                second_scan_completed,
            ),
        )
        for worker_id in range(2)
    ]

    started = []
    holder_ready = False
    contender_attempted = False
    try:
        if mode == _MODE_POSITIVE:
            processes[0].start()
            started.append(processes[0])
            holder_ready = holder_entered.wait(timeout=_SYNC_TIMEOUT_SECONDS)
            if holder_ready:
                processes[1].start()
                started.append(processes[1])
                contender_attempted = contender_attempting.wait(
                    timeout=_SYNC_TIMEOUT_SECONDS
                )
        elif mode == _MODE_TARGETED_MUTATION:
            for process in processes:
                process.start()
                started.append(process)
        else:
            raise ValueError(f"unknown race mode: {mode}")
    finally:
        release_holder.set()

    deadline = time.monotonic() + _JOIN_TIMEOUT_SECONDS
    for process in started:
        process.join(timeout=max(0, deadline - time.monotonic()))

    stuck = [process for process in started if process.is_alive()]
    stuck_pids = [process.pid for process in stuck]
    for process in stuck:
        process.terminate()
    for process in stuck:
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)

    missing_results = [path for path in result_paths if not path.is_file()]
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in result_paths
        if path.is_file()
    ]
    return {
        "mode": mode,
        "holder_ready": holder_ready,
        "contender_attempted": contender_attempted,
        "second_scan_completed": [event.is_set() for event in second_scan_completed],
        "stuck_pids": stuck_pids,
        "exitcodes": [process.exitcode for process in processes],
        "missing_results": missing_results,
        "results": results,
        "skills_root": skills_root,
        "temporary_residue": sorted(
            path for path in hermes_home.rglob("*.tmp") if path.is_file()
        ),
    }


def _assert_workers_completed(race: dict) -> None:
    """Assert orchestration and child reporting completed without deadlock."""
    if race["mode"] == _MODE_POSITIVE:
        assert race["holder_ready"], "holder never entered its guard region"
        assert race["contender_attempted"], "contender never reached guard entry"
    else:
        assert race["second_scan_completed"] == [True, True], (
            "both workers did not complete duplicate scan #2"
        )
    assert not race["stuck_pids"], (
        f"worker process did not terminate before timeout: {race['stuck_pids']}"
    )
    assert race["exitcodes"] == [0, 0]
    assert not race["missing_results"], (
        f"workers produced no result files: {race['missing_results']}"
    )
    worker_errors = [
        result for result in race["results"] if result["kind"] == "worker_error"
    ]
    assert not worker_errors, f"worker failed before completing create: {worker_errors}"
    assert len(race["results"]) == 2
    assert all(result["public_create_called"] for result in race["results"])
    assert all(result["guard_attempted"] for result in race["results"])


def _successful_results(race: dict) -> list:
    return [
        result
        for result in race["results"]
        if result["kind"] == "return"
        and result["payload"].get("success") is True
        and not result["payload"].get("staged")
    ]


def _duplicate_refusals(race: dict) -> list:
    return [
        result
        for result in race["results"]
        if result["kind"] == "duplicate_refusal"
        and result.get("cause_type") == "ValueError"
        and "duplicate live skill named" in result.get("error", "").lower()
    ]


def _live_skill_files(race: dict) -> list:
    return sorted(race["skills_root"].rglob(f"{_SKILL_NAME}/SKILL.md"))


def _assert_single_publication_contract(race: dict) -> None:
    """Assert the public create contract shared with the mutation witness."""
    successes = _successful_results(race)
    duplicate_refusals = _duplicate_refusals(race)
    assert len(successes) == 1, (
        f"expected exactly one successful public create: {race['results']}"
    )
    assert len(duplicate_refusals) == 1, (
        f"expected exactly one guarded duplicate refusal: {race['results']}"
    )
    assert successes[0]["worker_id"] == 0
    assert successes[0]["guard_entered"] is True
    assert duplicate_refusals[0]["worker_id"] == 1
    assert duplicate_refusals[0]["guard_entered"] is False

    live_skill_files = _live_skill_files(race)
    assert len(live_skill_files) == 1, live_skill_files

    winner_category = successes[0]["payload"].get("category")
    assert winner_category == _CATEGORIES[0]
    expected_live_file = (
        race["skills_root"] / winner_category / _SKILL_NAME / "SKILL.md"
    )
    assert live_skill_files == [expected_live_file]
    assert not (race["skills_root"] / _CATEGORIES[1] / _SKILL_NAME).exists()


def test_cross_process_public_create_same_name_publishes_once(tmp_path):
    """The real public guard permits one same-name publication."""
    guarded = _run_create_race(tmp_path / "guarded", mode=_MODE_POSITIVE)
    _assert_workers_completed(guarded)
    _assert_single_publication_contract(guarded)
    assert not guarded["temporary_residue"], (
        f"atomic publication left temporary files: {guarded['temporary_residue']}"
    )


def test_targeted_global_serialization_mutation_violates_contract(tmp_path):
    """Removing only the global name lock deterministically publishes twice."""
    mutated = _run_create_race(
        tmp_path / "targeted-mutation",
        mode=_MODE_TARGETED_MUTATION,
    )
    _assert_workers_completed(mutated)

    results = sorted(mutated["results"], key=lambda result: result["worker_id"])
    assert all(result["replacement_policy_seen"] == "new_only" for result in results)
    assert all(result["duplicate_scan_count"] == 2 for result in results)
    assert all(result["second_scan_conflict_count"] == 0 for result in results)
    assert all(result["second_scan_rendezvous_passed"] for result in results)
    assert all(result["global_lock_bypass_count"] == 1 for result in results)
    assert all(len(result["global_lock_paths"]) == 1 for result in results)
    assert len({result["global_lock_paths"][0] for result in results}) == 1
    assert all(result["target_lock_delegate_count"] == 1 for result in results)
    assert all(len(result["target_lock_paths"]) == 1 for result in results)
    assert len({result["target_lock_paths"][0] for result in results}) == 2
    assert all(result["guard_entered"] for result in results)

    successes = _successful_results(mutated)
    assert len(successes) == 2, mutated["results"]
    assert not _duplicate_refusals(mutated), mutated["results"]
    assert _live_skill_files(mutated) == [
        mutated["skills_root"] / category / _SKILL_NAME / "SKILL.md"
        for category in _CATEGORIES
    ]
    assert not mutated["temporary_residue"], (
        f"atomic publication left temporary files: {mutated['temporary_residue']}"
    )

    try:
        _assert_single_publication_contract(mutated)
    except AssertionError as exc:
        assert "expected exactly one successful public create" in str(exc)
    else:  # pragma: no cover - this is the mutation-sensitivity failure path
        raise AssertionError("targeted global-lock mutation escaped the contract")
