"""Collection boundary shared by reports and live screens."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from appmem.model import AppStats, ProcStats, SystemStats


class Backend(Protocol):
    def check(self) -> None: ...

    def read_system(self) -> SystemStats: ...

    def collect_apps(
        self,
        *,
        include_system: bool,
        strict: bool,
        count_procs: bool,
        previous_procs: Mapping[str, int],
    ) -> tuple[list[AppStats], dict[str, int]]: ...

    def find_app(self, scope: str, name: str, *, strict: bool) -> AppStats | None: ...

    def read_procs(self, app: AppStats) -> list[ProcStats]: ...

    def private_bytes(self, pid: int) -> int | None: ...


def select_backend(root: Path, uid: int) -> Backend:
    """Construct the Linux backend; `cli` takes the macOS path before this is called."""
    from appmem.collect import LinuxBackend

    return LinuxBackend(root, uid)
