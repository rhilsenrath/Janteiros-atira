"""Summarize a local run directory: missions, steps, model calls, facts and the answer.

    uv run python scripts/show_run.py [runs/<dir>] [--steps]
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = sorted(Path("runs").iterdir(), key=lambda p: p.stat().st_mtime)
    d = Path(args[0]) if args else runs[-1]
    ev = [json.loads(line) for line in (d / "events.jsonl").read_text().splitlines()]
    calls = defaultdict(lambda: [0, 0.0, 0, 0])
    for e in ev:
        if e["event"] == "llm_call":
            c = calls[e["agent"]]
            c[0] += 1
            c[1] += e.get("seconds") or 0
            c[2] += e.get("prompt") or 0
            c[3] += e.get("completion") or 0
    print(f"run {d}  ({ev[-1]['t']:.0f}s)")
    print("missions:")
    for e in ev:
        if e["event"] in ("mission_start", "mission_end", "go", "freeze", "collection_complete", "collection_end", "followup", "dispatch_result", "signed", "tan_requested"):
            body = {k: v for k, v in e.items() if k not in ("t", "event", "goal")}
            print(f"  {e['t']:7.1f} {e['event']:18s} {json.dumps(body, ensure_ascii=False)[:230]}")
    print("model calls per agent (calls, seconds, prompt tokens, completion tokens):")
    for a, c in sorted(calls.items()):
        print(f"  {a:22s} {c[0]:4d} {c[1]:7.1f}s {c[2]:8d} {c[3]:6d}")
    errs = Counter(e["event"] for e in ev if e["event"] in ("llm_error", "record_error", "tab_recover", "mission_crash", "supervisor_error"))
    if errs:
        print("problems:", dict(errs))
    if "--steps" in sys.argv:
        for e in ev:
            if e["event"] == "step":
                print(f"  {e['t']:7.1f} {e['agent']}#{e['step']} {e['tool']}{e['args'][:110]} → {e['result'][:140]}")
    nb = d / "notebook.jsonl"
    if nb.exists():
        kinds = Counter(json.loads(line)["kind"] for line in nb.read_text().splitlines())
        print("facts:", dict(kinds))
    ans = d / "answer.json"
    if ans.exists():
        print("answer:", ans.read_text()[:3000])


if __name__ == "__main__":
    main()
