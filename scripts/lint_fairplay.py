"""Fair-play linter: fail if the package contains any value seen in the public scenario.

The list of values lives OUTSIDE the package (local/public_values.txt, gitignored), one value per line.
Run before every test run or submission:  uv run python scripts/lint_fairplay.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "local" / "public_values.txt"


def packaged_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / f for f in out if (ROOT / f).is_file()]


def main() -> int:
    if not VALUES.exists():
        print(f"no value list at {VALUES}; nothing to check")
        return 0
    values = [v.strip() for v in VALUES.read_text(encoding="utf-8").splitlines() if v.strip() and not v.startswith("#")]
    hits = []
    for f in packaged_files():
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
    print(f"{len(hits)} public-scenario values found in {len(packaged_files())} packaged files")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
