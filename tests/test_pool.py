import subprocess
import sys
from pathlib import Path

from btc_puzzle_lab import pool

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
QUJDREVGR0g=
-----END PUBLIC KEY-----"""
TOKEN = "token-0123456789abcdef"


def config(*, puzzle=38, worker="runpod5090"):
    return pool.PoolRuntimeConfig(
        puzzle=puzzle,
        gpu_index=0,
        worker=worker,
        token=TOKEN,
        public_key=PUBLIC_KEY,
    )


def test_runtime_config_is_redacted_and_restricted():
    value = config()
    rendered = repr(value)
    assert TOKEN not in rendered
    assert PUBLIC_KEY not in rendered
    assert pool.doctor_ok(pool.validate_runtime_config(value))
    assert not pool.doctor_ok(pool.validate_runtime_config(config(puzzle=72)))
    assert not pool.doctor_ok(pool.validate_runtime_config(config(worker="not-safe")))


def test_ephemeral_config_has_strict_mode_and_command_has_no_secrets(tmp_path):
    value = config()
    path = tmp_path / "pool.conf"
    pool.write_runtime_config(path, value)
    text = path.read_text(encoding="utf-8")

    assert path.stat().st_mode & 0o777 == 0o600
    assert f"user_token={TOKEN}" in text
    assert "untrusted_computer=true" in text
    assert "save_key=true" in text
    assert "custom_range=none" in text

    command = pool.build_pool_command(Path("/opt/btcpuzzle"), value)
    joined = " ".join(command)
    assert TOKEN not in joined
    assert PUBLIC_KEY not in joined
    assert command[-2:] == ["-worker", "runpod5090"]


def test_sm120_build_contains_native_sass_and_forward_compatible_ptx():
    assert pool.build_gencode("120") == (
        "-gencode arch=compute_120,code=sm_120 "
        "-gencode arch=compute_120,code=compute_120"
    )
    try:
        pool.build_gencode("12.0")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_output_redaction_catches_token_key_and_plaintext_private_key():
    value = config()
    private_key = "a" * 64
    text = f"{TOKEN}\n{PUBLIC_KEY}\nPrivate Key: {private_key}\n"
    redacted = pool._redact_output(text, value)
    assert TOKEN not in redacted
    assert PUBLIC_KEY not in redacted
    assert private_key not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


def test_stream_gate_requires_target_before_submission():
    value = config()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "print('[SUCCESS] Range submitted successfully', flush=True);"
                "print('*** TARGET KEY FOUND! ***', flush=True);"
                "print('[SUCCESS] Range submitted successfully', flush=True)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    result = pool._stream_process(
        child,
        value,
        stop_after_target_submission=True,
        timeout=10,
    )
    assert result.submitted_any
    assert result.target_found
    assert result.target_range_submitted


def test_encrypted_winner_is_preserved_but_plaintext_is_refused(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    encrypted = runtime / "WINNER_encrypted.txt"
    encrypted.write_text("Private Key: " + "Q" * 172 + "=\n", encoding="utf-8")
    plaintext = runtime / "WINNER_plain.txt"
    plaintext.write_text("Private Key: " + "a" * 64 + "\n", encoding="utf-8")
    monkeypatch.setattr(pool, "workspace_root", lambda: tmp_path)

    saved = pool._preserve_encrypted_winners(runtime)

    assert len(saved) == 1
    assert saved[0].stat().st_mode & 0o777 == 0o600
    assert "Q" * 64 in saved[0].read_text(encoding="utf-8")
    assert not any("plain" in item.name for item in saved)


def test_upstream_patch_is_exact_and_fail_closed(tmp_path):
    source_dir = tmp_path
    pool_dir = source_dir / "Pool"
    pool_dir.mkdir()
    header = pool_dir / "PoolClient.h"
    header.write_text(
        """    std::string httpPost(
        const std::string& url,
        const std::string& data,
        const std::map<std::string, std::string>& headers = {});
""",
        encoding="utf-8",
    )
    client = pool_dir / "PoolClient.cpp"
    client.write_text(_upstream_fixture(), encoding="utf-8")

    pool._patch_upstream_sources(source_dir)

    patched = client.read_text(encoding="utf-8")
    patched_header = header.read_text(encoding="utf-8")
    assert pool._EXPECTED_TARGETS[38] in patched
    assert pool._EXPECTED_TARGETS[71] in patched
    assert "Unexpected target address from pool API" in patched
    assert "plaintext refused" in patched
    assert "return data;" not in patched
    assert "httpCode < 200 || httpCode >= 300" in patched
    assert "response.find(\"\\\"ok\\\":true\")" in patched
    assert "long* httpCode = nullptr" in patched_header

    try:
        pool._patch_upstream_sources(source_dir)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "expected once" in str(exc)


def _upstream_fixture():
    """Pinned upstream fragments; a mismatch must block the build."""
    return """\
\tif (!publicKey) {
\t\t// No encryption if no public key
\t\treturn data;
\t}

std::string PoolClient::httpPost(const std::string& url,
\tconst std::string& data,
\tconst std::map<std::string, std::string>& headers) {
\tstd::string response;
\tif (!curl) return response;

\tif (res != CURLE_OK) {
\t\tprintf("CURL error: %s\\n", curl_easy_strerror(res));
\t\tlogToFile(config.gpuIndex, std::string("ERROR httpPost(") + url + "): " + curl_easy_strerror(res));
\t\treturn "";
\t}

\treturn response;

\tresult.success = !result.hex.empty() && !result.targetAddress.empty();

\tif (!result.success) {
\t\tresult.error = extractJsonValue(response, "error");
\t\tif (result.error.empty()) {
\t\t\tresult.error = "Invalid API response";
\t\t}
\t\tlogToFile(config.gpuIndex, "ERROR getRange(): " + result.error + " | Response: " + response);
\t}

\tif (httpCode != 200) {
\t\tlogToFile(config.gpuIndex, "ERROR submitRange(hex=" + maskedHex + "): HTTP " + std::to_string(httpCode) + " | Response: " + response);
\t}

\tif (httpCode == 200) {
\t\trangesScanned++;
\t}

\treturn true;

\tstd::string response = httpPost(url, body, headers);

\tif (response.empty()) {
\t\tlogToFile(config.gpuIndex, "ERROR submitKey(encryptedKey=" + encryptedKey + "): Empty response from API");
\t\treturn false;
\t}

\treturn true;

\t\telse {
\t\t\tstd::cerr << "[ERROR] Encryption failed!" << std::endl;
\t\t\tlogToFile(config.gpuIndex, "ERROR notifyTargetFound(address=" + address + "): Encryption failed, sending plaintext");
\t\t\tstd::cout << "Private Key: " << keyToSend << std::endl;
\t\t}

\tstd::string response = httpPost(url.str(), json.str(), headers);

\tif (response.empty()) {
\t\tlogToFile(config.gpuIndex, "ERROR sendTelegram(): Empty response from Telegram API (chat_id=" + config.telegramChatId + ")");
\t}

\treturn !response.empty();
"""
