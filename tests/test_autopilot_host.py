from __future__ import annotations

from pathlib import Path

import pytest

from btc_puzzle_lab.autopilot.facts import GpuDevice, HostCapabilities
from btc_puzzle_lab.autopilot.host import (
    CommandResult,
    HostDiscoveryCode,
    HostDiscoveryDependencies,
    HostDiscoveryError,
    discover_host,
)

GIB = 1024**3
MIB = 1024**2
MEMINFO = Path("/proc/meminfo")
PROC_CGROUP = Path("/proc/self/cgroup")
PROC_MOUNTINFO = Path("/proc/self/mountinfo")
CPU_V2 = Path("/sys/fs/cgroup/cpu.max")
CPU_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
CPU_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
MEMORY_V2 = Path("/sys/fs/cgroup/memory.max")
MEMORY_V1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_V2_ROOT = "0::/\n"
MOUNTINFO_V2_ROOT = "29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
CGROUP_V1_ROOT = "2:cpu,cpuacct:/\n3:memory:/\n"
MOUNTINFO_V1_ROOT = (
    "30 23 0:27 / /sys/fs/cgroup/cpu rw - cgroup cgroup rw,cpu,cpuacct\n"
    "31 23 0:28 / /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n"
)
CGROUP_HYBRID_ROOT = CGROUP_V2_ROOT + CGROUP_V1_ROOT
MOUNTINFO_HYBRID_ROOT = MOUNTINFO_V2_ROOT + MOUNTINFO_V1_ROOT


def _dependencies(
    *,
    files: dict[Path, str] | None = None,
    architecture: str = "x86_64",
    logical_cpus: int | None = 16,
    affinity: set[int] | None = None,
    disk_free: int = 100 * GIB,
    executable: str | None = None,
    nvidia_present: bool = False,
    result: CommandResult | None = None,
    runner=None,
    cgroup: str = CGROUP_V2_ROOT,
    mountinfo: str = MOUNTINFO_V2_ROOT,
    cuda_visible: str | None = None,
) -> HostDiscoveryDependencies:
    observed_files = {
        MEMINFO: "MemTotal:       67108864 kB\n",
        PROC_CGROUP: cgroup,
        PROC_MOUNTINFO: mountinfo,
        CPU_V2: "max 100000\n",
        MEMORY_V2: "max\n",
    }
    if files is not None:
        observed_files.update(files)

    def read_text(path: Path) -> str:
        try:
            return observed_files[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    def run(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        del command, timeout_seconds
        return result or CommandResult(returncode=0, stdout="")

    return HostDiscoveryDependencies(
        architecture=lambda: architecture,
        logical_cpu_count=lambda: logical_cpus,
        cpu_affinity=lambda: affinity if affinity is not None else set(range(8)),
        read_text=read_text,
        disk_free_bytes=lambda path: disk_free,
        cuda_visible_devices=lambda: cuda_visible,
        find_executable=lambda name: executable,
        nvidia_device_present=lambda: nvidia_present,
        run_command=runner or run,
    )


def test_no_gpu_discovery_respects_affinity_v2_cpu_quota_memory_and_disk():
    files = {
        MEMINFO: "MemTotal:       67108864 kB\n",
        CPU_V2: "250000 100000\n",
        MEMORY_V2: str(12 * GIB),
    }
    host = discover_host(
        dependencies=_dependencies(
            files=files,
            architecture="AMD64",
            logical_cpus=32,
            affinity=set(range(6)),
            disk_free=77 * GIB,
        ),
        disk_path=Path("/workspace"),
    )

    assert type(host) is HostCapabilities
    assert host.architecture == "x86_64"
    assert host.cpu_count == 2
    assert host.memory_bytes == 12 * GIB
    assert host.disk_free_bytes == 77 * GIB
    assert host.gpus == ()


def test_v1_cpu_and_memory_limits_are_supported_and_take_the_minimum():
    files = {
        MEMINFO: "MemTotal:       33554432 kB\n",
        CPU_V1_QUOTA: "300000\n",
        CPU_V1_PERIOD: "100000\n",
        MEMORY_V1: str(8 * GIB),
    }
    host = discover_host(
        dependencies=_dependencies(
            files=files,
            logical_cpus=64,
            affinity=set(range(12)),
            cgroup=CGROUP_V1_ROOT,
            mountinfo=MOUNTINFO_V1_ROOT,
        )
    )

    assert host.cpu_count == 3
    assert host.memory_bytes == 8 * GIB


def test_unlimited_v1_resources_do_not_hide_physical_or_affinity_limits():
    files = {
        MEMINFO: "MemTotal:       16777216 kB\n",
        CPU_V1_QUOTA: "-1\n",
        CPU_V1_PERIOD: "100000\n",
        MEMORY_V1: str(1 << 62),
    }
    host = discover_host(
        dependencies=_dependencies(
            files=files,
            logical_cpus=12,
            affinity=set(range(5)),
            cgroup=CGROUP_V1_ROOT,
            mountinfo=MOUNTINFO_V1_ROOT,
        )
    )

    assert host.cpu_count == 5
    assert host.memory_bytes == 16 * GIB


def test_v1_and_v2_constraints_are_both_conservative_when_both_are_visible():
    files = {
        MEMINFO: "MemTotal:       67108864 kB\n",
        CPU_V2: "400000 100000\n",
        CPU_V1_QUOTA: "200000\n",
        CPU_V1_PERIOD: "100000\n",
        MEMORY_V2: str(20 * GIB),
        MEMORY_V1: str(10 * GIB),
    }
    host = discover_host(
        dependencies=_dependencies(
            files=files,
            cgroup=CGROUP_HYBRID_ROOT,
            mountinfo=MOUNTINFO_HYBRID_ROOT,
        )
    )

    assert host.cpu_count == 2
    assert host.memory_bytes == 10 * GIB


def test_nested_v2_membership_maps_through_mountinfo_and_all_ancestors():
    cgroup = "0::/workload/team/job\n"
    mountinfo = "29 23 0:26 / /run/cgroup rw - cgroup2 cgroup rw\n"
    files = {
        MEMINFO: "MemTotal:       67108864 kB\n",
        Path("/run/cgroup/cpu.max"): "max 100000\n",
        Path("/run/cgroup/workload/team/cpu.max"): "600000 100000\n",
        Path("/run/cgroup/workload/team/job/cpu.max"): "250000 100000\n",
        Path("/run/cgroup/memory.max"): "max\n",
        Path("/run/cgroup/workload/team/memory.max"): str(8 * GIB),
        Path("/run/cgroup/workload/team/job/memory.max"): str(12 * GIB),
    }

    host = discover_host(
        dependencies=_dependencies(
            files=files,
            logical_cpus=64,
            affinity=set(range(32)),
            cgroup=cgroup,
            mountinfo=mountinfo,
        )
    )

    assert host.cpu_count == 2
    assert host.memory_bytes == 8 * GIB


def test_nested_v1_memberships_map_relative_to_each_controller_mount_root():
    cgroup = "2:cpu,cpuacct:/tenant/job\n3:memory:/tenant/job\n"
    mountinfo = (
        "30 23 0:27 /tenant /run/cpu rw - cgroup cgroup rw,cpu,cpuacct\n"
        "31 23 0:28 /tenant /run/memory rw - cgroup cgroup rw,memory\n"
    )
    files = {
        MEMINFO: "MemTotal:       67108864 kB\n",
        Path("/run/cpu/cpu.cfs_quota_us"): "400000\n",
        Path("/run/cpu/cpu.cfs_period_us"): "100000\n",
        Path("/run/cpu/job/cpu.cfs_quota_us"): "200000\n",
        Path("/run/cpu/job/cpu.cfs_period_us"): "100000\n",
        Path("/run/memory/memory.limit_in_bytes"): str(10 * GIB),
        Path("/run/memory/job/memory.limit_in_bytes"): str(8 * GIB),
    }

    host = discover_host(
        dependencies=_dependencies(
            files=files,
            logical_cpus=64,
            affinity=set(range(32)),
            cgroup=cgroup,
            mountinfo=mountinfo,
        )
    )

    assert host.cpu_count == 2
    assert host.memory_bytes == 8 * GIB


@pytest.mark.parametrize(
    ("cgroup", "mountinfo"),
    [
        ("0::/tenant/../../etc\n", MOUNTINFO_V2_ROOT),
        ("0::/tenant/job\n", "29 23 0:26 /other /sys/fs/cgroup rw - cgroup2 cgroup rw\n"),
        ("0::/tenant/job\n", "29 23 0:26 / /sys/fs/cgroup/../escape rw - cgroup2 cgroup rw\n"),
    ],
)
def test_malicious_or_unmappable_cgroup_paths_fail_closed(cgroup, mountinfo):
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=_dependencies(cgroup=cgroup, mountinfo=mountinfo))

    assert caught.value.code is HostDiscoveryCode.CGROUP_INVALID


def test_multi_gpu_csv_uses_uuid_mib_units_full_topology_and_canonical_sorting():
    stdout = (
        '1,GPU-b,"NVIDIA Example, Secondary",24576,12.0,170,N/A\n'
        "0,GPU-a,NVIDIA RTX 4090,8192,8.9,128,Disabled\n"
    )
    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        calls.append((command, timeout_seconds))
        return CommandResult(returncode=0, stdout=stdout)

    host = discover_host(
        dependencies=_dependencies(
            executable="/usr/bin/nvidia-smi",
            nvidia_present=True,
            runner=runner,
        )
    )

    assert all(type(gpu) is GpuDevice for gpu in host.gpus)
    assert tuple(gpu.device_id for gpu in host.gpus) == ("GPU-a", "GPU-b")
    assert host.gpus[0].memory_bytes == 8192 * MIB
    assert host.gpus[0].compute_capability == (8, 9)
    assert host.gpus[0].multiprocessor_count == 128
    assert host.gpus[1].name == "NVIDIA Example, Secondary"
    assert host.gpus[1].memory_bytes == 24576 * MIB
    assert host.gpus[1].compute_capability == (12, 0)
    assert host.gpus[1].multiprocessor_count == 170
    assert calls == [
        (
            (
                "/usr/bin/nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total,compute_cap,multiprocessor_count,mig.mode.current",
                "--format=csv,noheader,nounits",
            ),
            5.0,
        )
    ]


@pytest.mark.parametrize("visibility", ["", "-1"])
def test_cuda_visibility_can_explicitly_disable_all_gpus_without_probe(visibility):
    called = False

    def runner(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        nonlocal called
        called = True
        raise AssertionError((command, timeout_seconds))

    host = discover_host(
        dependencies=_dependencies(
            cuda_visible=visibility,
            executable=None,
            nvidia_present=True,
            runner=runner,
        )
    )

    assert host.gpus == ()
    assert not called


def test_cuda_visibility_filters_by_nvidia_index_or_unique_uuid_prefix():
    stdout = "0,GPU-aaaa1111,first,8192,8.9,128,Disabled\n1,GPU-bbbb2222,second,24576,9.0,132,N/A\n"
    result = CommandResult(returncode=0, stdout=stdout)

    by_index = discover_host(
        dependencies=_dependencies(
            cuda_visible="1",
            executable="/usr/bin/nvidia-smi",
            result=result,
        )
    )
    by_uuid = discover_host(
        dependencies=_dependencies(
            cuda_visible="GPU-aaaa",
            executable="/usr/bin/nvidia-smi",
            result=result,
        )
    )

    assert tuple(gpu.device_id for gpu in by_index.gpus) == ("GPU-bbbb2222",)
    assert tuple(gpu.device_id for gpu in by_uuid.gpus) == ("GPU-aaaa1111",)


@pytest.mark.parametrize(
    "visibility",
    ["0,0", "2", "0,GPU-bbbb", " 0", "GPU-aaaa,GPU-aaaa1111", "GPU-"],
)
def test_invalid_duplicate_mixed_or_ambiguous_cuda_visibility_fails_closed(visibility):
    result = CommandResult(
        returncode=0,
        stdout=(
            "0,GPU-aaaa1111,first,8192,8.9,128,Disabled\n"
            "1,GPU-aaaa2222,second,24576,9.0,132,Disabled\n"
        ),
    )

    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(
            dependencies=_dependencies(
                cuda_visible=visibility,
                executable="/usr/bin/nvidia-smi",
                result=result,
            )
        )

    assert caught.value.code is HostDiscoveryCode.NVIDIA_PROBE_FAILED


def test_mig_visibility_or_selected_mig_enabled_gpu_fails_closed():
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=_dependencies(cuda_visible="MIG-deadbeef"))
    assert caught.value.code is HostDiscoveryCode.NVIDIA_PROBE_FAILED
    assert "Preparation" in str(caught.value)
    assert "MIG-aware" in str(caught.value)

    result = CommandResult(
        returncode=0,
        stdout="0,GPU-aaaa1111,sliced,81920,8.0,108,Enabled\n",
    )
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(
            dependencies=_dependencies(
                executable="/usr/bin/nvidia-smi",
                nvidia_present=True,
                result=result,
            )
        )
    assert caught.value.code is HostDiscoveryCode.NVIDIA_PROBE_FAILED
    assert "Preparation" in str(caught.value)
    assert "MIG-aware" in str(caught.value)


def test_absent_nvidia_tool_and_device_is_an_ordinary_cpu_only_host():
    called = False

    def runner(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        nonlocal called
        called = True
        raise AssertionError((command, timeout_seconds))

    host = discover_host(dependencies=_dependencies(runner=runner))

    assert host.gpus == ()
    assert not called


def test_installed_nvidia_tool_failure_without_a_device_signal_is_cpu_only():
    host = discover_host(
        dependencies=_dependencies(
            executable="/usr/bin/nvidia-smi",
            result=CommandResult(returncode=9, stdout="", stderr="no devices"),
        )
    )

    assert host.gpus == ()


@pytest.mark.parametrize(
    ("executable", "result"),
    [
        (None, None),
        ("/usr/bin/nvidia-smi", CommandResult(returncode=1, stdout="")),
        ("/usr/bin/nvidia-smi", CommandResult(returncode=0, stdout="")),
    ],
)
def test_nvidia_device_signal_fails_closed_when_tool_cannot_describe_it(executable, result):
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(
            dependencies=_dependencies(
                executable=executable,
                nvidia_present=True,
                result=result,
            )
        )

    assert caught.value.code is HostDiscoveryCode.NVIDIA_PROBE_FAILED


def test_nvidia_device_signal_fails_closed_when_runner_raises():
    def runner(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
        del command, timeout_seconds
        raise TimeoutError

    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(
            dependencies=_dependencies(
                executable="/usr/bin/nvidia-smi",
                nvidia_present=True,
                runner=runner,
            )
        )

    assert caught.value.code is HostDiscoveryCode.NVIDIA_PROBE_FAILED


@pytest.mark.parametrize(
    "stdout",
    [
        "0,GPU-a,only-six,1024,8.9,10\n",
        "0,GPU-a,name,1.5,8.9,10,Disabled\n",
        "0,GPU-a,name,1024,unknown,10,Disabled\n",
        "0,GPU-a,name,1024,8.9,N/A,Disabled\n",
        "zero,GPU-a,name,1024,8.9,10,Disabled\n",
        "0,MIG-a,name,1024,8.9,10,Enabled\n",
        "0,GPU-a,name,1024,8.9,10,Unknown\n",
        ("0,GPU-a,name,1024,8.9,10,Disabled\n1,GPU-a,other,2048,8.9,20,Disabled\n"),
    ],
)
def test_malformed_or_ambiguous_nvidia_output_is_rejected(stdout):
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(
            dependencies=_dependencies(
                executable="/usr/bin/nvidia-smi",
                nvidia_present=True,
                result=CommandResult(returncode=0, stdout=stdout),
            )
        )

    assert caught.value.code is HostDiscoveryCode.NVIDIA_OUTPUT_INVALID


@pytest.mark.parametrize(
    ("files", "cgroup", "mountinfo"),
    [
        (
            {MEMINFO: "MemTotal: 1 kB\n", CPU_V2: "broken"},
            CGROUP_V2_ROOT,
            MOUNTINFO_V2_ROOT,
        ),
        (
            {MEMINFO: "MemTotal: 1 kB\n", CPU_V2: "0 100000"},
            CGROUP_V2_ROOT,
            MOUNTINFO_V2_ROOT,
        ),
        (
            {MEMINFO: "MemTotal: 1 kB\n", CPU_V1_QUOTA: "100000"},
            CGROUP_V1_ROOT,
            MOUNTINFO_V1_ROOT,
        ),
        (
            {
                MEMINFO: "MemTotal: 1 kB\n",
                CPU_V1_QUOTA: "-2",
                CPU_V1_PERIOD: "100000",
            },
            CGROUP_V1_ROOT,
            MOUNTINFO_V1_ROOT,
        ),
        (
            {MEMINFO: "MemTotal: 1 kB\n", MEMORY_V2: "infinity"},
            CGROUP_V2_ROOT,
            MOUNTINFO_V2_ROOT,
        ),
    ],
)
def test_malformed_cgroup_limits_fail_closed(files, cgroup, mountinfo):
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=_dependencies(files=files, cgroup=cgroup, mountinfo=mountinfo))

    assert caught.value.code is HostDiscoveryCode.CGROUP_INVALID


def test_empty_cgroup_membership_file_fails_closed():
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=_dependencies(cgroup=""))

    assert caught.value.code is HostDiscoveryCode.CGROUP_INVALID


def test_unreadable_or_changing_cgroup_metadata_fails_closed():
    base = _dependencies()

    def dependencies_with_reader(reader):
        return HostDiscoveryDependencies(
            architecture=base.architecture,
            logical_cpu_count=base.logical_cpu_count,
            cpu_affinity=base.cpu_affinity,
            read_text=reader,
            disk_free_bytes=base.disk_free_bytes,
            cuda_visible_devices=base.cuda_visible_devices,
            find_executable=base.find_executable,
            nvidia_device_present=base.nvidia_device_present,
            run_command=base.run_command,
        )

    def unreadable(path: Path) -> str:
        if path == PROC_CGROUP:
            raise PermissionError(path)
        return base.read_text(path)

    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=dependencies_with_reader(unreadable))
    assert caught.value.code is HostDiscoveryCode.CGROUP_INVALID

    membership_reads = 0

    def moving(path: Path) -> str:
        nonlocal membership_reads
        if path == PROC_CGROUP:
            membership_reads += 1
            return CGROUP_V2_ROOT if membership_reads == 1 else "0::/moved\n"
        return base.read_text(path)

    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=dependencies_with_reader(moving))
    assert caught.value.code is HostDiscoveryCode.CGROUP_INVALID


@pytest.mark.parametrize(
    "meminfo",
    ["", "MemFree: 100 kB\n", "MemTotal: unknown kB\n", "MemTotal: 0 kB\n"],
)
def test_missing_or_invalid_physical_memory_fails_closed(meminfo):
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=_dependencies(files={MEMINFO: meminfo}))

    assert caught.value.code is HostDiscoveryCode.MEMORY_UNAVAILABLE


def test_cpu_requires_at_least_one_trustworthy_positive_observation():
    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(
            dependencies=_dependencies(
                logical_cpus=None,
                affinity=set(),
            )
        )

    assert caught.value.code is HostDiscoveryCode.CPU_UNAVAILABLE


def test_unreadable_disk_is_recorded_as_unknown_but_invalid_value_is_rejected():
    def unreadable(path: Path) -> int:
        raise OSError(path)

    dependencies = _dependencies()
    unreadable_dependencies = HostDiscoveryDependencies(
        architecture=dependencies.architecture,
        logical_cpu_count=dependencies.logical_cpu_count,
        cpu_affinity=dependencies.cpu_affinity,
        read_text=dependencies.read_text,
        disk_free_bytes=unreadable,
        cuda_visible_devices=dependencies.cuda_visible_devices,
        find_executable=dependencies.find_executable,
        nvidia_device_present=dependencies.nvidia_device_present,
        run_command=dependencies.run_command,
    )
    assert discover_host(dependencies=unreadable_dependencies).disk_free_bytes is None

    with pytest.raises(HostDiscoveryError) as caught:
        discover_host(dependencies=_dependencies(disk_free=-1))
    assert caught.value.code is HostDiscoveryCode.DISK_INVALID
