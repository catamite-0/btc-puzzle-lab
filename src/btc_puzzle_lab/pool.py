"""Fail-closed adapter for the official btcpuzzle.info public pool client.

This module deliberately supports only the public Puzzle #38 test pool and
Puzzle #71. It does not accept arbitrary addresses or key ranges. The GPL-3.0
pool client remains an external executable built from a pinned upstream commit.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from btc_puzzle_lab.paths import workspace_root
from btc_puzzle_lab.toolchain import (
    bin_dir,
    cuda_available,
    detect_compute_cap,
    detect_cuda_home,
    missing_build_tools,
    vendor_dir,
)

OFFICIAL_POOL_REPO = "https://github.com/ilkerccom/btcpuzzle.git"
OFFICIAL_POOL_COMMIT = "025e2656fc5ff6f3e8ea51477b8374c8000ee366"
ALLOWED_POOL_PUZZLES = (38, 71)

POOL_BINARY_ENV = "BTCPUZZLE_POOL_PATH"
POOL_TOKEN_ENV = "BTCPUZZLE_USER_TOKEN"
POOL_PUBLIC_KEY_ENV = "BTCPUZZLE_RSA_PUBLIC_KEY"
POOL_PUBLIC_KEY_FILE_ENV = "BTCPUZZLE_RSA_PUBLIC_KEY_FILE"
POOL_WORKER_ENV = "BTCPUZZLE_WORKER"
POOL_GPU_ENV = "BTCPUZZLE_GPU_INDEX"
POOL_COMPUTE_CAP_ENV = "BTCPUZZLE_COMPUTE_CAP"
POOL_CXX_ENV = "BTCPUZZLE_CXX"

_SUBMITTED_MARKER = "Range submitted successfully"
_TARGET_FOUND_MARKER = "*** TARGET KEY FOUND! ***"
_WORKER_RE = re.compile(r"^[A-Za-z0-9]{1,15}$")
_PRIVATE_KEY_LINE_RE = re.compile(r"(?i)(private key\s*:\s*)[0-9a-f]{64}")

_EXPECTED_TARGETS = {
    38: "1HBtApAFA9B2YZw3G2YKSMCtb3dVnjuNe2",
    71: "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU",
}
SAFETY_PATCHES = (
    "fixed-public-puzzle-targets",
    "submit-range-http-status",
    "encrypt-without-key-fail-closed",
    "rsa-fail-closed",
    "save-key-http-status",
    "telegram-response-check",
)


@dataclass(frozen=True)
class PoolRuntimeConfig:
    puzzle: int
    gpu_index: int
    worker: str
    token: str = field(repr=False)
    public_key: str = field(repr=False)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass(frozen=True)
class PoolInstallResult:
    ok: bool
    binary: Path | None
    message: str


@dataclass(frozen=True)
class PoolProcessResult:
    code: int
    submitted_any: bool
    target_found: bool
    target_range_submitted: bool


def pool_binary_path() -> Path | None:
    override = os.environ.get(POOL_BINARY_ENV, "").strip()
    candidates = [Path(override).expanduser()] if override else []
    candidates.append(bin_dir() / "btcpuzzle")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def provenance_path() -> Path:
    return bin_dir() / "btcpuzzle.provenance.json"


def verification_path() -> Path:
    return workspace_root() / "state" / "pool" / "verified.json"


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"command timed out after {timeout}s"
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode, output


def _read_public_key() -> str:
    inline = os.environ.get(POOL_PUBLIC_KEY_ENV, "").strip()
    file_value = os.environ.get(POOL_PUBLIC_KEY_FILE_ENV, "").strip()
    if inline and file_value:
        raise ValueError(
            f"set only one of {POOL_PUBLIC_KEY_ENV} or {POOL_PUBLIC_KEY_FILE_ENV}"
        )
    if file_value:
        path = Path(file_value).expanduser()
        try:
            inline = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read RSA public key file: {exc}") from exc
    return inline.replace("|", "\n").replace("@", "\n").strip()


def runtime_config_from_env(
    *,
    puzzle: int,
    gpu_index: int | None = None,
    worker: str | None = None,
) -> PoolRuntimeConfig:
    raw_gpu = str(gpu_index) if gpu_index is not None else os.environ.get(POOL_GPU_ENV, "0")
    try:
        selected_gpu = int(raw_gpu)
    except ValueError as exc:
        raise ValueError(f"{POOL_GPU_ENV} must be a non-negative integer") from exc
    return PoolRuntimeConfig(
        puzzle=puzzle,
        gpu_index=selected_gpu,
        worker=(worker if worker is not None else os.environ.get(POOL_WORKER_ENV, "")).strip(),
        token=os.environ.get(POOL_TOKEN_ENV, "").strip(),
        public_key=_read_public_key(),
    )


def validate_runtime_config(config: PoolRuntimeConfig) -> list[Check]:
    checks = [
        Check(
            "puzzle",
            config.puzzle in ALLOWED_POOL_PUZZLES,
            f"public pool #{config.puzzle}"
            if config.puzzle in ALLOWED_POOL_PUZZLES
            else "only public pools #38 and #71 are allowed",
        ),
        Check(
            "gpu_index",
            config.gpu_index >= 0,
            str(config.gpu_index) if config.gpu_index >= 0 else "must be non-negative",
        ),
        Check(
            "worker",
            not config.worker or bool(_WORKER_RE.fullmatch(config.worker)),
            config.worker or "auto-generated by official client",
        ),
    ]
    token_ok = 16 <= len(config.token) <= 2048 and not any(
        char.isspace() or ord(char) < 32 for char in config.token
    )
    checks.append(
        Check(
            "pool_token",
            token_ok,
            "set (redacted)" if token_ok else f"set {POOL_TOKEN_ENV} as a RunPod secret",
        )
    )
    key_ok = (
        "-----BEGIN PUBLIC KEY-----" in config.public_key
        and "-----END PUBLIC KEY-----" in config.public_key
        and "\x00" not in config.public_key
    )
    checks.append(
        Check(
            "rsa_public_key",
            key_ok,
            "set (redacted; private key stays off Pod)"
            if key_ok
            else f"set {POOL_PUBLIC_KEY_ENV} or {POOL_PUBLIC_KEY_FILE_ENV}",
        )
    )
    return checks


def render_pool_config(config: PoolRuntimeConfig) -> str:
    failures = [check.detail for check in validate_runtime_config(config) if not check.ok]
    if failures:
        raise ValueError("invalid pool configuration: " + "; ".join(failures))
    public_key = "|".join(line.strip() for line in config.public_key.splitlines() if line.strip())
    return "\n".join(
        [
            "# Ephemeral config generated by btc-puzzle-pool.",
            f"user_token={config.token}",
            f"worker_name={config.worker}",
            f"target_puzzle={config.puzzle}",
            f"gpu_index={config.gpu_index}",
            "untrusted_computer=true",
            f"public_key={public_key}",
            "telegram_share=false",
            "telegram_token=",
            "telegram_chat_id=",
            "api_share=false",
            "api_share_url=",
            "save_key=true",
            "custom_range=none",
            "",
        ]
    )


def write_runtime_config(path: Path, config: PoolRuntimeConfig) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(render_pool_config(config))
    path.chmod(0o600)


def build_pool_command(binary: Path, config: PoolRuntimeConfig) -> list[str]:
    command = [
        str(binary),
        "-gpu",
        "-gpuId",
        str(config.gpu_index),
        "-puzzle",
        str(config.puzzle),
    ]
    if config.worker:
        command.extend(["-worker", config.worker])
    return command


def _nvcc_release(cuda_home: Path) -> tuple[int, int] | None:
    code, output = _run([str(cuda_home / "bin" / "nvcc"), "--version"])
    if code != 0:
        return None
    match = re.search(r"release\s+(\d+)\.(\d+)", output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _selected_compute_cap() -> str | None:
    override = os.environ.get(POOL_COMPUTE_CAP_ENV, "").strip().replace(".", "")
    if override:
        if not override.isdigit() or not 50 <= int(override) <= 999:
            raise ValueError(f"{POOL_COMPUTE_CAP_ENV} must look like 120 or 12.0")
        return str(int(override))
    return detect_compute_cap()


def build_gencode(compute_cap: str) -> str:
    if not compute_cap.isdigit():
        raise ValueError("compute capability must contain digits only")
    return (
        f"-gencode arch=compute_{compute_cap},code=sm_{compute_cap} "
        f"-gencode arch=compute_{compute_cap},code=compute_{compute_cap}"
    )


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"upstream safety patch {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


def _patch_upstream_sources(source_dir: Path) -> None:
    pool_client = source_dir / "Pool" / "PoolClient.cpp"
    pool_header = source_dir / "Pool" / "PoolClient.h"
    text = pool_client.read_text(encoding="utf-8-sig")
    header = pool_header.read_text(encoding="utf-8-sig")
    old_submit = """\tif (httpCode != 200) {
\t\tlogToFile(config.gpuIndex, \"ERROR submitRange(hex=\" + maskedHex + \"): HTTP \" + std::to_string(httpCode) + \" | Response: \" + response);
\t}

\tif (httpCode == 200) {
\t\trangesScanned++;
\t}

\treturn true;
"""
    old_target_validation = """\tresult.success = !result.hex.empty() && !result.targetAddress.empty();

\tif (!result.success) {
\t\tresult.error = extractJsonValue(response, \"error\");
\t\tif (result.error.empty()) {
\t\t\tresult.error = \"Invalid API response\";
\t\t}
\t\tlogToFile(config.gpuIndex, \"ERROR getRange(): \" + result.error + \" | Response: \" + response);
\t}
"""
    new_target_validation = f"""\tconst std::string expectedTarget =
\t\tconfig.targetPuzzle == 38 ? \"{_EXPECTED_TARGETS[38]}\" :
\t\tconfig.targetPuzzle == 71 ? \"{_EXPECTED_TARGETS[71]}\" : \"\";
\tresult.success = !result.hex.empty() && !result.targetAddress.empty() &&
\t\t!expectedTarget.empty() && result.targetAddress == expectedTarget;

\tif (!result.success) {{
\t\tif (!expectedTarget.empty() && !result.targetAddress.empty() &&
\t\t\tresult.targetAddress != expectedTarget) {{
\t\t\tresult.error = \"Unexpected target address from pool API\";
\t\t}}
\t\tif (result.error.empty()) {{
\t\t\tresult.error = extractJsonValue(response, \"error\");
\t\t}}
\t\tif (result.error.empty()) {{
\t\t\tresult.error = \"Invalid API response\";
\t\t}}
\t\tlogToFile(config.gpuIndex, \"ERROR getRange(): \" + result.error + \" | Response: \" + response);
\t}}
"""
    new_submit = """\tif (httpCode != 200) {
\t\tlogToFile(config.gpuIndex, \"ERROR submitRange(hex=\" + maskedHex + \"): HTTP \" + std::to_string(httpCode) + \" | Response: \" + response);
\t\treturn false;
\t}

\trangesScanned++;
\treturn true;
"""
    old_encrypt = """\t\telse {
\t\t\tstd::cerr << \"[ERROR] Encryption failed!\" << std::endl;
\t\t\tlogToFile(config.gpuIndex, \"ERROR notifyTargetFound(address=\" + address + \"): Encryption failed, sending plaintext\");
\t\t\tstd::cout << \"Private Key: \" << keyToSend << std::endl;
\t\t}
"""
    new_encrypt = """\t\telse {
\t\t\tstd::cerr << \"[ERROR] Encryption failed; refusing plaintext output\" << std::endl;
\t\t\tlogToFile(config.gpuIndex, \"ERROR notifyTargetFound(address=\" + address + \"): Encryption failed; plaintext refused\");
\t\t\treturn false;
\t\t}
"""
    old_encrypt_without_key = """\tif (!publicKey) {
\t\t// No encryption if no public key
\t\treturn data;
\t}
"""
    new_encrypt_without_key = """\tif (!publicKey) {
\t\tlogToFile(config.gpuIndex, "ERROR encryptData(): public key unavailable; plaintext refused");
\t\treturn "";
\t}
"""
    old_http_decl = """    std::string httpPost(
        const std::string& url,
        const std::string& data,
        const std::map<std::string, std::string>& headers = {});
"""
    new_http_decl = """    std::string httpPost(
        const std::string& url,
        const std::string& data,
        const std::map<std::string, std::string>& headers = {},
        long* httpCode = nullptr);
"""
    old_http_impl = """std::string PoolClient::httpPost(const std::string& url,
\tconst std::string& data,
\tconst std::map<std::string, std::string>& headers) {
\tstd::string response;
\tif (!curl) return response;
"""
    new_http_impl = """std::string PoolClient::httpPost(const std::string& url,
\tconst std::string& data,
\tconst std::map<std::string, std::string>& headers,
\tlong* httpCode) {
\tstd::string response;
\tif (!curl) {
\t\tif (httpCode) *httpCode = 0;
\t\treturn response;
\t}
"""
    old_http_error = """\tif (res != CURLE_OK) {
\t\tprintf(\"CURL error: %s\\n\", curl_easy_strerror(res));
\t\tlogToFile(config.gpuIndex, std::string(\"ERROR httpPost(\") + url + \"): \" + curl_easy_strerror(res));
\t\treturn \"\";
\t}

\treturn response;
"""
    new_http_error = """\tif (res != CURLE_OK) {
\t\tif (httpCode) *httpCode = 0;
\t\tprintf(\"CURL error: %s\\n\", curl_easy_strerror(res));
\t\tlogToFile(config.gpuIndex, std::string(\"ERROR httpPost(\") + url + \"): \" + curl_easy_strerror(res));
\t\treturn \"\";
\t}

\tif (httpCode) curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, httpCode);
\treturn response;
"""
    old_submit_key = """\tstd::string response = httpPost(url, body, headers);

\tif (response.empty()) {
\t\tlogToFile(config.gpuIndex, \"ERROR submitKey(encryptedKey=\" + encryptedKey + \"): Empty response from API\");
\t\treturn false;
\t}

\treturn true;
"""
    new_submit_key = """\tlong httpCode = 0;
\tstd::string response = httpPost(url, body, headers, &httpCode);

\tif (response.empty() || httpCode < 200 || httpCode >= 300) {
\t\tlogToFile(config.gpuIndex, \"ERROR submitKey(): request failed with HTTP \" + std::to_string(httpCode));
\t\treturn false;
\t}

\treturn true;
"""
    old_telegram = """\tstd::string response = httpPost(url.str(), json.str(), headers);

\tif (response.empty()) {
\t\tlogToFile(config.gpuIndex, \"ERROR sendTelegram(): Empty response from Telegram API (chat_id=\" + config.telegramChatId + \")\");
\t}

\treturn !response.empty();
"""
    new_telegram = """\tlong httpCode = 0;
\tstd::string response = httpPost(url.str(), json.str(), headers, &httpCode);
\tbool ok = httpCode >= 200 && httpCode < 300 &&
\t\tresponse.find(\"\\\"ok\\\":true\") != std::string::npos;

\tif (!ok) {
\t\tlogToFile(config.gpuIndex, \"ERROR sendTelegram(): request was not accepted (chat_id=\" + config.telegramChatId + \")\");
\t}

\treturn ok;
"""
    text = _replace_once(
        text,
        old_target_validation,
        new_target_validation,
        label="fixed-public-puzzle-targets",
    )
    text = _replace_once(text, old_submit, new_submit, label="submit-range-http-status")
    text = _replace_once(
        text,
        old_encrypt_without_key,
        new_encrypt_without_key,
        label="encrypt-without-key-fail-closed",
    )
    text = _replace_once(text, old_encrypt, new_encrypt, label="rsa-fail-closed")
    text = _replace_once(text, old_http_impl, new_http_impl, label="http-post-status-out")
    text = _replace_once(text, old_http_error, new_http_error, label="http-post-status-read")
    text = _replace_once(text, old_submit_key, new_submit_key, label="save-key-http-status")
    text = _replace_once(text, old_telegram, new_telegram, label="telegram-response-check")
    header = _replace_once(
        header,
        old_http_decl,
        new_http_decl,
        label="http-post-status-declaration",
    )
    pool_client.write_text(text, encoding="utf-8")
    pool_header.write_text(header, encoding="utf-8")


def _safe_remove_vendor(path: Path) -> None:
    expected_parent = vendor_dir().resolve()
    resolved = path.resolve()
    if resolved.parent != expected_parent or resolved.name != "btcpuzzle-official":
        raise RuntimeError(f"refusing to remove unexpected path: {resolved}")
    shutil.rmtree(resolved)


def _checkout_pinned_source(source_dir: Path, *, force: bool) -> None:
    if source_dir.exists() and force:
        _safe_remove_vendor(source_dir)
    if source_dir.exists():
        if not (source_dir / ".git").is_dir():
            raise RuntimeError(f"{source_dir} exists but is not a git checkout; use --force")
        code, head = _run(["git", "-C", str(source_dir), "rev-parse", "HEAD"])
        status_code, status = _run(
            ["git", "-C", str(source_dir), "status", "--porcelain"]
        )
        if code or status_code or head.strip() != OFFICIAL_POOL_COMMIT or status.strip():
            raise RuntimeError(
                "existing official-client checkout is not the clean pinned commit; use --force"
            )
        return

    source_dir.parent.mkdir(parents=True, exist_ok=True)
    code, output = _run(
        ["git", "clone", "--no-checkout", OFFICIAL_POOL_REPO, str(source_dir)]
    )
    if code != 0:
        raise RuntimeError(f"official client clone failed: {output[-2000:]}")
    code, output = _run(
        [
            "git",
            "-C",
            str(source_dir),
            "checkout",
            "--detach",
            OFFICIAL_POOL_COMMIT,
        ]
    )
    if code != 0:
        raise RuntimeError(f"official client checkout failed: {output[-2000:]}")
    _, head = _run(["git", "-C", str(source_dir), "rev-parse", "HEAD"])
    if head.strip() != OFFICIAL_POOL_COMMIT:
        raise RuntimeError("official client commit verification failed")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_provenance() -> dict[str, object] | None:
    path = provenance_path()
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def install_pool_client(*, force: bool = False) -> PoolInstallResult:
    missing = missing_build_tools()
    if missing:
        return PoolInstallResult(False, None, "missing build tools: " + ", ".join(missing))
    if not cuda_available():
        return PoolInstallResult(False, None, "nvcc missing; use a CUDA devel image")
    cuda_home = detect_cuda_home()
    if cuda_home is None:
        return PoolInstallResult(False, None, "CUDA toolkit root not found")
    try:
        compute_cap = _selected_compute_cap()
    except ValueError as exc:
        return PoolInstallResult(False, None, str(exc))
    if compute_cap is None:
        return PoolInstallResult(False, None, "GPU compute capability not detected")
    release = _nvcc_release(cuda_home)
    if compute_cap == "120" and (release is None or release < (12, 8)):
        return PoolInstallResult(False, None, "RTX 5090 requires nvcc 12.8 or newer")

    destination = bin_dir() / "btcpuzzle"
    provenance = _load_provenance()
    if destination.is_file() and os.access(destination, os.X_OK) and not force:
        expected = (
            provenance
            and provenance.get("upstream_commit") == OFFICIAL_POOL_COMMIT
            and provenance.get("compute_cap") == compute_cap
            and provenance.get("binary_sha256") == _file_sha256(destination)
        )
        if expected:
            return PoolInstallResult(True, destination.resolve(), "pinned client already installed")
        return PoolInstallResult(
            False,
            None,
            "existing pool binary lacks matching provenance; retry with --force",
        )

    source_dir = vendor_dir() / "btcpuzzle-official"
    try:
        _checkout_pinned_source(source_dir, force=force)
        _patch_upstream_sources(source_dir)
    except (OSError, RuntimeError) as exc:
        return PoolInstallResult(False, None, str(exc))

    requested_cxx = os.environ.get(POOL_CXX_ENV, "").strip()
    requested_path = shutil.which(requested_cxx) if requested_cxx else None
    cxx = requested_path or shutil.which("g++-9") or shutil.which("g++")
    if not cxx or not Path(cxx).is_file():
        return PoolInstallResult(False, None, f"C++ compiler not found (set {POOL_CXX_ENV})")
    jobs = max(1, min(os.cpu_count() or 1, 16))
    _run(["make", "clean"], cwd=source_dir)
    command = [
        "make",
        f"-j{jobs}",
        "gpu=1",
        "all",
        f"CXX={cxx}",
        f"CXXCUDA={cxx}",
        f"CUDA={cuda_home}",
        f"GENCODE={build_gencode(compute_cap)}",
        f"BUILD_HASH={secrets.token_hex(32)}",
    ]
    code, output = _run(command, cwd=source_dir)
    built = source_dir / "vanitysearch"
    if code != 0 or not built.is_file():
        return PoolInstallResult(
            False,
            None,
            "official client build failed:\n" + output[-4000:],
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    record = {
        "schema": 1,
        "upstream_repo": OFFICIAL_POOL_REPO,
        "upstream_commit": OFFICIAL_POOL_COMMIT,
        "compute_cap": compute_cap,
        "cuda_release": list(release) if release else None,
        "compiler": str(cxx),
        "gencode": build_gencode(compute_cap),
        "safety_patches": list(SAFETY_PATCHES),
        "binary_sha256": _file_sha256(destination),
        "built_at": datetime.now(UTC).isoformat(),
    }
    provenance_path().write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return PoolInstallResult(
        True,
        destination.resolve(),
        f"built pinned official client for sm_{compute_cap}",
    )


def _verification_matches(binary: Path, compute_cap: str | None) -> bool:
    path = verification_path()
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(record, dict)
        and record.get("upstream_commit") == OFFICIAL_POOL_COMMIT
        and record.get("compute_cap") == compute_cap
        and record.get("binary_sha256") == _file_sha256(binary)
        and record.get("test_puzzle") == 38
    )


def run_pool_doctor(config: PoolRuntimeConfig) -> list[Check]:
    checks = validate_runtime_config(config)
    binary = pool_binary_path()
    checks.append(
        Check(
            "pool_binary",
            binary is not None,
            str(binary) if binary else "run: btc-puzzle-pool install",
        )
    )
    smi = shutil.which("nvidia-smi")
    code, gpu_output = _run([smi, "-L"]) if smi else (1, "")
    checks.append(
        Check(
            "nvidia_driver",
            code == 0 and bool(gpu_output.strip()),
            gpu_output.splitlines()[0] if code == 0 and gpu_output.strip() else "GPU not visible",
        )
    )
    binary_probe_code, binary_probe_output = (
        _run([str(binary), "-l"], timeout=30) if binary is not None else (1, "")
    )
    binary_probe_detail = next(
        (
            line.strip()
            for line in binary_probe_output.splitlines()
            if line.strip() and "warning" not in line.lower()
        ),
        "client could not enumerate CUDA devices",
    )
    checks.append(
        Check(
            "pool_binary_gpu",
            binary_probe_code == 0,
            binary_probe_detail,
        )
    )
    try:
        compute_cap = _selected_compute_cap()
    except ValueError:
        compute_cap = None
    checks.append(
        Check(
            "compute_cap",
            compute_cap is not None,
            f"sm_{compute_cap}" if compute_cap else "not detected",
        )
    )
    provenance = _load_provenance()
    provenance_ok = bool(
        binary
        and provenance
        and provenance.get("upstream_commit") == OFFICIAL_POOL_COMMIT
        and provenance.get("compute_cap") == compute_cap
        and provenance.get("binary_sha256") == _file_sha256(binary)
        and provenance.get("safety_patches") == list(SAFETY_PATCHES)
    )
    checks.append(
        Check(
            "provenance",
            provenance_ok,
            f"pinned {OFFICIAL_POOL_COMMIT[:12]} + safety patches"
            if provenance_ok
            else "missing/mismatched pinned build provenance",
        )
    )
    if config.puzzle == 71:
        verified = bool(binary and _verification_matches(binary, compute_cap))
        checks.append(
            Check(
                "puzzle38_gate",
                verified,
                "same binary/GPU passed an end-to-end #38 submission"
                if verified
                else "run: btc-puzzle-pool test",
            )
        )
    return checks


def doctor_ok(checks: list[Check]) -> bool:
    return all(check.ok for check in checks if check.blocking)


def format_pool_doctor(checks: list[Check]) -> str:
    lines = ["public-pool preflight:", ""]
    for check in checks:
        mark = "ok" if check.ok else ("!!" if check.blocking else "--")
        lines.append(f"  [{mark}] {check.name:<16} {check.detail}")
    lines.extend(["", "result: ready" if doctor_ok(checks) else "result: blocked"])
    return "\n".join(lines)


def _redact_output(text: str, config: PoolRuntimeConfig) -> str:
    redacted = text.replace(config.token, "[REDACTED_TOKEN]") if config.token else text
    if config.public_key:
        redacted = redacted.replace(config.public_key, "[REDACTED_PUBLIC_KEY]")
        redacted = redacted.replace(
            "|".join(config.public_key.splitlines()),
            "[REDACTED_PUBLIC_KEY]",
        )
    return _PRIVATE_KEY_LINE_RE.sub(r"\1[REDACTED_PRIVATE_KEY]", redacted)


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=15)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _stream_process(
    proc: subprocess.Popen[bytes],
    config: PoolRuntimeConfig,
    *,
    stop_after_target_submission: bool,
    timeout: int | None,
    on_tick: Callable[[], None] | None = None,
) -> PoolProcessResult:
    if proc.stdout is None:
        raise RuntimeError("pool client stdout pipe missing")
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    line_buffer = ""
    submitted_any = False
    target_found = False
    target_range_submitted = False
    deadline = time.monotonic() + timeout if timeout else None

    def observe(line: str) -> bool:
        nonlocal submitted_any, target_found, target_range_submitted
        if _TARGET_FOUND_MARKER in line:
            target_found = True
        if _SUBMITTED_MARKER in line:
            submitted_any = True
            if target_found:
                target_range_submitted = True
                return stop_after_target_submission
        return False

    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print("pool test timed out", file=sys.stderr)
                _stop_process(proc)
                return PoolProcessResult(
                    124,
                    submitted_any,
                    target_found,
                    target_range_submitted,
                )
            events = selector.select(timeout=1)
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                decoded = decoder.decode(chunk)
                sys.stdout.write(_redact_output(decoded, config))
                sys.stdout.flush()
                line_buffer += decoded
                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    if observe(line):
                        _stop_process(proc)
            if on_tick is not None:
                on_tick()
            if proc.poll() is not None and not selector.get_map():
                break
    except KeyboardInterrupt:
        _stop_process(proc)
        return PoolProcessResult(
            130,
            submitted_any,
            target_found,
            target_range_submitted,
        )
    finally:
        selector.close()
    remainder = decoder.decode(b"", final=True)
    if remainder:
        sys.stdout.write(_redact_output(remainder, config))
        line_buffer += remainder
    if line_buffer:
        observe(line_buffer)
    return PoolProcessResult(
        proc.returncode or 0,
        submitted_any,
        target_found,
        target_range_submitted,
    )


def _preserve_encrypted_winners(
    runtime_dir: Path,
    *,
    seen: set[str] | None = None,
) -> list[Path]:
    destination = workspace_root() / "state" / "pool" / "results"
    saved: list[Path] = []
    observed = seen if seen is not None else set()
    for source in runtime_dir.glob("WINNER_*.txt"):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        source_id = f"{source.name}:{hashlib.sha256(text.encode()).hexdigest()}"
        if source_id in observed:
            continue
        key_match = re.search(r"(?i)private key\s*:\s*(\S+)", text)
        if (
            _PRIVATE_KEY_LINE_RE.search(text)
            or key_match is None
            or len(key_match.group(1)) < 32
        ):
            print(
                f"refusing to persist unsafe winner artifact {source.name}",
                file=sys.stderr,
            )
            continue
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = destination / f"{source.stem}_{source_id[-12:]}.txt"
        if not target.exists():
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
        observed.add(source_id)
        saved.append(target)
    return saved


def _write_verification(binary: Path, compute_cap: str | None) -> None:
    path = verification_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "schema": 1,
        "test_puzzle": 38,
        "upstream_commit": OFFICIAL_POOL_COMMIT,
        "compute_cap": compute_cap,
        "binary_sha256": _file_sha256(binary),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")


def run_pool_client(
    config: PoolRuntimeConfig,
    *,
    stop_after_target_submission: bool = False,
    timeout: int | None = None,
) -> tuple[int, bool]:
    checks = run_pool_doctor(config)
    print(format_pool_doctor(checks))
    if not doctor_ok(checks):
        return 2, False
    binary = pool_binary_path()
    if binary is None:
        return 2, False

    runtime_parent = workspace_root() / "state" / "pool" / "runtime"
    runtime_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="session-", dir=runtime_parent) as raw_dir:
        runtime_dir = Path(raw_dir)
        runtime_dir.chmod(0o700)
        write_runtime_config(runtime_dir / "pool.conf", config)
        observed_winners: set[str] = set()
        saved: list[Path] = []

        def preserve_winners() -> None:
            for item in _preserve_encrypted_winners(
                runtime_dir,
                seen=observed_winners,
            ):
                if item not in saved:
                    saved.append(item)
                    print(f"saved encrypted winner artifact: {item}")

        child_env = os.environ.copy()
        for key in (POOL_TOKEN_ENV, POOL_PUBLIC_KEY_ENV, POOL_PUBLIC_KEY_FILE_ENV):
            child_env.pop(key, None)
        proc = subprocess.Popen(
            build_pool_command(binary, config),
            cwd=runtime_dir,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        result = _stream_process(
            proc,
            config,
            stop_after_target_submission=stop_after_target_submission,
            timeout=timeout,
            on_tick=preserve_winners,
        )
        preserve_winners()
    verified = bool(
        config.puzzle == 38
        and result.target_found
        and result.target_range_submitted
        and saved
    )
    if verified:
        _write_verification(binary, _selected_compute_cap())
        print("#38 hardware/client gate passed; decrypt the saved artifact off Pod before #71")
    if config.puzzle == 38:
        return result.code, verified
    return result.code, result.submitted_any


def _add_runtime_args(parser: argparse.ArgumentParser, *, default_puzzle: int) -> None:
    parser.add_argument(
        "--puzzle",
        type=int,
        choices=ALLOWED_POOL_PUZZLES,
        default=default_puzzle,
        help=f"public pool puzzle (default: {default_puzzle})",
    )
    parser.add_argument("--gpu-index", type=int, default=None)
    parser.add_argument("--worker", default=None, help="1-15 safe worker-name characters")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc-puzzle-pool",
        description="Pinned adapter for the public btcpuzzle.info #38/#71 pools",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="build pinned official client for this GPU")
    install.add_argument("--force", action="store_true")

    doctor = sub.add_parser("doctor", help="redacted preflight without leasing a range")
    _add_runtime_args(doctor, default_puzzle=38)

    test = sub.add_parser("test", help="complete one #38 range and record verification")
    test.add_argument("--gpu-index", type=int, default=None)
    test.add_argument("--worker", default=None)
    test.add_argument("--timeout", type=int, default=900)

    run = sub.add_parser("run", help="run the public #71 pool after the #38 gate")
    _add_runtime_args(run, default_puzzle=71)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install":
        result = install_pool_client(force=args.force)
        print(result.message)
        if result.binary:
            print(f"binary={result.binary}")
        return 0 if result.ok else 1
    try:
        puzzle = 38 if args.command == "test" else args.puzzle
        config = runtime_config_from_env(
            puzzle=puzzle,
            gpu_index=args.gpu_index,
            worker=args.worker,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        checks = run_pool_doctor(config)
        print(format_pool_doctor(checks))
        return 0 if doctor_ok(checks) else 1
    if args.command == "test":
        code, submitted = run_pool_client(
            config,
            stop_after_target_submission=True,
            timeout=args.timeout,
        )
        return 0 if submitted else code or 1
    code, _ = run_pool_client(config)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
