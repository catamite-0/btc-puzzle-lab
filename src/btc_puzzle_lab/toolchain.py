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
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.paths import workspace_root

# Reproducible default sources. Override REPO and REF together for a fork.
_KEYHUNT_REPO = "https://github.com/albertobsd/keyhunt.git"
_KANGAROO_REPO = "https://github.com/JeanLucPons/Kangaroo.git"
_BITCRACK_REPO = "https://github.com/brichard19/BitCrack.git"

KEYHUNT_REPO = os.environ.get("BTC_PUZZLE_LAB_KEYHUNT_REPO", _KEYHUNT_REPO)
KANGAROO_REPO = os.environ.get("BTC_PUZZLE_LAB_KANGAROO_REPO", _KANGAROO_REPO)
BITCRACK_REPO = os.environ.get("BTC_PUZZLE_LAB_BITCRACK_REPO", _BITCRACK_REPO)

KEYHUNT_REF = os.environ.get(
    "BTC_PUZZLE_LAB_KEYHUNT_REF",
    "2134a2024e524775b13f82aa1fa07b1c8053f867" if KEYHUNT_REPO == _KEYHUNT_REPO else "HEAD",
)
KANGAROO_REF = os.environ.get(
    "BTC_PUZZLE_LAB_KANGAROO_REF",
    "37576c82d198c20fca65b14da74138ae6153a446"
    if KANGAROO_REPO == _KANGAROO_REPO
    else "HEAD",
)
BITCRACK_REF = os.environ.get(
    "BTC_PUZZLE_LAB_BITCRACK_REF",
    "6bf8059ef075eb1622298395866b0bd02375e1d9"
    if BITCRACK_REPO == _BITCRACK_REPO
    else "HEAD",
)

INSTALLABLE = ("keyhunt", "kangaroo", "bitcrack")
MANUAL_ONLY = {
    "rckangaroo": (
        "RCKangaroo is not auto-built here; place RCKangaroo under bin/ "
        "or set RCKANGAROO_PATH"
    ),
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


def missing_build_tools() -> list[str]:
    needed = ["git", "make", "g++"]
    return [name for name in needed if not _which_ok(name)]


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
        names.append("bitcrack")
    return names


def _clone_or_update(repo: str, dest: Path, ref: str) -> str:
    """Fetch and check out exactly ``ref`` so solver builds are reproducible."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").is_dir():
        code, out = _run(["git", "-C", str(dest), "remote", "set-url", "origin", repo])
        if code != 0:
            raise RuntimeError(f"git remote update failed for {dest}: {out}")
    else:
        if dest.exists():
            shutil.rmtree(dest)
        code, out = _run(["git", "clone", "--filter=blob:none", "--no-checkout", repo, str(dest)])
        if code != 0:
            raise RuntimeError(f"git clone failed for {repo}: {out}")

    code, out = _run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref])
    if code != 0:
        raise RuntimeError(f"git fetch failed for {repo}@{ref}: {out}")
    code, out = _run(["git", "-C", str(dest), "checkout", "--force", "--detach", "FETCH_HEAD"])
    if code != 0:
        raise RuntimeError(f"git checkout failed for {repo}@{ref}: {out}")

    code, resolved = _run(["git", "-C", str(dest), "rev-parse", "HEAD"])
    resolved = resolved.strip()
    if code != 0 or not resolved:
        raise RuntimeError(f"git rev-parse failed for {repo}@{ref}: {resolved}")
    if len(ref) == 40 and all(char in "0123456789abcdefABCDEF" for char in ref):
        if resolved.lower() != ref.lower():
            raise RuntimeError(
                f"source verification failed for {repo}: expected {ref}, got {resolved}"
            )
    return resolved


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
    mapping = {
        "keyhunt": "KEYHUNT_PATH",
        "kangaroo": "KANGAROO_PATH",
        "bitcrack": "BITCRACK_PATH",
        "rckangaroo": "RCKANGAROO_PATH",
    }
    for name, path in paths.items():
        env_key = mapping.get(name)
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
    _clone_or_update(KEYHUNT_REPO, src_dir, KEYHUNT_REF)
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
    _clone_or_update(KANGAROO_REPO, src_dir, KANGAROO_REF)
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
    _clone_or_update(BITCRACK_REPO, src_dir, BITCRACK_REF)
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


def install_engines(
    names: list[str] | None = None,
    *,
    force: bool = False,
) -> list[InstallResult]:
    """Install solvers into workspace bin/ and write config/engines.env.

    Default set: keyhunt + kangaroo, and bitcrack when ``nvcc`` is present.
    """
    missing = missing_build_tools()
    if missing:
        raise RuntimeError(
            "missing build tools: "
            + ", ".join(missing)
            + " (Debian/Ubuntu: apt install git build-essential libssl-dev libgmp-dev)"
        )

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
        else:
            result = InstallResult(name, False, None, "not supported")
        results.append(result)
        if result.ok and result.binary is not None:
            installed_paths[name] = result.binary

    if installed_paths:
        env_path = _write_engines_env(installed_paths)
        # Ensure current process sees the new paths without requiring a restart.
        for name, path in installed_paths.items():
            env_key = {
                "keyhunt": "KEYHUNT_PATH",
                "kangaroo": "KANGAROO_PATH",
                "bitcrack": "BITCRACK_PATH",
            }.get(name)
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


def format_install_results(results: list[InstallResult]) -> str:
    lines = []
    for item in results:
        mark = "ok" if item.ok else "skip/fail"
        path = str(item.binary) if item.binary else "-"
        lines.append(f"[{mark}] {item.name}: {item.message}")
        if item.binary is not None and item.name != "config":
            lines.append(f"         path={path}")
    return "\n".join(lines)
