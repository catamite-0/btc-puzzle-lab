import pytest

from btc_puzzle_lab.cli import main
from btc_puzzle_lab.doctor import doctor_ok, format_doctor, run_doctor
from btc_puzzle_lab.paths import clear_path_cache


@pytest.fixture
def build_deps_present(monkeypatch):
    """Assert on doctor's logic, not on whatever this machine has installed.

    These used to probe the real host, so the suite went red on any box without
    libgmp-dev — a fact about the container, not about the code under test.
    """
    monkeypatch.setattr("btc_puzzle_lab.doctor.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.doctor.missing_build_headers", lambda: [])


def test_doctor_ready(tmp_path, monkeypatch, build_deps_present):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    checks = run_doctor()
    assert doctor_ok(checks)
    text = format_doctor(checks)
    assert "doctor preflight" in text
    assert "ready" in text


def test_cli_doctor(tmp_path, monkeypatch, build_deps_present):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    assert main(["doctor"]) == 0


def test_doctor_blocks_when_build_tools_are_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.doctor.missing_build_tools", lambda: ["g++"])
    monkeypatch.setattr("btc_puzzle_lab.doctor.missing_build_headers", lambda: [])
    checks = run_doctor()
    assert not doctor_ok(checks)
    assert main(["doctor"]) == 1


def test_doctor_reports_missing_dev_headers(monkeypatch):
    # doctor used to say build_tools ok on a host where keyhunt cannot compile.
    monkeypatch.setattr("btc_puzzle_lab.doctor.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.doctor.missing_build_headers", lambda: ["gmp.h"])
    check = next(c for c in run_doctor() if c.name == "build_tools")
    assert not check.ok
    assert "gmp.h" in check.detail
    assert "libgmp-dev" in check.detail


def test_compute_tier_is_not_asked_for_a_gpu_solver(tmp_path, monkeypatch, build_deps_present):
    """tier "compute" means a big CPU box with no card — not a GPU host."""
    from btc_puzzle_lab.strategy import HostProfile

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr(
        "btc_puzzle_lab.doctor.probe_host",
        lambda: HostProfile(cpus=64, mem_mb=116_000, engines=frozenset(), tier="compute"),
    )
    names = {c.name for c in run_doctor()}
    assert "gpu_solver" not in names


def test_doctor_blocks_relay_url_without_pubkey(tmp_path, monkeypatch, build_deps_present):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setenv("RELAY_URL", "https://control.example:8787/hit")
    monkeypatch.setenv("RELAY_TOKEN", "control-hub-token-1")
    checks = run_doctor()
    relay = next(c for c in checks if c.name == "relay_policy")
    assert not relay.ok
    assert "RELAY_SEAL_PUBKEY" in relay.detail
    assert not doctor_ok(checks)


def test_gpu_solver_accepts_rckangaroo(tmp_path, monkeypatch, build_deps_present):
    from btc_puzzle_lab.strategy import HostProfile

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    fake = tmp_path / "bin" / "RCKangaroo"
    fake.parent.mkdir(parents=True)
    fake.write_text("x", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    monkeypatch.setattr(
        "btc_puzzle_lab.doctor.probe_host",
        lambda: HostProfile(
            cpus=8,
            mem_mb=32_768,
            engines=frozenset({"rckangaroo"}),
            gpu=True,
            gpu_name="RTX 5090",
            tier="gpu",
        ),
    )
    check = next(c for c in run_doctor() if c.name == "gpu_solver")
    assert check.ok
    assert "rckangaroo" in check.detail
