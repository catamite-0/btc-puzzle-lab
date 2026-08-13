"""First-class external solver toolchain (clone → build → workspace bin/).

Production path for this lab: orchestrate upstream puzzle solvers under the
workspace instead of asking operators to wire someone else's KEYHUNT_PATH by hand.

Upstream projects stay external (their licenses apply). We do not vendor their
source in git — only build artifacts under ignored ``vendor/`` + ``bin/``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.paths import workspace_root

# Pinned default remotes (override via BTC_PUZZLE_LAB_*_REPO env if needed).
KEYHUNT_REPO = os.environ.get(
    "BTC_PUZZLE_LAB_KEYHUNT_REPO", "https://github.com/albertobsd/keyhunt.git"
)
KANGAROO_REPO = os.environ.get(
    "BTC_PUZZLE_LAB_KANGAROO_REPO", "https://github.com/JeanLucPons/Kangaroo.git"
)
BITCRACK_REPO = os.environ.get(
    "BTC_PUZZLE_LAB_BITCRACK_REPO", "https://github.com/brichard19/BitCrack.git"
)
RCKANGAROO_REPO = os.environ.get(
    "BTC_PUZZLE_LAB_RCKANGAROO_REPO", "https://github.com/RetiredC/RCKangaroo.git"
)

# Pinned upstream revisions. Tracking a moving default branch means two hosts can
# install different solvers on different days with no record of which — and an
# upstream output-format change lands silently. Override per engine to move.
PINNED_COMMITS = {
    "keyhunt": os.environ.get(
        "BTC_PUZZLE_LAB_KEYHUNT_COMMIT", "2134a2024e524775b13f82aa1fa07b1c8053f867"
    ),
    "kangaroo": os.environ.get(
        "BTC_PUZZLE_LAB_KANGAROO_COMMIT", "37576c82d198c20fca65b14da74138ae6153a446"
    ),
    "bitcrack": os.environ.get(
        "BTC_PUZZLE_LAB_BITCRACK_COMMIT", "6bf8059ef075eb1622298395866b0bd02375e1d9"
    ),
    "rckangaroo": os.environ.get(
        "BTC_PUZZLE_LAB_RCKANGAROO_COMMIT", "618473acd7f5e696be5ec4f8377c18cc55c1361c"
    ),
}

INSTALLABLE = ("keyhunt", "kangaroo", "bitcrack", "rckangaroo")
MANUAL_ONLY: dict[str, str] = {}

ENGINE_ENV_VARS = {
    "keyhunt": "KEYHUNT_PATH",
    "kangaroo": "KANGAROO_PATH",
    "bitcrack": "BITCRACK_PATH",
    "rckangaroo": "RCKANGAROO_PATH",
}


@dataclass(frozen=True)
class InstallResult:
    name: str
    ok: bool
    binary: Path | None
    message: str


def vendor_dir() -> Path:
    return workspace_root() / "vendor"


def bin_dir() -> Path:
    return workspace_root() / "bin"


def engines_env_path() -> Path:
    return workspace_root() / "config" / "engines.env"


def _run(cmd: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode, out


def _which_ok(name: str) -> bool:
    return shutil.which(name) is not None


def missing_build_tools(extra: Sequence[str] = ()) -> list[str]:
    """Build tools not on PATH. ``extra`` adds engine-specific ones (e.g. cmake)."""
    needed = ["git", "make", "g++", *extra]
    return [name for name in dict.fromkeys(needed) if not _which_ok(name)]


# header -> Debian/Ubuntu package providing it.
_REQUIRED_HEADERS = {
    "gmp.h": "libgmp-dev",
    "openssl/sha.h": "libssl-dev",
}

# Tools an engine needs on top of the common git/make/g++ set.
ENGINE_TOOLS: dict[str, tuple[str, ...]] = {
    "rckangaroo": ("cmake",),
}

_APT_FOR_TOOL = {
    "git": "git",
    "make": "build-essential",
    "g++": "build-essential",
    "cmake": "cmake",
}
_DNF_FOR_TOOL = {
    "git": "git",
    "make": "make",
    "g++": "gcc-c++",
    "cmake": "cmake",
}
_DNF_FOR_HEADER = {
    "gmp.h": "gmp-devel",
    "openssl/sha.h": "openssl-devel",
}


def _have_header(header: str) -> bool:
    """Ask the compiler, rather than guessing include paths per distro."""
    if not _which_ok("g++"):
        return False
    try:
        proc = subprocess.run(
            ["g++", "-E", "-x", "c++", "-"],
            input=f"#include <{header}>\nint main(){{return 0;}}\n",
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


def missing_build_headers() -> list[str]:
    """Dev headers the solver builds need but `which` cannot see.

    keyhunt links against GMP and OpenSSL; a host with git/make/g++ but no
    -dev packages passes the tool check and then fails deep in `make`.
    """
    return [header for header in _REQUIRED_HEADERS if not _have_header(header)]


def build_deps_hint(tools: list[str], headers: list[str]) -> str:
    packages = ["git", "build-essential"]
    packages += [_REQUIRED_HEADERS[h] for h in headers if h in _REQUIRED_HEADERS]
    missing = ", ".join(tools + headers)
    return (
        f"missing build dependencies: {missing}\n"
        f"  Debian/Ubuntu: sudo apt install -y {' '.join(dict.fromkeys(packages))}\n"
        f"  Fedora/RHEL:   sudo dnf install -y git gcc-c++ make gmp-devel openssl-devel"
    )


@dataclass(frozen=True)
class DepResult:
    ok: bool
    message: str
    installed: tuple[str, ...] = ()
    missing_tools: tuple[str, ...] = ()
    missing_headers: tuple[str, ...] = ()


def _package_manager() -> tuple[str, list[str], list[str]] | None:
    """(name, update argv, install argv prefix), already privilege-wrapped.

    ``sudo -n`` on purpose: an unattended bring-up must fail with a readable
    message rather than block forever on a password prompt nobody will type.
    """
    if os.geteuid() == 0:
        prefix: list[str] = []
    elif _which_ok("sudo"):
        prefix = ["sudo", "-n"]
    else:
        return None
    if _which_ok("apt-get"):
        return (
            "apt-get",
            [*prefix, "apt-get", "update", "-qq"],
            [*prefix, "apt-get", "install", "-y", "-qq"],
        )
    if _which_ok("dnf"):
        return "dnf", [*prefix, "dnf", "makecache", "-q"], [*prefix, "dnf", "install", "-y", "-q"]
    return None


def required_packages(manager: str, tools: Sequence[str], headers: Sequence[str]) -> list[str]:
    if manager == "dnf":
        tool_map, header_map = _DNF_FOR_TOOL, _DNF_FOR_HEADER
    else:
        tool_map, header_map = _APT_FOR_TOOL, _REQUIRED_HEADERS
    packages = [tool_map[t] for t in tools if t in tool_map]
    packages += [header_map[h] for h in headers if h in header_map]
    return list(dict.fromkeys(packages))


def ensure_build_deps(
    engine: str | None = None,
    *,
    auto_install: bool = True,
    timeout: float = 900.0,
) -> DepResult:
    """Make sure this host can compile ``engine`` (or the common set when None).

    Returns rather than raises, so a caller can report the exact apt line when the
    host has no package manager or no privilege to use one.
    """
    extra = ENGINE_TOOLS.get(engine or "", ())
    tools = missing_build_tools(extra)
    headers = missing_build_headers()
    if not tools and not headers:
        return DepResult(ok=True, message="build dependencies present")
    if not auto_install:
        return DepResult(
            ok=False,
            message=build_deps_hint(tools, headers),
            missing_tools=tuple(tools),
            missing_headers=tuple(headers),
        )

    manager = _package_manager()
    if manager is None:
        reason = (
            "no supported package manager on PATH"
            if os.geteuid() == 0
            else "not root and sudo is unavailable"
        )
        return DepResult(
            ok=False,
            message=f"cannot install build dependencies ({reason}).\n{build_deps_hint(tools, headers)}",
            missing_tools=tuple(tools),
            missing_headers=tuple(headers),
        )

    name, update_cmd, install_cmd = manager
    packages = required_packages(name, tools, headers)
    if not packages:
        return DepResult(
            ok=False,
            message=build_deps_hint(tools, headers),
            missing_tools=tuple(tools),
            missing_headers=tuple(headers),
        )
    try:
        subprocess.run(update_cmd, capture_output=True, text=True, check=False, timeout=timeout)
        proc = subprocess.run(
            [*install_cmd, *packages],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DepResult(
            ok=False,
            message=f"package install failed to run: {exc}\n{build_deps_hint(tools, headers)}",
            missing_tools=tuple(tools),
            missing_headers=tuple(headers),
        )

    still_tools = missing_build_tools(extra)
    still_headers = missing_build_headers()
    if still_tools or still_headers:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]
        return DepResult(
            ok=False,
            message=(
                f"{name} install did not satisfy everything "
                f"(still missing: {', '.join(still_tools + still_headers)})"
                + (f"\n{detail}" if detail else "")
            ),
            installed=tuple(packages),
            missing_tools=tuple(still_tools),
            missing_headers=tuple(still_headers),
        )
    return DepResult(
        ok=True,
        message=f"installed via {name}: {', '.join(packages)}",
        installed=tuple(packages),
    )


def cuda_available() -> bool:
    return _which_ok("nvcc") or Path("/usr/local/cuda/bin/nvcc").is_file()


def detect_cuda_home() -> Path | None:
    env = os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend(
        [
            Path("/usr/local/cuda"),
            Path("/usr/lib/cuda"),
        ]
    )
    # Versioned installs: /usr/local/cuda-12.8 etc.
    local = Path("/usr/local")
    if local.is_dir():
        candidates.extend(sorted(local.glob("cuda-*"), reverse=True))
    for path in candidates:
        if (path / "include" / "cuda.h").is_file() or (path / "bin" / "nvcc").is_file():
            return path.resolve()
    if _which_ok("nvcc"):
        nvcc = Path(shutil.which("nvcc") or "").resolve()
        # .../bin/nvcc -> CUDA home
        if nvcc.parent.name == "bin":
            return nvcc.parent.parent
    return None


def detect_compute_cap() -> str | None:
    """Return COMPUTE_CAP like '86' from nvidia-smi, or None."""
    if not _which_ok("nvidia-smi"):
        return None
    code, out = _run(
        [
            "nvidia-smi",
            "--query-gpu=compute_cap",
            "--format=csv,noheader",
        ]
    )
    if code != 0 or not out.strip():
        return None
    # First GPU only; "8.6" -> "86"
    first = out.strip().splitlines()[0].strip()
    if "." in first:
        major, _, minor = first.partition(".")
        if major.isdigit() and minor.isdigit():
            return f"{int(major)}{int(minor)}"
    digits = "".join(ch for ch in first if ch.isdigit())
    return digits or None


def build_gencode(compute_cap: str) -> str:
    """NVCC gencode flags: native SASS + matching PTX for forward compat.

    Borrowed for RTX 5090 (sm_120) and other new arches where a single
    ``code=sm_XX`` line is brittle across toolkit/driver pairs.
    """
    cap = compute_cap.strip()
    if not cap.isdigit():
        raise ValueError(f"compute_cap must be digits like '120', got {compute_cap!r}")
    return (
        f"-gencode arch=compute_{cap},code=sm_{cap} "
        f"-gencode arch=compute_{cap},code=compute_{cap}"
    )


def default_install_names() -> list[str]:
    names = ["keyhunt", "kangaroo"]
    if cuda_available():
        names += ["bitcrack", "rckangaroo"]
    return names


def _clone_or_update(repo: str, dest: Path, commit: str | None = None) -> None:
    """Check out ``commit`` (pinned) or the default branch tip when unpinned."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if commit is None:
        if (dest / ".git").is_dir():
            code, out = _run(["git", "-C", str(dest), "pull", "--ff-only"])
            if code != 0:
                raise RuntimeError(f"git pull failed for {dest}: {out}")
            return
        if dest.exists():
            shutil.rmtree(dest)
        code, out = _run(["git", "clone", "--depth", "1", repo, str(dest)])
        if code != 0:
            raise RuntimeError(f"git clone failed for {repo}: {out}")
        return

    if (dest / ".git").is_dir():
        code, out = _run(["git", "-C", str(dest), "rev-parse", "HEAD"])
        if code == 0 and out.strip().startswith(commit):
            return
    else:
        if dest.exists():
            shutil.rmtree(dest)
        code, out = _run(["git", "init", "-q", str(dest)])
        if code != 0:
            raise RuntimeError(f"git init failed for {dest}: {out}")
        _run(["git", "-C", str(dest), "remote", "add", "origin", repo])

    # Fetching a bare SHA is cheapest; fall back to a full history fetch for
    # servers that refuse reachable-SHA1 requests.
    code, out = _run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", commit])
    if code != 0:
        code, out = _run(["git", "-C", str(dest), "fetch", "origin"])
        if code != 0:
            raise RuntimeError(f"git fetch failed for {repo}@{commit}: {out}")
    code, out = _run(["git", "-C", str(dest), "checkout", "-q", "--detach", commit])
    if code != 0:
        raise RuntimeError(f"git checkout {commit} failed for {repo}: {out}")


def _install_link(src: Path, name: str) -> Path:
    target_dir = bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / name
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    # Copy instead of symlink so relocating vendor/ does not break PATH lookups
    # that only see bin/; keep executable bit.
    shutil.copy2(src, dest)
    dest.chmod(dest.stat().st_mode | 0o111)
    return dest.resolve()


def _write_engines_env(paths: dict[str, Path]) -> Path:
    env_path = engines_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            existing[key.strip()] = value.strip().strip('"').strip("'")
    for name, path in paths.items():
        env_key = ENGINE_ENV_VARS.get(name)
        if env_key:
            existing[env_key] = str(path)
    lines = [
        "# Auto-written by `btc-puzzle-lab engines install`.",
        "# Loaded at CLI startup (does not override already-exported env vars).",
        "# Safe to commit? No — keep local; gitignored via config/engines.env pattern.",
        "",
    ]
    for key in ("KEYHUNT_PATH", "BITCRACK_PATH", "KANGAROO_PATH", "RCKANGAROO_PATH"):
        if key in existing:
            lines.append(f"{key}={existing[key]}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def install_keyhunt(*, force: bool = False) -> InstallResult:
    dest_bin = bin_dir() / "keyhunt"
    if dest_bin.is_file() and os.access(dest_bin, os.X_OK) and not force:
        return InstallResult("keyhunt", True, dest_bin.resolve(), "already installed")

    src_dir = vendor_dir() / "keyhunt"
    _clone_or_update(KEYHUNT_REPO, src_dir, PINNED_COMMITS.get("keyhunt"))
    code, out = _run(["make"], cwd=src_dir)
    built = src_dir / "keyhunt"
    if code != 0 or not built.is_file():
        code, out_legacy = _run(["make", "legacy"], cwd=src_dir)
        out = (out + "\n" + out_legacy).strip()
        if code != 0 or not built.is_file():
            return InstallResult(
                "keyhunt",
                False,
                None,
                f"build failed (tried make / make legacy):\n{out[-2000:]}",
            )
    path = _install_link(built, "keyhunt")
    return InstallResult("keyhunt", True, path, "built and installed to bin/keyhunt")


def _patch_kangaroo_sources(src_dir: Path) -> None:
    """Apply minimal build fixes for modern g++ (upstream Timer.h omits cstdint)."""
    timer_h = src_dir / "Timer.h"
    if not timer_h.is_file():
        return
    text = timer_h.read_text(encoding="utf-8", errors="ignore")
    if "cstdint" in text:
        return
    needle = "#include <string>"
    if needle in text:
        timer_h.write_text(
            text.replace(needle, "#include <string>\n#include <cstdint>", 1),
            encoding="utf-8",
        )


def install_kangaroo(*, force: bool = False) -> InstallResult:
    dest_bin = bin_dir() / "kangaroo"
    if dest_bin.is_file() and os.access(dest_bin, os.X_OK) and not force:
        return InstallResult("kangaroo", True, dest_bin.resolve(), "already installed")

    src_dir = vendor_dir() / "Kangaroo"
    _clone_or_update(KANGAROO_REPO, src_dir, PINNED_COMMITS.get("kangaroo"))
    _patch_kangaroo_sources(src_dir)
    # CPU build (no CUDA). GPU operators can rebuild upstream with gpu=1 themselves.
    code, out = _run(["make", "clean"], cwd=src_dir)
    code, out = _run(["make", "all"], cwd=src_dir)
    # JeanLucPons binary is typically lowercase kangaroo
    candidates = [src_dir / "kangaroo", src_dir / "Kangaroo"]
    built = next((p for p in candidates if p.is_file()), None)
    if code != 0 or built is None:
        return InstallResult(
            "kangaroo",
            False,
            None,
            f"build failed (make all):\n{out[-2000:]}",
        )
    path = _install_link(built, "kangaroo")
    return InstallResult("kangaroo", True, path, "built and installed to bin/kangaroo")


def _patch_bitcrack_makefile(src_dir: Path, *, cuda_home: Path, compute_cap: str | None) -> None:
    makefile = src_dir / "Makefile"
    if not makefile.is_file():
        return
    text = makefile.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("CUDA_HOME"):
            out.append(f"CUDA_HOME={cuda_home}")
        elif compute_cap and line.startswith("COMPUTE_CAP"):
            out.append(f"COMPUTE_CAP={compute_cap}")
        else:
            out.append(line)
    # Append dual gencode (SASS + PTX) for newer arches such as sm_120 / RTX 5090.
    if compute_cap:
        gencode = build_gencode(compute_cap)
        if not any(gencode in line for line in out):
            out.append(f"NVCCFLAGS+={gencode}")
    makefile.write_text("\n".join(out) + "\n", encoding="utf-8")


def install_bitcrack(*, force: bool = False) -> InstallResult:
    dest_bin = bin_dir() / "cuBitCrack"
    if dest_bin.is_file() and os.access(dest_bin, os.X_OK) and not force:
        return InstallResult("bitcrack", True, dest_bin.resolve(), "already installed")

    if not cuda_available():
        return InstallResult(
            "bitcrack",
            False,
            None,
            "nvcc not found — install CUDA toolkit on this host, then retry "
            "`engines install --only bitcrack`",
        )
    cuda_home = detect_cuda_home()
    if cuda_home is None:
        return InstallResult(
            "bitcrack",
            False,
            None,
            "CUDA headers not found (set CUDA_HOME to the toolkit root)",
        )

    src_dir = vendor_dir() / "BitCrack"
    _clone_or_update(BITCRACK_REPO, src_dir, PINNED_COMMITS.get("bitcrack"))
    compute_cap = detect_compute_cap()
    _patch_bitcrack_makefile(src_dir, cuda_home=cuda_home, compute_cap=compute_cap)
    _run(["make", "clean"], cwd=src_dir)
    code, out = _run(["make", "BUILD_CUDA=1"], cwd=src_dir)
    candidates = [
        src_dir / "bin" / "cuBitCrack",
        src_dir / "cuBitCrack",
        src_dir / "bin" / "BitCrack",
    ]
    built = next((p for p in candidates if p.is_file()), None)
    if code != 0 or built is None:
        hint = f" CUDA_HOME={cuda_home}"
        if compute_cap:
            hint += f" COMPUTE_CAP={compute_cap}"
        return InstallResult(
            "bitcrack",
            False,
            None,
            f"build failed (make BUILD_CUDA=1;{hint}):\n{out[-2000:]}",
        )
    path = _install_link(built, "cuBitCrack")
    # Also expose as bin/BitCrack for engines candidate list.
    _install_link(built, "BitCrack")
    msg = f"built and installed to bin/cuBitCrack (CUDA_HOME={cuda_home}"
    if compute_cap:
        msg += f", COMPUTE_CAP={compute_cap}"
    msg += ")"
    return InstallResult("bitcrack", True, path, msg)


def install_rckangaroo(*, force: bool = False) -> InstallResult:
    dest_bin = bin_dir() / "RCKangaroo"
    if dest_bin.is_file() and os.access(dest_bin, os.X_OK) and not force:
        return InstallResult("rckangaroo", True, dest_bin.resolve(), "already installed")

    if not cuda_available():
        return InstallResult(
            "rckangaroo",
            False,
            None,
            "nvcc not found — RCKangaroo is GPU-only; install the CUDA toolkit and retry",
        )
    if not _which_ok("cmake"):
        return InstallResult(
            "rckangaroo",
            False,
            None,
            "cmake not found (Debian/Ubuntu: sudo apt install -y cmake)",
        )
    cuda_home = detect_cuda_home()
    if cuda_home is None:
        return InstallResult(
            "rckangaroo", False, None, "CUDA headers not found (set CUDA_HOME)"
        )

    src_dir = vendor_dir() / "RCKangaroo"
    _clone_or_update(RCKANGAROO_REPO, src_dir, PINNED_COMMITS.get("rckangaroo"))
    code, out = _run(
        [
            "cmake",
            "-B",
            "build",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCUDAToolkit_ROOT={cuda_home}",
        ],
        cwd=src_dir,
    )
    if code != 0:
        return InstallResult(
            "rckangaroo", False, None, f"cmake configure failed:\n{out[-2000:]}"
        )
    code, out = _run(["cmake", "--build", "build", "-j"], cwd=src_dir)
    candidates = [
        src_dir / "build" / "bin" / "rckangaroo",
        src_dir / "build" / "rckangaroo",
        src_dir / "build" / "bin" / "RCKangaroo",
    ]
    built = next((p for p in candidates if p.is_file()), None)
    if code != 0 or built is None:
        return InstallResult(
            "rckangaroo", False, None, f"build failed:\n{out[-2000:]}"
        )
    path = _install_link(built, "RCKangaroo")

    # RCKangaroo loads kernel_sm*.cubin from its working directory; engines.py links
    # them in from beside the binary, so they have to land in bin/ too.
    cubins = sorted(src_dir.glob("*.cubin"))
    for cubin in cubins:
        shutil.copy2(cubin, bin_dir() / cubin.name)
    if not cubins:
        return InstallResult(
            "rckangaroo",
            False,
            path,
            "built, but no kernel_sm*.cubin found upstream — it would spin at 0 MKeys/s",
        )
    names = ", ".join(c.name for c in cubins)
    return InstallResult(
        "rckangaroo", True, path, f"built and installed to bin/RCKangaroo (kernels: {names})"
    )


@dataclass(frozen=True)
class SelfCheckResult:
    name: str
    ok: bool
    puzzle_id: int | None
    message: str
    seconds: float = 0.0


# Solved practice puzzles small enough to finish in seconds, chosen per engine:
# keyhunt/bitcrack search by address, kangaroo needs bits >= 32, and RCKangaroo
# rejects -range below 32 so it needs bits-1 >= 32.
SELFCHECK_PUZZLES = {
    "keyhunt": 20,
    "bitcrack": 20,
    "kangaroo": 32,
    "rckangaroo": 40,
}


def selfcheck_engine(name: str, *, timeout: float = 180.0) -> SelfCheckResult:
    """Make the engine actually solve a puzzle whose answer we already know.

    A build that compiles and runs still tells you nothing: every engine in this
    lab has at some point searched correctly and then failed to hand the key
    back (wrong result filename, unlabelled output, a kernel that never loaded).
    Only an end-to-end solve distinguishes "installed" from "works".
    """
    # Imported here so the build layer does not hard-depend on the search layer.
    from btc_puzzle_lab.catalog import get_puzzle
    from btc_puzzle_lab.engines import resolve_binary, run_external_engine

    puzzle_id = SELFCHECK_PUZZLES.get(name)
    if puzzle_id is None:
        return SelfCheckResult(name, False, None, "no self-check defined for this engine")
    if resolve_binary(name) is None:
        return SelfCheckResult(name, False, puzzle_id, "not installed")
    try:
        puzzle = get_puzzle(puzzle_id)
    except (KeyError, ValueError) as exc:
        return SelfCheckResult(name, False, puzzle_id, f"catalog lookup failed: {exc}")
    if puzzle.practice_solution is None:
        return SelfCheckResult(
            name, False, puzzle_id, f"puzzle #{puzzle_id} has no known solution to check against"
        )

    started = time.monotonic()
    result = run_external_engine(puzzle, name, timeout=timeout, progress=False)
    elapsed = time.monotonic() - started

    if result.secret is None:
        return SelfCheckResult(
            name,
            False,
            puzzle_id,
            f"ran but returned no key for #{puzzle_id} ({result.message})",
            elapsed,
        )
    if result.secret != puzzle.practice_solution:
        return SelfCheckResult(
            name, False, puzzle_id, f"returned the wrong key for #{puzzle_id}", elapsed
        )
    return SelfCheckResult(name, True, puzzle_id, f"solved #{puzzle_id}", elapsed)


def selfcheck_engines(
    names: list[str] | None = None,
    *,
    timeout: float = 180.0,
) -> list[SelfCheckResult]:
    from btc_puzzle_lab.engines import resolve_binary

    selected = names or [n for n in SELFCHECK_PUZZLES if resolve_binary(n) is not None]
    return [selfcheck_engine(name, timeout=timeout) for name in selected]


def format_selfcheck_results(results: list[SelfCheckResult]) -> str:
    if not results:
        return "self-check: no engines installed to verify"
    lines = ["engine self-check (solves a puzzle with a known answer):"]
    for item in results:
        mark = "ok" if item.ok else "!!"
        timing = f" in {item.seconds:.1f}s" if item.seconds else ""
        lines.append(f"  [{mark}] {item.name:<11} {item.message}{timing}")
    return "\n".join(lines)


def install_engines(
    names: list[str] | None = None,
    *,
    force: bool = False,
) -> list[InstallResult]:
    """Install solvers into workspace bin/ and write config/engines.env.

    Default set: keyhunt + kangaroo, and bitcrack when ``nvcc`` is present.
    """
    missing = missing_build_tools()
    missing_headers = missing_build_headers()
    if missing or missing_headers:
        raise RuntimeError(build_deps_hint(missing, missing_headers))

    selected = list(names or default_install_names())
    unknown = [n for n in selected if n not in INSTALLABLE and n not in MANUAL_ONLY]
    if unknown:
        raise ValueError(f"unknown engine(s): {', '.join(unknown)}")

    results: list[InstallResult] = []
    installed_paths: dict[str, Path] = {}

    for name in selected:
        if name in MANUAL_ONLY:
            results.append(InstallResult(name, False, None, MANUAL_ONLY[name]))
            continue
        if name == "keyhunt":
            result = install_keyhunt(force=force)
        elif name == "kangaroo":
            result = install_kangaroo(force=force)
        elif name == "bitcrack":
            result = install_bitcrack(force=force)
        elif name == "rckangaroo":
            result = install_rckangaroo(force=force)
        else:
            result = InstallResult(name, False, None, "not supported")
        results.append(result)
        if result.ok and result.binary is not None:
            installed_paths[name] = result.binary

    if installed_paths:
        env_path = _write_engines_env(installed_paths)
        # Ensure current process sees the new paths without requiring a restart.
        for name, path in installed_paths.items():
            env_key = ENGINE_ENV_VARS.get(name)
            if env_key and not os.environ.get(env_key):
                os.environ[env_key] = str(path)
        results.append(
            InstallResult(
                "config",
                True,
                env_path,
                f"wrote {env_path}",
            )
        )
    return results


@dataclass(frozen=True)
class EnsureResult:
    """Outcome of "make this one engine runnable on this host"."""

    engine: str
    ok: bool
    already_present: bool
    binary: Path | None
    message: str
    deps: DepResult | None = None
    install: InstallResult | None = None
    selfcheck: SelfCheckResult | None = None


def ensure_engine(
    engine: str,
    *,
    force: bool = False,
    install_deps: bool = True,
    selfcheck: bool = True,
    selfcheck_timeout: float = 180.0,
) -> EnsureResult:
    """Get ``engine`` from "named" to "verified working", doing whatever is missing.

    Build deps → clone at the pinned commit → compile → install into ``bin/`` →
    solve a puzzle with a known answer. Each step is skipped when already
    satisfied, so this is cheap to call on every run.
    """
    from btc_puzzle_lab.engines import resolve_binary

    if engine not in INSTALLABLE:
        return EnsureResult(
            engine=engine,
            ok=True,
            already_present=True,
            binary=None,
            message="built-in engine; no external toolchain needed",
        )

    def _verify(binary: Path | None, *, already: bool, install: InstallResult | None,
                deps: DepResult | None, note: str) -> EnsureResult:
        check = None
        if selfcheck and engine in SELFCHECK_PUZZLES:
            check = selfcheck_engine(engine, timeout=selfcheck_timeout)
            if not check.ok:
                return EnsureResult(
                    engine=engine,
                    ok=False,
                    already_present=already,
                    binary=binary,
                    message=f"{note}, but the self-check failed: {check.message}",
                    deps=deps,
                    install=install,
                    selfcheck=check,
                )
            note = f"{note}; self-check {check.message} in {check.seconds:.1f}s"
        return EnsureResult(
            engine=engine,
            ok=True,
            already_present=already,
            binary=binary,
            message=note,
            deps=deps,
            install=install,
            selfcheck=check,
        )

    existing = resolve_binary(engine)
    if existing is not None and not force:
        return _verify(existing, already=True, install=None, deps=None, note="already installed")

    deps = ensure_build_deps(engine, auto_install=install_deps)
    if not deps.ok:
        return EnsureResult(
            engine=engine,
            ok=False,
            already_present=False,
            binary=None,
            message=deps.message,
            deps=deps,
        )

    try:
        results = install_engines([engine], force=force)
    except (RuntimeError, ValueError) as exc:
        return EnsureResult(
            engine=engine,
            ok=False,
            already_present=False,
            binary=None,
            message=str(exc),
            deps=deps,
        )
    install = next((r for r in results if r.name == engine), None)
    if install is None or not install.ok:
        return EnsureResult(
            engine=engine,
            ok=False,
            already_present=False,
            binary=install.binary if install else None,
            message=install.message if install else f"{engine} was not installed",
            deps=deps,
            install=install,
        )
    return _verify(
        install.binary,
        already=False,
        install=install,
        deps=deps,
        note=install.message,
    )


def format_install_results(results: list[InstallResult]) -> str:
    lines = []
    for item in results:
        mark = "ok" if item.ok else "skip/fail"
        path = str(item.binary) if item.binary else "-"
        lines.append(f"[{mark}] {item.name}: {item.message}")
        if item.binary is not None and item.name != "config":
            lines.append(f"         path={path}")
    return "\n".join(lines)
