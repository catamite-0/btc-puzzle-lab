import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "machine-bootstrap.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_machine_bootstrap_shell_syntax():
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_machine_bootstrap_is_fail_closed():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "engines install || true" not in text
    assert "import-catalog || true" not in text
    assert "btc-puzzle-lab engines install --only bitcrack --force" in text
    assert "btc-puzzle-lab engines install --force" not in text
    assert "btc-puzzle-lab import-catalog" not in text
    assert "btc-puzzle-lab benchmark-gpu --seconds 90" in text
    assert "btc-puzzle-lab verify 20" in text
    assert "once --ids 20" not in text
    assert "--ids 71" not in text
    assert "--status unsolved" not in text
    assert "sys.version_info < (3, 12)" in text
    assert '[[ "$(id -u)" -eq 0 ]]' in text


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX executable scripts")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_machine_bootstrap_rejects_old_python_before_apt(tmp_path):
    requested = tmp_path / "requested-python"
    requested.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    requested.chmod(0o755)
    for name in ("python3.12", "python3"):
        fake = tmp_path / name
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)

    env = os.environ.copy()
    env["BTC_PUZZLE_LAB_PYTHON"] = str(requested)
    env["PATH"] = f"{tmp_path}:/usr/bin:/bin"
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Python 3.12+ is required" in proc.stderr
    assert "apt build deps" not in proc.stdout
