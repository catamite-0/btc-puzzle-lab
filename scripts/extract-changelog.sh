#!/usr/bin/env bash
# Extract one version section from CHANGELOG.md for GitHub Release bodies.
# Usage: scripts/extract-changelog.sh 0.3.0 [CHANGELOG.md]
set -euo pipefail

version="${1:?usage: extract-changelog.sh VERSION [CHANGELOG]}"
changelog="${2:-CHANGELOG.md}"

python3 - "$version" "$changelog" <<'PY'
import re
import sys
from pathlib import Path

version, path = sys.argv[1], Path(sys.argv[2])
text = path.read_text(encoding="utf-8")
pattern = rf"^## \[{re.escape(version)}\].*?(?=^## |\Z)"
match = re.search(pattern, text, flags=re.M | re.S)
if not match:
    raise SystemExit(f"changelog section not found for {version}: {path}")
print(match.group(0).rstrip() + "\n")
PY
