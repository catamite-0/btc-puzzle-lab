"""Source-derived RCKangaroo allocation floors used during selection."""

from typing import Final

_KANGAROOS_PER_SM: Final = 256 * 24
_GPU_FIXED_BYTES: Final = 13_551_640
_GPU_BYTES_PER_KANGAROO: Final = 3_132
_GPU_BYTES_PER_SM: Final = 2_048
_HOST_PREFIX_TABLE_BYTES: Final = 12 * 256**3
_HOST_GLOBAL_DP_BUFFERS_BYTES: Final = 2 * 512 * 1024 * 48
_HOST_DP_BUFFER_BYTES_PER_GPU: Final = 256 * 1024 * 48
_HOST_POINT_BYTES_PER_KANGAROO: Final = 96
_MAX_SM_COUNT: Final = 256


def _device_allocation(*, sm_count: int, compute_capability: tuple[int, int]) -> tuple[int, int]:
    if type(sm_count) is not int or not 1 <= sm_count <= _MAX_SM_COUNT:
        raise ValueError("sm_count must be an integer in 1..256")
    if type(compute_capability) is not tuple or compute_capability not in ((8, 9), (12, 0)):
        raise ValueError("unsupported compute capability")
    if any(type(part) is not int for part in compute_capability):
        raise ValueError("compute capability parts must be integers")

    inverse_sms = max(1, sm_count // (32 if compute_capability == (8, 9) else 24))
    allocated_kangaroos = _KANGAROOS_PER_SM * (sm_count - inverse_sms)
    if allocated_kangaroos < 1:
        raise ValueError("sm_count leaves no SM for allocated kangaroos")
    gpu_bytes = (
        _GPU_BYTES_PER_KANGAROO * allocated_kangaroos
        + _GPU_BYTES_PER_SM * sm_count
        + _GPU_FIXED_BYTES
    )
    return allocated_kangaroos, gpu_bytes


def rck_base_allocation_bytes(
    *, sm_count: int, compute_capability: tuple[int, int]
) -> tuple[int, int]:
    """Return conservative ``(host startup, GPU)`` allocation floors.

    These pinned-source values intentionally exclude CUDA/driver overhead and
    dynamic distinguished-point memory, so they can reject impossible hardware
    but cannot authorize execution.
    """

    allocated_kangaroos, gpu_bytes = _device_allocation(
        sm_count=sm_count, compute_capability=compute_capability
    )
    host_steady_bytes = (
        _HOST_PREFIX_TABLE_BYTES
        + _HOST_GLOBAL_DP_BUFFERS_BYTES
        + _HOST_DP_BUFFER_BYTES_PER_GPU
        + _HOST_POINT_BYTES_PER_KANGAROO * allocated_kangaroos
    )
    host_startup_bytes = host_steady_bytes + _HOST_POINT_BYTES_PER_KANGAROO * allocated_kangaroos
    return host_startup_bytes, gpu_bytes
