"""The task-profile lease pool: bounded, reusable sweep identities.

The old sweep minted a durable ``serp-<url-path>`` profile per unique URL and
never cleaned it up — unbounded accumulation on the volume. The pool replaces
that with a handful of ``task-N`` identities leased for one sweep and returned,
reused warm by the next. These tests pin the properties that make that safe:
two sweeps never share a profile, a released one is reused rather than re-minted,
and the pool stays bounded by ``task_budget`` under the instance pool's cap.
"""
from __future__ import annotations

import pytest

from app.services.instances import InstanceManager
from app.services.profiles import ProfileInUse, ProfileService, ProfileStore
from app.services.settings import SettingsService
from app.services.task_profiles import TaskProfilePool, is_task_profile


def _settings(tmp_path):
    return SettingsService(tmp_path / "settings.json", tmp_path / ".dek")


@pytest.fixture
def pool(tmp_path):
    settings = _settings(tmp_path)
    profiles = ProfileStore(tmp_path / "profiles")
    return TaskProfilePool(profiles, settings), profiles, settings


def _pool_names(profiles: ProfileStore) -> list[str]:
    return sorted(p.name for p in profiles.all() if is_task_profile(p.name))


class TestNaming:
    def test_only_task_n_names_count_as_pooled(self):
        assert is_task_profile("task-1")
        assert is_task_profile("task-42")
        # A user's own profile that merely starts with the prefix is not pooled.
        assert not is_task_profile("task-force")
        assert not is_task_profile("task-")
        assert not is_task_profile("task-01")  # not a canonical integer
        assert not is_task_profile("task-0")   # zero is not a pool number
        assert not is_task_profile("Default")
        assert not is_task_profile("research")

    def test_non_ascii_digit_suffix_is_false_not_a_crash(self):
        # "²".isdigit() is True but int("²") raises — is_task_profile must return
        # False rather than propagate, or a single such profile breaks every sweep.
        assert is_task_profile("task-²") is False
        assert is_task_profile("task-³⁴") is False

    def test_acquire_tolerates_a_stored_non_ascii_digit_profile(self, pool):
        # A user can create "task-²" (names aren't charset-checked). acquire scans
        # every profile through is_task_profile, so this must not raise.
        p, profiles, _ = pool
        profiles.get_or_create("task-²", default_country="US", default_region="california")
        assert p.acquire("job-1") == "task-1"


class TestDistinctLeases:
    def test_two_concurrent_acquires_never_return_the_same_profile(self, pool):
        p, _, _ = pool
        a = p.acquire("job-a")
        b = p.acquire("job-b")  # job-a still holds its lease
        assert a != b
        assert {a, b} == {"task-1", "task-2"}


class TestReuseVsMint:
    def test_a_released_profile_is_reused_not_reminted(self, pool):
        p, profiles, _ = pool
        first = p.acquire("job-1")
        p.release("job-1")
        second = p.acquire("job-2")
        assert second == first, "the freed profile is handed to the next sweep"
        assert _pool_names(profiles) == ["task-1"], "no second profile was minted"

    def test_mints_a_new_profile_only_when_all_existing_are_leased(self, pool):
        p, profiles, _ = pool
        a = p.acquire("job-1")  # mints task-1
        b = p.acquire("job-2")  # all existing leased -> mints task-2
        assert {a, b} == {"task-1", "task-2"}
        assert _pool_names(profiles) == ["task-1", "task-2"]

        # Free the lower one; the next acquire reuses it rather than minting a third.
        p.release("job-1")
        c = p.acquire("job-3")
        assert c == "task-1"
        assert _pool_names(profiles) == ["task-1", "task-2"], "still only two profiles exist"

    def test_a_deleted_free_profile_is_reminted_on_the_next_acquire(self, pool):
        p, profiles, _ = pool
        first = p.acquire("job-1")
        p.release("job-1")
        assert profiles.delete(first) is True  # user deletes the free pool profile
        assert _pool_names(profiles) == []
        again = p.acquire("job-2")
        assert again == "task-1", "the hole is re-minted"
        assert _pool_names(profiles) == ["task-1"]

    def test_mint_fills_the_lowest_hole_not_max_plus_one(self, pool):
        # task-1 and task-3 leased, task-2 a free gap. With every existing profile
        # leased, the next mint must fill the hole (task-2), not extend to task-4 —
        # this is what keeps the count bounded at the concurrency ceiling.
        p, profiles, _ = pool
        p.acquire("job-1")  # task-1
        p.acquire("job-2")  # task-2
        p.acquire("job-3")  # task-3
        p.release("job-2")
        assert profiles.delete("task-2") is True  # task-2 becomes a free gap
        # task-1 and task-3 are still leased -> no existing free profile to reuse.
        filled = p.acquire("job-4")
        assert filled == "task-2", "the lowest missing number is minted, not max+1"
        assert _pool_names(profiles) == ["task-1", "task-2", "task-3"]


class TestBounded:
    def test_never_exceeds_task_budget_under_the_concurrency_cap(self, pool):
        p, profiles, settings = pool
        settings.update(max_instances=4, interactive_reserve=1)
        budget = settings.load().task_budget
        assert budget == 3

        # Hold the ceiling's worth of leases at once — the most the instance pool
        # would ever permit concurrently.
        held = [p.acquire(f"job-{i}") for i in range(budget)]
        assert len(set(held)) == budget
        assert _pool_names(profiles) == ["task-1", "task-2", "task-3"]

        # Now "beyond the cap": release one and acquire again. Because concurrency
        # never exceeds the ceiling, the pool reuses rather than minting a fourth.
        p.release("job-0")
        p.acquire("job-extra")
        assert len(_pool_names(profiles)) == budget, "profile count stays at the budget"


class TestRelease:
    def test_release_frees_every_lease_a_task_holds(self, pool):
        p, _, _ = pool
        one = p.acquire("job-1")
        two = p.acquire("job-1")  # same task, two leases (within-task parallelism)
        assert one != two
        assert sorted(p.leased_by("job-1")) == sorted([one, two])

        p.release("job-1")
        assert p.leased_by("job-1") == [], "both leases freed"

        # Both names are free again, so the next acquires reuse them.
        reused = {p.acquire("job-2"), p.acquire("job-3")}
        assert reused == {one, two}

    def test_release_of_an_unknown_task_is_a_safe_noop(self, pool):
        p, _, _ = pool
        p.release("never-acquired")  # a stubbed sweep's finally must not raise


class TestManageable:
    """The Profiles UI keeps task-* profiles listed and deletable when free."""

    @pytest.fixture
    def managed(self, tmp_path):
        settings = _settings(tmp_path)
        manager = InstanceManager(settings)
        manager.profiles = ProfileStore(tmp_path / "profiles")
        return manager, ProfileService(manager, settings), settings

    @pytest.mark.asyncio
    async def test_task_profile_is_listed_and_deletable_when_free_refused_in_use(self, managed):
        manager, service, settings = managed
        pool = TaskProfilePool(manager.profiles, settings)
        name = pool.acquire("job-1")  # mints task-1 into the durable store

        # It shows up in the profile list like any other, not special-cased out.
        assert name in {v.name for v in await service.list_profiles()}

        # A leased/open profile is refused by the existing in-use guard.
        manager._reserve_profile(name)
        with pytest.raises(ProfileInUse):
            await service.delete_profile(name)

        # Free again -> deletable. (A deleted free pool profile is simply
        # re-minted on the next acquire; see TestReuseVsMint.)
        manager._release_profile(name)
        result = await service.delete_profile(name)
        assert result.name == name
        assert name not in {pf.name for pf in manager.profiles.all()}
