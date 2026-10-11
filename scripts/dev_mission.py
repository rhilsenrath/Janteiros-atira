"""Run one specialist mission locally against the scouting websites, to develop and debug it.

    uv run python scripts/dev_mission.py tender
    uv run python scripts/dev_mission.py mail-approvals
    uv run python scripts/dev_mission.py erp GROUP-CODE        (requires a notebook with requirements: --notebook path)
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from janteiros.browser.kit import Tab  # noqa: E402
from janteiros.browser.pool import BrowserPool  # noqa: E402
from janteiros.notebook import Fact  # noqa: E402
from janteiros.planner import make_plan  # noqa: E402
from janteiros.run_context import load  # noqa: E402
from janteiros.specialist import Specialist  # noqa: E402
from janteiros.team import MAIL_GROUPS, Team, erp_mission, mail_mission, supplier_mission, tender_mission  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mission")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--notebook", help="preload facts from a notebook.jsonl")
    a = ap.parse_args()
    ctx = load()
    out = Path("runs") / ("dev-" + a.mission + (("-" + a.arg) if a.arg else "") + "-" + time.strftime("%H%M%S"))
    team = Team(out)
    team.brief = ctx.brief
    bp = await make_plan(team.llm, ctx.brief, ctx.access, ctx.submission, team.log)
    team.set_plan(bp)
    if a.notebook:
        for line in Path(a.notebook).read_text().splitlines():
            d = json.loads(line)
            team.notebook.facts.append(Fact(**{**d, "id": len(team.notebook.facts) + 1}))
    role_of = {v: k for k, v in bp.sites.items()}
    pool = await BrowserPool().start(1)
    try:
        if a.mission == "tender":
            role, m = "tender", tender_mission(team)
        elif a.mission == "supplier":
            role, m = "supplier", supplier_mission(team)
        elif a.mission.startswith("mail"):
            kinds = dict(MAIL_GROUPS)[a.mission]
            role, m = "mail", mail_mission(team, a.mission, kinds, a.mission == MAIL_GROUPS[0][0])
        elif a.mission == "erp":
            reqs = [f for f in team.notebook.ok("requirement") if f.data.get("catalog_group") == a.arg]
            role, m = "erp", erp_mission(team, a.arg, reqs)
        else:
            raise SystemExit("unknown mission")
        site = next(x for x in ctx.access if x["siteId"] == role_of[role])
        tab = Tab(site, await pool.page(0), team.log, role)
        m.start_url = tab.base_url + "/"
        spec = Specialist(a.mission, tab, team, team.manuals[role])
        res = await spec.run(m)
        print("RESULT", res)
        print("FACTS")
        for f in team.notebook.facts:
            print(" ", f.status, f.brief()[:400])
        print("LLM", team.llm.usage)
    finally:
        await pool.close()


asyncio.run(main())
