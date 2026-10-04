"""Application footprint collection on Apple Silicon.

The native reader is injected so ordinary tests never inspect the host. A
collection is one immutable process inventory; detail views reuse its members.
"""

from __future__ import annotations

import hashlib
import platform
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, get_args

from appmem.darwin_native import (
    DarwinNative,
    HostMemory,
    ProcessIdentity,
    ProcessMemory,
    ReadResult,
)


class DarwinUnavailableError(RuntimeError):
    """The host cannot run this backend: wrong OS, processor or macOS version."""


class DarwinReadError(DarwinUnavailableError):
    """A supported host failed a required native read; the next attempt may succeed.

    A subclass so the live view, which only needs "no data this tick", catches both.
    """


class NativeReader(Protocol):
    def host(self) -> ReadResult[HostMemory]: ...
    def pids(self) -> ReadResult[list[int]]: ...
    def process(self, pid: int) -> ReadResult[ProcessIdentity]: ...
    def path(self, pid: int) -> ReadResult[str]: ...
    def memory(self, pid: int) -> ReadResult[ProcessMemory]: ...
    def compressed(self, pid: int) -> ReadResult[int]: ...
    def responsible(self, pid: int) -> ReadResult[int]: ...
    def bundle_id(self, bundle: str) -> ReadResult[str]: ...


# The rule that placed a process in its app, in the order the rules are tried.
Via = Literal["bundle", "ancestry", "responsible", "root"]
VIA_RULES: tuple[Via, ...] = get_args(Via)


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
    compressed_bytes: int | None = None
    via: Via = "root"


@dataclass(frozen=True)
class DarwinApp:
    id: str
    key: str
    name: str
    display_name: str
    bundle_id: str | None
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
    def compressed_bytes(self) -> int | None:
        known = [p.compressed_bytes for p in self.members if p.compressed_bytes is not None]
        return sum(known) if known else None

    @property
    def compressed_readable_processes(self) -> int:
        return sum(p.compressed_bytes is not None for p in self.members)

    @property
    def compressed_partial(self) -> bool:
        return self.compressed_readable_processes < self.procs

    @property
    def procs(self) -> int:
        return len(self.members)

    @property
    def partial(self) -> bool:
        return self.unreadable_processes > 0

    @property
    def bundle_path(self) -> str | None:
        return self.key.removeprefix("bundle:") if self.key.startswith("bundle:") else None


@dataclass
class _Group:
    key: str
    name: str
    bundle: str | None
    members: list[DarwinProcess] = field(default_factory=lambda: [])
    uncertain: bool = False


def _executable_name(path: str | None, comm: str) -> str:
    """Basename of the executable path, never argv.

    BSD comm holds 16 bytes and is cut at 15 characters, which merges helpers whose
    names share a prefix; it is only the fallback when the path cannot be read.
    """
    name = path.rsplit("/", 1)[-1] if path else ""
    return name or comm or "unknown"


def _macos_major() -> int:
    head = platform.mac_ver()[0].split(".")[0]
    return int(head) if head.isdigit() else 0


def _bundle(path: str | None) -> str | None:
    """Outermost `.app` directory of an executable path.

    A `.app` inside a `.framework` is a tool the framework ships (the Python
    framework's Python.app), not an application the user runs, so it is no bundle.
    """
    if not path or not path.startswith("/"):
        return None
    parts = path.split("/")
    for index, part in enumerate(parts):
        if part.endswith(".framework"):
            return None
        if part.endswith(".app") and part not in (".app", "..app"):
            return "/".join(parts[: index + 1])
    return None


class DarwinBackend:
    """One-shot native reads and bounded ancestry grouping for the current user."""

    def __init__(self, uid: int, native: NativeReader | None = None) -> None:
        if native is None:
            if sys.platform != "darwin" or platform.machine() != "arm64":
                raise DarwinUnavailableError(
                    "macOS 15+ on Apple Silicon with a native arm64 Python is required "
                    f"(this is {sys.platform} {platform.machine()}); "
                    "the memory layouts are only validated there"
                )
            if _macos_major() < 15:
                raise DarwinUnavailableError("macOS 15 or newer is required")
            native = DarwinNative()
        self._native = native
        self._uid = uid

    def check(self) -> None:
        self.read_system()

    def read_system(self) -> HostMemory:
        result = self._native.host()
        if result.value is None:
            raise DarwinReadError(f"native host memory read failed: {result.unavailable}")
        return result.value

    def _read_inventory(self) -> tuple[dict[int, ProcessIdentity], dict[int, DarwinProcess]]:
        result = self._native.pids()
        if result.value is None:
            raise DarwinReadError(f"native process inventory failed: {result.unavailable}")
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
        compressed = self._native.compressed(pid).value
        # Re-reading the identity after the memory read catches a PID reused mid-sample
        # when the parent or owner changed; a second memory read catches it by start time.
        after = self._native.process(pid).value
        if after is None or after.ppid != identity.ppid or after.uid != identity.uid:
            return None
        if memory is not None:
            again = self._native.memory(pid).value
            if again is None or again.start_abstime != memory.start_abstime:
                return None
        unavailable = None if memory is not None else str(memory_result.unavailable or "error")
        return DarwinProcess(
            pid=pid,
            ppid=after.ppid,
            start_abstime=memory.start_abstime if memory is not None else None,
            command=_executable_name(path, after.command),
            footprint_bytes=memory.footprint_bytes if memory is not None else None,
            unavailable=unavailable,
            path=path,
            resident_bytes=memory.resident_bytes if memory is not None else None,
            compressed_bytes=compressed,
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

    def _responsible_bundle(
        self,
        top: int,
        identities: dict[int, ProcessIdentity],
        processes: dict[int, DarwinProcess],
        direct_bundles: dict[int, str],
    ) -> str | None:
        """The app bundle macOS holds responsible for a launchd-parented helper, if any.

        The answer is a plain pid, so it is only trusted when it is another process of
        this sample that started no later than the helper (a reused pid starts later).
        """
        owner = self._native.responsible(top).value
        if owner is None or owner == top or owner not in processes:
            return None
        started, owner_started = processes[top].start_abstime, processes[owner].start_abstime
        if started is None or owner_started is None or owner_started > started:
            return None
        chain, _ = self._ancestry(processes[owner], identities, processes)
        return next((direct_bundles[item] for item in chain if item in direct_bundles), None)

    def _group_processes(
        self, identities: dict[int, ProcessIdentity], processes: dict[int, DarwinProcess]
    ) -> dict[str, _Group]:
        direct_bundles = {
            pid: bundle
            for pid, process in processes.items()
            if (bundle := _bundle(process.path)) is not None
        }

        groups: dict[str, _Group] = {}
        owners: dict[int, str | None] = {}
        for process in processes.values():
            chain, missing = self._ancestry(process, identities, processes)
            bundle = next((direct_bundles[item] for item in chain if item in direct_bundles), None)
            via: Via = "bundle" if process.pid in direct_bundles else "ancestry"
            top = chain[-1]
            if bundle is None and processes[top].ppid == 1:
                # Only a process launchd itself parents has no ancestry to follow: XPC services.
                if top not in owners:
                    owners[top] = self._responsible_bundle(
                        top, identities, processes, direct_bundles
                    )
                bundle, via = owners[top], "responsible"
            if bundle is not None:
                key = f"bundle:{bundle}"
                name = bundle.rsplit("/", 1)[-1].removesuffix(".app") or "unknown"
            else:
                # Same-named roots with no bundle are one app, as Linux merges units by name.
                name, via = processes[top].command, "root"
                key = f"session:{name}"
            group = groups.setdefault(key, _Group(key, name, bundle))
            group.members.append(replace(process, via=via))
            if missing or process.path is None or process.start_abstime is None:
                group.uncertain = True
        return groups

    def _app(self, group: _Group, *, shared_name: bool) -> DarwinApp:
        """Two bundles with one display name are told apart by their bundle id,
        which stays with the bundle when the twin quits (unlike an ordinal)."""
        bundle_id = None
        if group.bundle is not None and shared_name:
            bundle_id = self._native.bundle_id(group.bundle).value
        members = sorted(group.members, key=lambda item: item.pid)
        readable = [item.footprint_bytes for item in members if item.footprint_bytes is not None]
        return DarwinApp(
            id=hashlib.sha256(group.key.encode()).hexdigest()[:12],
            key=group.key,
            name=f"{group.name} ({bundle_id})" if bundle_id else group.name,
            display_name=group.name,
            bundle_id=bundle_id,
            footprint_bytes=sum(readable) if readable else None,
            readable_processes=len(readable),
            unreadable_processes=len(members) - len(readable),
            members=tuple(members),
            grouping_partial=group.uncertain,
        )

    def collect_apps(self) -> list[DarwinApp]:
        identities, processes = self._read_inventory()
        groups = self._group_processes(identities, processes)
        shared = Counter(group.name for group in groups.values())
        apps = [self._app(group, shared_name=shared[group.name] > 1) for group in groups.values()]
        return sorted(
            apps,
            key=lambda app: (
                app.footprint_bytes is None,
                -(app.footprint_bytes or 0),
                app.name,
                app.id,
            ),
        )

    def lookup(self, name: str) -> tuple[DarwinApp | None, list[DarwinApp]]:
        """The app a name or id selects, or None with the apps that nearly match.

        An id never changes. A name does when a same-named twin starts or quits, so
        the plain display name still finds an app whose name carries a bundle id,
        and `Name (bundle id)` still finds it after the twin is gone.
        """
        apps = self.collect_apps()
        exact = next((app for app in apps if app.id == name or app.name == name), None)
        if exact is not None:
            return exact, []
        plain = [app for app in apps if app.display_name == name]
        if len(plain) == 1:
            return plain[0], []
        qualified = re.fullmatch(r"(.+) \((.+)\)", name)
        if qualified is not None:
            display, wanted = qualified.groups()
            for app in apps:
                path = app.bundle_path
                if (
                    app.display_name == display
                    and path is not None
                    and self._native.bundle_id(path).value == wanted
                ):
                    return app, []
        needle = name.casefold()
        return None, [app for app in apps if needle in app.name.casefold()]

    def find_app(self, name: str) -> DarwinApp | None:
        return self.lookup(name)[0]

    def read_procs(self, app: DarwinApp) -> tuple[DarwinProcess, ...]:
        return app.members
