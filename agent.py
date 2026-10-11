"""Janteiros RFQ agent system: entry point for `uv run python agent.py` (sandbox) or local development."""

import asyncio
import json
import os
import time
from pathlib import Path

from janteiros.run_context import load
from janteiros.supervisor import Supervisor
from janteiros.team import Team


async def main() -> None:
    ctx = load()
    out = Path(os.environ.get("JANTEIROS_OUT", "runs")) / time.strftime("%Y%m%d-%H%M%S")
    team = Team(out)
    sup = Supervisor(team, ctx)
    try:
        summary = await sup.run()
        print("SUMMARY " + json.dumps(summary, ensure_ascii=False, default=str)[:3000], flush=True)
    finally:
        await sup.close()


if __name__ == "__main__":
    asyncio.run(main())
