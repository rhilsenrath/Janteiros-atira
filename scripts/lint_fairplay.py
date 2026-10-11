"""Fair-play linter: fail if the package contains any value seen in the public scenario.

The list of values lives OUTSIDE the package (local/public_values.txt, gitignored), one value per line.
The files checked are the ones the atira CLI would upload (its own dry run), not what git tracks.
Run before every test run or submission:  uv run python scripts/lint_fairplay.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "local" / "public_values.txt"


def packaged_files() -> list[Path]:
    if not shutil.which("atira"):
        sys.exit("atira CLI not found; the linter needs it to know what goes in the package")
    out = subprocess.run(["atira", "test-run", str(ROOT), "--dry-run", "--json"], capture_output=True, text=True,
                         check=True).stdout
    return [ROOT / p for p in json.loads(out)["paths"]]


def main() -> int:
    if not VALUES.exists():
        print(f"no value list at {VALUES}; nothing to check")
        return 0
    values = [v.strip() for v in VALUES.read_text(encoding="utf-8").splitlines() if v.strip() and not v.startswith("#")]
    files = packaged_files()
    hits = []
    for f in files:
        if f.suffix in (".lock",) or f.name == "uv.lock":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for v in values:
            if v in text:
                line = text[: text.index(v)].count("\n") + 1
                hits.append(f"{f.relative_to(ROOT)}:{line}: {v!r}")
    for h in hits:
        print(h)
    print(f"{len(hits)} public-scenario values found in {len(files)} packaged files")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
