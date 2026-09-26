"""Linux backend behavior at the collection boundary, using fixture trees only."""

from pathlib import Path

import pytest

from appmem.collect import CgroupUnavailableError, LinuxBackend, MemoryStatUnavailableError
from helpers import make_unit, user_service_root, write_cgroup_procs, write_memory_stat

_MIB = 1024 * 1024


def _backend(root: Path) -> tuple[LinuxBackend, Path]:
    user_root = user_service_root(root, uid=1000)
    write_memory_stat(user_root, anon=1)
    return LinuxBackend(root, 1000), user_root


def test_collect_apps_groups_units_and_hides_small_rows(tmp_path: Path) -> None:
    backend, user_root = _backend(tmp_path)
    make_unit(user_root / "app.slice" / "app-ghostty@one.service", anon=2 * _MIB, pids=[1])
    make_unit(user_root / "app.slice" / "app-ghostty@two.service", anon=3 * _MIB, pids=[2])
    make_unit(user_root / "app.slice" / "app-tiny.service", anon=100)

    apps, counts = backend.collect_apps(
        include_system=False, strict=True, count_procs=True, previous_procs={}
    )

    assert [(app.name, app.ram, app.procs, len(app.unit_paths)) for app in apps] == [
        ("ghostty", 5 * _MIB, 2, 2)
    ]
    assert len(counts) == 3
    assert backend.find_app("user", "ghostty", strict=True) == apps[0]


def test_skipped_recount_carries_existing_count_and_counts_new_unit(tmp_path: Path) -> None:
    backend, user_root = _backend(tmp_path)
    first = make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * _MIB, pids=[1])
    _, previous = backend.collect_apps(
        include_system=False, strict=False, count_procs=True, previous_procs={}
    )
    write_cgroup_procs(first, [1, 2, 3])
    second = make_unit(user_root / "app.slice" / "app-brave.service", anon=2 * _MIB, pids=[4, 5])

    apps, current = backend.collect_apps(
        include_system=False, strict=False, count_procs=False, previous_procs=previous
    )

    assert {app.name: app.procs for app in apps} == {"ghostty": 1, "brave": 2}
    assert current == {str(first): 1, str(second): 2}
    assert previous == {str(first): 1}


def test_strict_and_tick_reads_preserve_distinct_missing_stat_errors(tmp_path: Path) -> None:
    backend, user_root = _backend(tmp_path)
    (user_root / "memory.stat").write_text("anon 1\n")

    with pytest.raises(CgroupUnavailableError):
        backend.check()
    with pytest.raises(MemoryStatUnavailableError):
        backend.collect_apps(
            include_system=False, strict=False, count_procs=False, previous_procs={}
        )
