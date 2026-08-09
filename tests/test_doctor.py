from btc_puzzle_lab.cli import main
from btc_puzzle_lab.doctor import doctor_ok, format_doctor, run_doctor
from btc_puzzle_lab.paths import clear_path_cache


def test_doctor_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    checks = run_doctor()
    assert doctor_ok(checks)
    text = format_doctor(checks)
    assert "doctor preflight" in text
    assert "ready" in text


def test_cli_doctor(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    assert main(["doctor"]) == 0
