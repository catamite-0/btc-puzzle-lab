"""Inventory-blind discovery of physical host capabilities.

The planner needs facts about the machine it is running on, not a report of
which solver binaries happen to be installed.  This module therefore probes
only Linux resource limits, filesystem capacity, and NVIDIA device topology.
It never inspects compilers, CUDA toolkits, engine directories, or executable
solver inventory.

Every operating-system interaction is carried by ``HostDiscoveryDependencies``
so plan-only callers and tests can supply a closed, deterministic boundary.
"""

from __future__ import annotations

import csv
import io
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from btc_puzzle_lab.autopilot.facts import (
    DomainValidationError,
    GpuDevice,
    HostCapabilities,
)

_MEMINFO = Path("/proc/meminfo")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_PROC_SELF_MOUNTINFO = Path("/proc/self/mountinfo")
_V1_UNLIMITED_FLOOR = 1 << 60
_NVIDIA_TIMEOUT_SECONDS = 5.0
_MAX_COMMAND_OUTPUT = 1_048_576
_MAX_CGROUP_METADATA_BYTES = 1_048_576
_MIB = 1024 * 1024
_COMPUTE_CAPABILITY_RE = re.compile(r"^(?P<major>[1-9][0-9]*)\.(?P<minor>[0-9]+)$")
_UNSIGNED_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_SIGNED_INTEGER_RE = re.compile(r"^(?:-1|0|[1-9][0-9]*)$")
_MOUNTINFO_ESCAPES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}


class HostDiscoveryCode(StrEnum):
    """Stable, non-sensitive failure categories for host discovery."""

    ARCHITECTURE_UNAVAILABLE = "architecture_unavailable"
    CPU_UNAVAILABLE = "cpu_unavailable"
    MEMORY_UNAVAILABLE = "memory_unavailable"
    CGROUP_INVALID = "cgroup_invalid"
    DISK_INVALID = "disk_invalid"
    NVIDIA_PROBE_FAILED = "nvidia_probe_failed"
    NVIDIA_OUTPUT_INVALID = "nvidia_output_invalid"
    FACTS_INVALID = "facts_invalid"


class HostDiscoveryError(RuntimeError):
    """Physical host facts could not be discovered without guessing."""

    def __init__(self, code: HostDiscoveryCode, message: str) -> None:
        if not isinstance(code, HostDiscoveryCode):
            raise TypeError("code must be a HostDiscoveryCode")
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded command-runner result used by the injected NVIDIA probe."""

    returncode: int
    stdout: str
    stderr: str = ""

    def __post_init__(self) -> None:
        if type(self.returncode) is not int:
            raise TypeError("returncode must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("command output must be text")
        if len(self.stdout.encode("utf-8")) > _MAX_COMMAND_OUTPUT:
            raise ValueError("stdout exceeds the discovery output limit")
        if len(self.stderr.encode("utf-8")) > _MAX_COMMAND_OUTPUT:
            raise ValueError("stderr exceeds the discovery output limit")


def _system_affinity() -> Collection[int] | None:
    try:
        return os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return None


def _system_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _system_disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _system_cuda_visible_devices() -> str | None:
    """Read CUDA's process-visible device selector without consulting tool inventory.

    Semantics are pinned to NVIDIA's CUDA Programming Guide, "Device
    Enumeration and Properties":
    https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html
    """

    return os.environ.get("CUDA_VISIBLE_DEVICES")


def _system_nvidia_present() -> bool:
    """Return whether Linux exposes a physical NVIDIA device or driver."""

    indicators = (
        Path("/proc/driver/nvidia/version"),
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia0"),
    )
    if any(path.exists() for path in indicators):
        return True
    try:
        vendor_paths = Path("/sys/bus/pci/devices").glob("*/vendor")
        return any(
            path.read_text(encoding="ascii").strip().lower() == "0x10de" for path in vendor_paths
        )
    except OSError:
        return False


def _system_run(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_seconds,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class HostDiscoveryDependencies:
    """Injected boundary for every external observation made by discovery."""

    architecture: Callable[[], str] = platform.machine
    logical_cpu_count: Callable[[], int | None] = os.cpu_count
    cpu_affinity: Callable[[], Collection[int] | None] = _system_affinity
    read_text: Callable[[Path], str] = _system_read_text
    disk_free_bytes: Callable[[Path], int] = _system_disk_free
    cuda_visible_devices: Callable[[], str | None] = _system_cuda_visible_devices
    find_executable: Callable[[str], str | None] = shutil.which
    nvidia_device_present: Callable[[], bool] = _system_nvidia_present
    run_command: Callable[[tuple[str, ...], float], CommandResult] = _system_run


@dataclass(frozen=True, slots=True)
class _CgroupMembership:
    version: int
    path: PurePosixPath
    controllers: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CgroupMount:
    version: int
    root: PurePosixPath
    mount_point: PurePosixPath
    controllers: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CgroupLimits:
    cpu_counts: tuple[int, ...]
    memory_bytes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _GpuProbe:
    index: int
    device: GpuDevice
    mig_mode: str


def _required_metadata_text(dependencies: HostDiscoveryDependencies, path: Path) -> str:
    try:
        raw = dependencies.read_text(path)
    except (OSError, UnicodeError) as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"cannot safely read cgroup metadata {path}",
        ) from exc
    try:
        encoded_size = len(raw.encode("utf-8")) if type(raw) is str else None
    except UnicodeError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"cgroup metadata {path} is not valid UTF-8 text",
        ) from exc
    if encoded_size is None or encoded_size > _MAX_CGROUP_METADATA_BYTES:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"cgroup metadata {path} is not bounded text",
        )
    return raw


def _optional_text(dependencies: HostDiscoveryDependencies, path: Path) -> str | None:
    try:
        return dependencies.read_text(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"cannot safely read resource constraint {path}",
        ) from exc


def _canonical_absolute_path(raw: str, *, label: str) -> PurePosixPath:
    if (
        type(raw) is not str
        or not raw.startswith("/")
        or raw.startswith("//")
        or (raw != "/" and raw.endswith("/"))
        or len(raw.encode("utf-8")) > 4096
        or any(ord(character) < 32 for character in raw)
    ):
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"{label} is not a canonical absolute path",
        )
    path = PurePosixPath(raw)
    if path.as_posix() != raw or any(part in (".", "..") for part in path.parts):
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"{label} is not a canonical absolute path",
        )
    return path


def _decode_mountinfo_path(raw: str, *, label: str) -> PurePosixPath:
    decoded: list[str] = []
    index = 0
    while index < len(raw):
        character = raw[index]
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        escape = raw[index + 1 : index + 4]
        if len(escape) != 3 or escape not in _MOUNTINFO_ESCAPES:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                f"{label} contains an unsupported mountinfo escape",
            )
        decoded.append(_MOUNTINFO_ESCAPES[escape])
        index += 4
    return _canonical_absolute_path("".join(decoded), label=label)


def _parse_cgroup_memberships(raw: str) -> tuple[_CgroupMembership, ...]:
    if not raw:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            "/proc/self/cgroup is empty",
        )
    memberships: list[_CgroupMembership] = []
    seen_controllers: set[str] = set()
    seen_v2 = False
    for line in raw.splitlines():
        if not line:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "/proc/self/cgroup contains an empty record",
            )
        fields = line.split(":", 2)
        if len(fields) != 3:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "/proc/self/cgroup has an unsupported schema",
            )
        hierarchy_raw, controllers_raw, path_raw = fields
        if not _UNSIGNED_INTEGER_RE.fullmatch(hierarchy_raw):
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "cgroup hierarchy id is not canonical",
            )
        hierarchy = int(hierarchy_raw)
        path = _canonical_absolute_path(path_raw, label="cgroup membership path")
        if path_raw.endswith(" (deleted)"):
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "current cgroup membership was deleted during discovery",
            )
        if hierarchy == 0:
            if controllers_raw or seen_v2:
                raise HostDiscoveryError(
                    HostDiscoveryCode.CGROUP_INVALID,
                    "cgroup v2 membership is malformed or duplicated",
                )
            controllers = frozenset()
            seen_v2 = True
            version = 2
        else:
            controller_items = controllers_raw.split(",") if controllers_raw else []
            if (
                not controller_items
                or any(
                    not item or any(character.isspace() for character in item)
                    for item in controller_items
                )
                or len(set(controller_items)) != len(controller_items)
            ):
                raise HostDiscoveryError(
                    HostDiscoveryCode.CGROUP_INVALID,
                    "cgroup v1 controller list is malformed",
                )
            controllers = frozenset(controller_items)
            if seen_controllers.intersection(controllers):
                raise HostDiscoveryError(
                    HostDiscoveryCode.CGROUP_INVALID,
                    "cgroup v1 controller membership is ambiguous",
                )
            seen_controllers.update(controllers)
            version = 1
        memberships.append(_CgroupMembership(version=version, path=path, controllers=controllers))
    return tuple(memberships)


def _parse_cgroup_mounts(raw: str) -> tuple[_CgroupMount, ...]:
    mounts: list[_CgroupMount] = []
    for line in raw.splitlines():
        if not line:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "/proc/self/mountinfo contains an empty record",
            )
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "/proc/self/mountinfo has no field separator",
            ) from None
        if separator < 6 or len(fields) < separator + 4:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "/proc/self/mountinfo has an unsupported schema",
            )
        filesystem_type = fields[separator + 1]
        if filesystem_type not in {"cgroup", "cgroup2"}:
            continue
        root = _decode_mountinfo_path(fields[3], label="cgroup mount root")
        mount_point = _decode_mountinfo_path(fields[4], label="cgroup mount point")
        super_options = frozenset(fields[separator + 3].split(","))
        mounts.append(
            _CgroupMount(
                version=2 if filesystem_type == "cgroup2" else 1,
                root=root,
                mount_point=mount_point,
                controllers=(frozenset() if filesystem_type == "cgroup2" else super_options),
            )
        )
    return tuple(mounts)


def _membership_constraint_paths(
    memberships: tuple[_CgroupMembership, ...],
    mounts: tuple[_CgroupMount, ...],
    *,
    controller: str,
) -> tuple[tuple[int, Path], ...]:
    relevant = tuple(
        membership
        for membership in memberships
        if membership.version == 2 or controller in membership.controllers
    )
    paths: set[tuple[int, Path]] = set()
    for membership in relevant:
        matching_mounts = tuple(
            mount
            for mount in mounts
            if mount.version == membership.version
            and (mount.version == 2 or controller in mount.controllers)
        )
        mapped = False
        for mount in matching_mounts:
            try:
                relative = membership.path.relative_to(mount.root)
            except ValueError:
                continue
            mapped = True
            components = relative.parts
            for length in range(len(components) + 1):
                directory = Path(mount.mount_point.joinpath(*components[:length]).as_posix())
                try:
                    directory.relative_to(Path(mount.mount_point.as_posix()))
                except ValueError:
                    raise HostDiscoveryError(
                        HostDiscoveryCode.CGROUP_INVALID,
                        "cgroup constraint path escapes its mounted hierarchy",
                    ) from None
                paths.add((membership.version, directory))
        if not mapped:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                f"current cgroup {controller} membership has no matching mount",
            )
    return tuple(sorted(paths, key=lambda item: (item[0], str(item[1]))))


def _positive_integer(raw: str, *, label: str) -> int:
    if type(raw) is not str or not _UNSIGNED_INTEGER_RE.fullmatch(raw):
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"{label} is not a canonical integer",
        )
    value = int(raw)
    if value <= 0:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"{label} must be positive",
        )
    return value


def _cpu_quota_counts(
    dependencies: HostDiscoveryDependencies,
    memberships: tuple[_CgroupMembership, ...],
    mounts: tuple[_CgroupMount, ...],
) -> tuple[int, ...]:
    counts: list[int] = []
    for version, directory in _membership_constraint_paths(memberships, mounts, controller="cpu"):
        if version == 2:
            raw = _optional_text(dependencies, directory / "cpu.max")
            if raw is None:
                continue
            fields = raw.strip().split()
            if len(fields) != 2:
                raise HostDiscoveryError(
                    HostDiscoveryCode.CGROUP_INVALID,
                    "cgroup v2 cpu.max has an unsupported schema",
                )
            quota_raw, period_raw = fields
            period = _positive_integer(period_raw, label="cgroup v2 CPU period")
            if quota_raw != "max":
                quota = _positive_integer(quota_raw, label="cgroup v2 CPU quota")
                counts.append(max(1, quota // period))
            continue

        raw_quota = _optional_text(dependencies, directory / "cpu.cfs_quota_us")
        raw_period = _optional_text(dependencies, directory / "cpu.cfs_period_us")
        if (raw_quota is None) != (raw_period is None):
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "cgroup v1 CPU quota and period must be observed together",
            )
        if raw_quota is None or raw_period is None:
            continue
        quota_text = raw_quota.strip()
        if not _SIGNED_INTEGER_RE.fullmatch(quota_text):
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "cgroup v1 CPU quota is not a canonical integer",
            )
        quota = int(quota_text)
        period = _positive_integer(raw_period.strip(), label="cgroup v1 CPU period")
        if quota == 0:
            raise HostDiscoveryError(
                HostDiscoveryCode.CGROUP_INVALID,
                "cgroup v1 CPU quota is invalid",
            )
        if quota > 0:
            counts.append(max(1, quota // period))
    return tuple(counts)


def _discover_cpu_count(
    dependencies: HostDiscoveryDependencies,
    cgroup_limits: _CgroupLimits,
) -> int:
    candidates: list[int] = []
    try:
        logical_count = dependencies.logical_cpu_count()
    except OSError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.CPU_UNAVAILABLE,
            "logical CPU count could not be read",
        ) from exc
    if logical_count is not None:
        if type(logical_count) is not int or logical_count <= 0:
            raise HostDiscoveryError(
                HostDiscoveryCode.CPU_UNAVAILABLE,
                "logical CPU count is invalid",
            )
        candidates.append(logical_count)

    try:
        affinity = dependencies.cpu_affinity()
    except OSError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.CPU_UNAVAILABLE,
            "CPU affinity could not be read",
        ) from exc
    if affinity is not None:
        try:
            affinity_count = len(frozenset(affinity))
        except (TypeError, ValueError) as exc:
            raise HostDiscoveryError(
                HostDiscoveryCode.CPU_UNAVAILABLE,
                "CPU affinity is invalid",
            ) from exc
        if affinity_count <= 0:
            raise HostDiscoveryError(
                HostDiscoveryCode.CPU_UNAVAILABLE,
                "CPU affinity must contain at least one CPU",
            )
        candidates.append(affinity_count)

    candidates.extend(cgroup_limits.cpu_counts)
    if not candidates:
        raise HostDiscoveryError(
            HostDiscoveryCode.CPU_UNAVAILABLE,
            "no trustworthy CPU capacity observation is available",
        )
    return min(candidates)


def _physical_memory_bytes(dependencies: HostDiscoveryDependencies) -> int:
    try:
        raw = dependencies.read_text(_MEMINFO)
    except OSError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.MEMORY_UNAVAILABLE,
            "physical memory total could not be read",
        ) from exc
    for line in raw.splitlines():
        if not line.startswith("MemTotal:"):
            continue
        fields = line.split()
        if len(fields) != 3 or fields[0] != "MemTotal:" or fields[2] != "kB":
            break
        try:
            total_kib = int(fields[1])
        except ValueError:
            break
        if total_kib > 0:
            return total_kib * 1024
        break
    raise HostDiscoveryError(
        HostDiscoveryCode.MEMORY_UNAVAILABLE,
        "/proc/meminfo does not contain a canonical MemTotal",
    )


def _memory_limit_bytes(raw: str, *, version: int, label: str) -> int | None:
    value = raw.strip()
    if version == 2 and value == "max":
        return None
    if not _UNSIGNED_INTEGER_RE.fullmatch(value):
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"{label} is not a canonical integer",
        )
    limit = int(value)
    if version == 1 and limit >= _V1_UNLIMITED_FLOOR:
        return None
    if limit <= 0:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            f"{label} must be positive or explicitly unlimited",
        )
    return limit


def _cgroup_memory_limits(
    dependencies: HostDiscoveryDependencies,
    memberships: tuple[_CgroupMembership, ...],
    mounts: tuple[_CgroupMount, ...],
) -> tuple[int, ...]:
    limits: list[int] = []
    for version, directory in _membership_constraint_paths(
        memberships, mounts, controller="memory"
    ):
        filename = "memory.max" if version == 2 else "memory.limit_in_bytes"
        raw = _optional_text(dependencies, directory / filename)
        if raw is None:
            continue
        limit = _memory_limit_bytes(raw, version=version, label=f"cgroup {filename}")
        if limit is not None:
            limits.append(limit)
    return tuple(limits)


def _discover_cgroup_limits(dependencies: HostDiscoveryDependencies) -> _CgroupLimits:
    membership_before = _required_metadata_text(dependencies, _PROC_SELF_CGROUP)
    mountinfo_before = _required_metadata_text(dependencies, _PROC_SELF_MOUNTINFO)
    memberships = _parse_cgroup_memberships(membership_before)
    mounts = _parse_cgroup_mounts(mountinfo_before)
    cpu_counts = _cpu_quota_counts(dependencies, memberships, mounts)
    memory_bytes = _cgroup_memory_limits(dependencies, memberships, mounts)
    membership_after = _required_metadata_text(dependencies, _PROC_SELF_CGROUP)
    mountinfo_after = _required_metadata_text(dependencies, _PROC_SELF_MOUNTINFO)
    if membership_after != membership_before or mountinfo_after != mountinfo_before:
        raise HostDiscoveryError(
            HostDiscoveryCode.CGROUP_INVALID,
            "cgroup membership or mounts changed during discovery",
        )
    return _CgroupLimits(cpu_counts=cpu_counts, memory_bytes=memory_bytes)


def _discover_memory_bytes(
    dependencies: HostDiscoveryDependencies,
    cgroup_limits: _CgroupLimits,
) -> int:
    candidates = [_physical_memory_bytes(dependencies), *cgroup_limits.memory_bytes]
    return min(candidates)


def _parse_compute_capability(value: str) -> tuple[int, int]:
    match = _COMPUTE_CAPABILITY_RE.fullmatch(value)
    if match is None:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
            "NVIDIA compute capability is not canonical",
        )
    return int(match.group("major")), int(match.group("minor"))


def _parse_gpu_output(stdout: str) -> tuple[_GpuProbe, ...]:
    try:
        rows = tuple(csv.reader(io.StringIO(stdout), strict=True))
    except csv.Error as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
            "nvidia-smi returned malformed CSV",
        ) from exc
    probes: list[_GpuProbe] = []
    for row in rows:
        if not row or all(not field.strip() for field in row):
            continue
        if len(row) != 7:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
                "nvidia-smi returned an unsupported GPU schema",
            )
        index_raw, device_id, name, memory_mib_raw, capability_raw, sm_count_raw, mig_mode = (
            field.strip() for field in row
        )
        if not device_id.startswith("GPU-") or not name:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
                "NVIDIA GPU UUID and name must be canonical",
            )
        try:
            index = int(index_raw)
            memory_mib = int(memory_mib_raw)
            sm_count = int(sm_count_raw)
        except ValueError as exc:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
                "NVIDIA index, memory, and multiprocessor count must be integers",
            ) from exc
        if not _UNSIGNED_INTEGER_RE.fullmatch(index_raw) or memory_mib <= 0 or sm_count <= 0:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
                "NVIDIA index must be canonical and resource counts must be positive",
            )
        if mig_mode not in {"Disabled", "Enabled", "N/A"}:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
                "NVIDIA MIG mode is not a supported canonical value",
            )
        try:
            probes.append(
                _GpuProbe(
                    index=index,
                    device=GpuDevice(
                        device_id=device_id,
                        name=name,
                        memory_bytes=memory_mib * _MIB,
                        compute_capability=_parse_compute_capability(capability_raw),
                        multiprocessor_count=sm_count,
                    ),
                    mig_mode=mig_mode,
                )
            )
        except DomainValidationError as exc:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
                "nvidia-smi returned invalid GPU facts",
            ) from exc
    if len({probe.index for probe in probes}) != len(probes) or len(
        {probe.device.device_id for probe in probes}
    ) != len(probes):
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_OUTPUT_INVALID,
            "nvidia-smi returned duplicate GPU indices or UUIDs",
        )
    return tuple(sorted(probes, key=lambda probe: probe.index))


def _cuda_visibility(dependencies: HostDiscoveryDependencies) -> str | None:
    try:
        value = dependencies.cuda_visible_devices()
    except (OSError, UnicodeError) as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "CUDA device visibility could not be determined",
        ) from exc
    if value is None:
        return None
    try:
        encoded_size = len(value.encode("utf-8")) if type(value) is str else None
    except UnicodeError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "CUDA_VISIBLE_DEVICES is not valid UTF-8 text",
        ) from exc
    if (
        encoded_size is None
        or encoded_size > 4096
        or any(ord(character) < 32 for character in value)
    ):
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "CUDA_VISIBLE_DEVICES is not bounded canonical text",
        )
    return value


def _visible_gpu_devices(
    probes: tuple[_GpuProbe, ...],
    visibility: str | None,
) -> tuple[GpuDevice, ...]:
    if visibility is None:
        selected = probes
    else:
        tokens = visibility.split(",")
        if not tokens or any(not token for token in tokens):
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                "CUDA_VISIBLE_DEVICES contains an empty device selector",
            )
        if all(_UNSIGNED_INTEGER_RE.fullmatch(token) for token in tokens):
            if len(set(tokens)) != len(tokens):
                raise HostDiscoveryError(
                    HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                    "CUDA_VISIBLE_DEVICES contains duplicate GPU indices",
                )
            by_index = {probe.index: probe for probe in probes}
            requested = tuple(int(token) for token in tokens)
            if any(index not in by_index for index in requested):
                raise HostDiscoveryError(
                    HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                    "CUDA_VISIBLE_DEVICES names an unavailable GPU index",
                )
            selected = tuple(by_index[index] for index in requested)
        elif all(token.startswith("GPU-") for token in tokens):
            resolved: list[_GpuProbe] = []
            for token in tokens:
                matches = tuple(
                    probe for probe in probes if probe.device.device_id.startswith(token)
                )
                if len(matches) != 1:
                    raise HostDiscoveryError(
                        HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                        "CUDA_VISIBLE_DEVICES GPU UUID prefix is unavailable or ambiguous",
                    )
                resolved.append(matches[0])
            if len({probe.device.device_id for probe in resolved}) != len(resolved):
                raise HostDiscoveryError(
                    HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                    "CUDA_VISIBLE_DEVICES resolves more than once to the same GPU",
                )
            selected = tuple(resolved)
        else:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                "CUDA_VISIBLE_DEVICES must use only indices or only GPU UUID prefixes",
            )
    if any(probe.mig_mode == "Enabled" for probe in selected):
        # A MIG compute instance has its own UUID and resource slice.  Whole-card
        # ``--query-gpu`` values are therefore not admissible for it; see NVIDIA's
        # MIG device naming contract:
        # https://docs.nvidia.com/datacenter/tesla/mig-user-guide/mig-device-names.html
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "MIG is enabled; Preparation must provide MIG-aware slice memory and SM discovery",
        )
    return tuple(sorted((probe.device for probe in selected), key=lambda device: device.device_id))


def _discover_gpus(dependencies: HostDiscoveryDependencies) -> tuple[GpuDevice, ...]:
    visibility = _cuda_visibility(dependencies)
    if visibility in {"", "-1"}:
        # CUDA documents an empty value as no visible devices.  ``-1`` also
        # yields an empty prefix before the first invalid index; accepting only
        # this complete sentinel avoids silently truncating malformed lists.
        return ()
    if visibility is not None and visibility.startswith("MIG-"):
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "MIG visibility requires Preparation to provide MIG-aware slice memory and SM discovery",
        )
    visibility_requires_gpu = visibility is not None
    try:
        device_present = dependencies.nvidia_device_present()
    except OSError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "NVIDIA device presence could not be determined",
        ) from exc
    if type(device_present) is not bool:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "NVIDIA presence probe returned an invalid value",
        )

    try:
        executable = dependencies.find_executable("nvidia-smi")
    except OSError as exc:
        if device_present or visibility_requires_gpu:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                "an NVIDIA device is present but nvidia-smi cannot be located",
            ) from exc
        return ()
    if executable is None:
        if device_present or visibility_requires_gpu:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                "an NVIDIA device is present but nvidia-smi is unavailable",
            )
        return ()
    if not isinstance(executable, str) or not executable.strip():
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "the nvidia-smi executable path is invalid",
        )

    command = (
        executable,
        "--query-gpu=index,uuid,name,memory.total,compute_cap,multiprocessor_count,mig.mode.current",
        "--format=csv,noheader,nounits",
    )
    try:
        result = dependencies.run_command(command, _NVIDIA_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, TypeError) as exc:
        if device_present or visibility_requires_gpu:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                "an NVIDIA device is present but nvidia-smi could not run",
            ) from exc
        return ()
    if type(result) is not CommandResult:
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "the NVIDIA command runner returned an invalid result",
        )
    if result.returncode != 0:
        if device_present or visibility_requires_gpu:
            raise HostDiscoveryError(
                HostDiscoveryCode.NVIDIA_PROBE_FAILED,
                "an NVIDIA device is present but nvidia-smi failed",
            )
        return ()
    probes = _parse_gpu_output(result.stdout)
    if not probes and (device_present or visibility_requires_gpu):
        raise HostDiscoveryError(
            HostDiscoveryCode.NVIDIA_PROBE_FAILED,
            "an NVIDIA device is present but nvidia-smi returned no devices",
        )
    return _visible_gpu_devices(probes, visibility)


def _discover_disk_free(
    dependencies: HostDiscoveryDependencies,
    disk_path: Path,
) -> int | None:
    try:
        free = dependencies.disk_free_bytes(disk_path)
    except OSError:
        return None
    if type(free) is not int or free < 0:
        raise HostDiscoveryError(
            HostDiscoveryCode.DISK_INVALID,
            "disk capacity probe returned an invalid value",
        )
    return free


def discover_host(
    *,
    dependencies: HostDiscoveryDependencies | None = None,
    disk_path: Path | None = None,
) -> HostCapabilities:
    """Discover usable physical resources without consulting engine inventory."""

    dependencies = dependencies or HostDiscoveryDependencies()
    if type(dependencies) is not HostDiscoveryDependencies:
        raise TypeError("dependencies must be HostDiscoveryDependencies")
    if disk_path is None:
        disk_path = Path.cwd()
    elif not isinstance(disk_path, Path):
        raise TypeError("disk_path must be a pathlib.Path")

    try:
        architecture = dependencies.architecture()
    except OSError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.ARCHITECTURE_UNAVAILABLE,
            "machine architecture could not be read",
        ) from exc
    if not isinstance(architecture, str) or not architecture.strip():
        raise HostDiscoveryError(
            HostDiscoveryCode.ARCHITECTURE_UNAVAILABLE,
            "machine architecture is unavailable",
        )
    normalized_architecture = architecture.strip().lower()
    normalized_architecture = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }.get(normalized_architecture, normalized_architecture)
    cgroup_limits = _discover_cgroup_limits(dependencies)

    try:
        return HostCapabilities(
            architecture=normalized_architecture,
            cpu_count=_discover_cpu_count(dependencies, cgroup_limits),
            memory_bytes=_discover_memory_bytes(dependencies, cgroup_limits),
            disk_free_bytes=_discover_disk_free(dependencies, disk_path),
            gpus=_discover_gpus(dependencies),
        )
    except DomainValidationError as exc:
        raise HostDiscoveryError(
            HostDiscoveryCode.FACTS_INVALID,
            "discovered host facts are invalid",
        ) from exc


__all__ = [
    "CommandResult",
    "HostDiscoveryCode",
    "HostDiscoveryDependencies",
    "HostDiscoveryError",
    "discover_host",
]
