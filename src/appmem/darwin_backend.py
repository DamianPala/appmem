"""Application footprint collection on Apple Silicon.

The native reader is injected so ordinary tests never inspect the host. A
collection is one immutable process inventory; detail views reuse its members.
"""

from __future__ import annotations

import hashlib
import platform
import re
import sys
from dataclasses import dataclass
from typing import Protocol

from appmem.darwin_native import (
    DarwinNative,
    HostMemory,
    ProcessIdentity,
    ProcessMemory,
    ReadResult,
)


class DarwinUnavailableError(RuntimeError):
    """The platform or a required native inventory read is unavailable."""


class NativeReader(Protocol):
    def host(self) -> ReadResult[HostMemory]: ...
    def pids(self) -> ReadResult[list[int]]: ...
    def process(self, pid: int) -> ReadResult[ProcessIdentity]: ...
    def path(self, pid: int) -> ReadResult[str]: ...
    def memory(self, pid: int) -> ReadResult[ProcessMemory]: ...


@dataclass(frozen=True)
class DarwinProcess:
    pid: int
    ppid: int
    start_abstime: int | None
    command: str
    footprint_bytes: int | None
    unavailable: str | None
    path: str | None
    resident_bytes: int | None = None


@dataclass(frozen=True)
class DarwinApp:
    id: str
    key: str
    name: str
    footprint_bytes: int | None
    readable_processes: int
    unreadable_processes: int
    members: tuple[DarwinProcess, ...]
    grouping_partial: bool

    @property
    def resident_bytes(self) -> int | None:
        known = [p.resident_bytes for p in self.members if p.resident_bytes is not None]
        return sum(known) if known else None

    @property
    def resident_readable_processes(self) -> int:
        return sum(p.resident_bytes is not None for p in self.members)

    @property
    def resident_partial(self) -> bool:
        return self.resident_readable_processes < self.procs

    @property
    def procs(self) -> int:
        return len(self.members)

    @property
    def partial(self) -> bool:
        return self.unreadable_processes > 0


def _safe_name(value: str) -> str:
    # BSD comm is a short executable label, never command-line arguments.
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", value) or "unknown"


def _bundle(path: str | None) -> str | None:
    if not path or not path.startswith("/"):
        return None
    parts = path.split("/")
    for index, part in enumerate(parts):
        if part.endswith(".app") and part not in (".app", "..app"):
            return "/".join(parts[: index + 1])
    return None


def _identity_key(pid: int, start: int | None) -> str:
    return f"{pid}:{start if start is not None else 'unknown'}"


class DarwinBackend:
    """One-shot native reads and bounded ancestry grouping for the current user."""

    def __init__(self, uid: int, native: NativeReader | None = None) -> None:
        if native is None:
            if sys.platform != "darwin" or platform.machine() != "arm64":
                raise DarwinUnavailableError("macOS 15+ on Apple Silicon is required")
            major = int(platform.mac_ver()[0].split(".")[0] or "0")
            if major < 15:
                raise DarwinUnavailableError("macOS 15 or newer is required")
            native = DarwinNative()
        self._native = native
        self._uid = uid

    def check(self) -> None:
        self.read_system()

    def read_system(self) -> HostMemory:
        result = self._native.host()
        if result.value is None:
            raise DarwinUnavailableError(f"native host memory read failed: {result.unavailable}")
        return result.value

    def _read_inventory(self) -> tuple[dict[int, ProcessIdentity], dict[int, DarwinProcess]]:
        result = self._native.pids()
        if result.value is None:
            raise DarwinUnavailableError(f"native process inventory failed: {result.unavailable}")
        identities: dict[int, ProcessIdentity] = {}
        for pid in result.value:
            identity = self._native.process(pid).value
            if identity is not None:
                identities[pid] = identity

        processes: dict[int, DarwinProcess] = {}
        for pid, identity in identities.items():
            if identity.uid != self._uid:
                continue
            process = self._read_process(identity)
            if process is not None:
                processes[pid] = process
        return identities, processes

    def _read_process(self, identity: ProcessIdentity) -> DarwinProcess | None:
        pid = identity.pid
        memory_result = self._native.memory(pid)
        memory = memory_result.value
        path = self._native.path(pid).value
        # A second rusage read verifies the PID did not restart mid-sample.
        after = self._native.process(pid).value
        if after is None or after.ppid != identity.ppid or after.uid != identity.uid:
            return None
        if memory is not None:
            again = self._native.memory(pid).value
            if again is None or again.start_abstime != memory.start_abstime:
                return None
        if (
            identity.start_abstime is not None
            and memory is not None
            and identity.start_abstime != memory.start_abstime
        ):
            return None
        unavailable = None if memory is not None else str(memory_result.unavailable or "error")
        return DarwinProcess(
            pid=pid,
            ppid=after.ppid,
            start_abstime=memory.start_abstime if memory is not None else identity.start_abstime,
            command=_safe_name(after.command),
            footprint_bytes=memory.footprint_bytes if memory is not None else None,
            unavailable=unavailable,
            path=path,
            resident_bytes=memory.resident_bytes if memory is not None else None,
        )

    def _ancestry(
        self,
        process: DarwinProcess,
        identities: dict[int, ProcessIdentity],
        processes: dict[int, DarwinProcess],
    ) -> tuple[list[int], bool]:
        pid = process.pid
        chain: list[int] = []
        visited: set[int] = set()
        while pid in processes and pid not in visited and len(chain) < 64:
            visited.add(pid)
            chain.append(pid)
            parent = processes[pid].ppid
            if parent <= 1:
                return chain, False
            if parent not in identities:
                return chain, True
            if identities[parent].uid != self._uid:
                return chain, False
            if parent not in processes:
                return chain, True
            child_start = processes[pid].start_abstime
            parent_start = processes[parent].start_abstime
            if child_start is not None and parent_start is not None and parent_start > child_start:
                return chain, True
            pid = parent
        return chain, True

    def _group_processes(
        self, identities: dict[int, ProcessIdentity], processes: dict[int, DarwinProcess]
    ) -> tuple[dict[str, list[DarwinProcess]], set[str]]:
        direct_bundles = {
            pid: bundle
            for pid, process in processes.items()
            if (bundle := _bundle(process.path)) is not None
        }

        groups: dict[str, list[DarwinProcess]] = {}
        uncertain: set[str] = set()
        for process in processes.values():
            chain, missing = self._ancestry(process, identities, processes)
            bundle = next((direct_bundles[item] for item in chain if item in direct_bundles), None)
            if bundle is not None:
                key = f"bundle:{bundle}"
            else:
                root = processes[chain[-1]]
                key = f"session:{_identity_key(root.pid, root.start_abstime)}"
            groups.setdefault(key, []).append(process)
            if missing or process.path is None or process.start_abstime is None:
                uncertain.add(key)

        return groups, uncertain

    def collect_apps(self) -> list[DarwinApp]:
        identities, processes = self._read_inventory()
        groups, uncertain = self._group_processes(identities, processes)
        names: dict[str, str] = {}
        for key in groups:
            if key.startswith("bundle:"):
                names[key] = _safe_name(key[7:].rsplit("/", 1)[-1].removesuffix(".app"))
            else:
                root_pid = int(key.split(":", 2)[1])
                names[key] = processes[root_pid].command
        duplicates = {name for name in names.values() if list(names.values()).count(name) > 1}
        apps: list[DarwinApp] = []
        for key, members in groups.items():
            members.sort(key=lambda item: item.pid)
            readable = [
                item.footprint_bytes for item in members if item.footprint_bytes is not None
            ]
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            name = names[key]
            if name in duplicates:
                name = f"{name} [{digest[:6]}]"
            apps.append(
                DarwinApp(
                    id=digest,
                    key=key,
                    name=name,
                    footprint_bytes=sum(readable) if readable else None,
                    readable_processes=len(readable),
                    unreadable_processes=len(members) - len(readable),
                    members=tuple(members),
                    grouping_partial=key in uncertain,
                )
            )
        return sorted(
            apps,
            key=lambda app: (
                app.footprint_bytes is None,
                -(app.footprint_bytes or 0),
                app.name,
                app.id,
            ),
        )

    def find_app(self, name: str) -> DarwinApp | None:
        apps = self.collect_apps()
        return next((app for app in apps if app.id == name or app.name == name), None)

    def read_procs(self, app: DarwinApp) -> tuple[DarwinProcess, ...]:
        return app.members
