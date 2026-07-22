"""Detecting the container's real ceilings, and turning them into a safe count.

Everything here reads files that only exist inside a Linux container, so the
files are mocked with tmp_path and the layered fallback (cgroup v2 → v1 →
/proc/meminfo → nothing) is exercised directly. The one hard rule: an unreadable
or absent limit must degrade to None, never raise — a status page and a launch
both depend on this not crashing off a container.
"""
from __future__ import annotations

from app.services.capacity import (
    PER_BROWSER_GB,
    Capacity,
    detect_capacity,
    detect_memory_limit_bytes,
)

GB = 1024 ** 3


def _write(path, text):
    path.write_text(text)
    return path


class TestMemoryDetection:
    def test_cgroup_v2_is_preferred(self, tmp_path):
        v2 = _write(tmp_path / "memory.max", str(4 * GB))
        v1 = _write(tmp_path / "limit_in_bytes", str(16 * GB))
        meminfo = _write(tmp_path / "meminfo", "MemTotal: 33554432 kB\n")
        assert detect_memory_limit_bytes(cgroup_v2=v2, cgroup_v1=v1, meminfo=meminfo) == 4 * GB

    def test_cgroup_v1_used_when_v2_absent(self, tmp_path):
        v2 = tmp_path / "does-not-exist"
        v1 = _write(tmp_path / "limit_in_bytes", str(2 * GB))
        meminfo = _write(tmp_path / "meminfo", "MemTotal: 33554432 kB\n")
        assert detect_memory_limit_bytes(cgroup_v2=v2, cgroup_v1=v1, meminfo=meminfo) == 2 * GB

    def test_proc_meminfo_is_the_last_resort(self, tmp_path):
        v2 = tmp_path / "no-v2"
        v1 = tmp_path / "no-v1"
        # 8 GB expressed in kB, the /proc/meminfo unit.
        meminfo = _write(tmp_path / "meminfo", "MemFree: 1 kB\nMemTotal:  8388608 kB\n")
        assert detect_memory_limit_bytes(cgroup_v2=v2, cgroup_v1=v1, meminfo=meminfo) == 8 * GB

    def test_cgroup_max_word_falls_through(self, tmp_path):
        # cgroup v2 writes the literal "max" for "no limit" — skip to the next source.
        v2 = _write(tmp_path / "memory.max", "max\n")
        v1 = _write(tmp_path / "limit_in_bytes", str(3 * GB))
        meminfo = tmp_path / "none"
        assert detect_memory_limit_bytes(cgroup_v2=v2, cgroup_v1=v1, meminfo=meminfo) == 3 * GB

    def test_cgroup_v1_unlimited_sentinel_falls_through(self, tmp_path):
        # cgroup v1 writes a huge sentinel instead of a word for "unlimited".
        v2 = tmp_path / "no-v2"
        v1 = _write(tmp_path / "limit_in_bytes", "9223372036854771712")
        meminfo = _write(tmp_path / "meminfo", "MemTotal: 8388608 kB\n")
        assert detect_memory_limit_bytes(cgroup_v2=v2, cgroup_v1=v1, meminfo=meminfo) == 8 * GB

    def test_all_sources_unreadable_is_none(self, tmp_path):
        assert detect_memory_limit_bytes(
            cgroup_v2=tmp_path / "a", cgroup_v1=tmp_path / "b", meminfo=tmp_path / "c",
        ) is None

    def test_garbage_contents_are_none_not_a_crash(self, tmp_path):
        v2 = _write(tmp_path / "memory.max", "not-a-number\n")
        v1 = _write(tmp_path / "limit_in_bytes", "")
        meminfo = _write(tmp_path / "meminfo", "no MemTotal line here\n")
        assert detect_memory_limit_bytes(cgroup_v2=v2, cgroup_v1=v1, meminfo=meminfo) is None


class TestRecommendedCount:
    def test_none_memory_means_no_recommendation(self):
        assert Capacity(memory_limit_bytes=None).recommended_max_browsers() is None

    def test_four_gb_recommends_about_four(self):
        # The calibration point: a ~4 GB container should land at the observed
        # safe ceiling, and it must not warn the shipped default (max=4).
        rec = Capacity(memory_limit_bytes=4 * GB).recommended_max_browsers()
        assert rec == int(4 / PER_BROWSER_GB) == 4

    def test_never_below_one(self):
        # A tiny container still gets to try a single browser rather than zero.
        assert Capacity(memory_limit_bytes=GB // 4).recommended_max_browsers() == 1

    def test_scales_with_memory(self):
        assert Capacity(memory_limit_bytes=16 * GB).recommended_max_browsers() == 16


class TestDetectCapacity:
    def test_reads_pids_max_as_a_secondary_signal(self, tmp_path):
        v2 = _write(tmp_path / "memory.max", str(4 * GB))
        pids = _write(tmp_path / "pids.max", "512\n")
        cap = detect_capacity(
            cgroup_v2=v2, cgroup_v1=tmp_path / "x", meminfo=tmp_path / "y", pids_max=pids,
        )
        assert cap.memory_limit_bytes == 4 * GB
        assert cap.pids_max == 512
        assert cap.recommended_max_browsers() == 4

    def test_pids_max_word_is_none(self, tmp_path):
        cap = detect_capacity(
            cgroup_v2=tmp_path / "x", cgroup_v1=tmp_path / "y", meminfo=tmp_path / "z",
            pids_max=_write(tmp_path / "pids.max", "max\n"),
        )
        assert cap.pids_max is None

    def test_everything_absent_never_raises(self, tmp_path):
        cap = detect_capacity(
            cgroup_v2=tmp_path / "a", cgroup_v1=tmp_path / "b",
            meminfo=tmp_path / "c", pids_max=tmp_path / "d",
        )
        assert cap == Capacity(memory_limit_bytes=None, pids_max=None)
        assert cap.recommended_max_browsers() is None
