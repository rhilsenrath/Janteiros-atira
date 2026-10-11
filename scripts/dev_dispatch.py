"""Run the dispatcher alone (prepare + final + TAN through the mail tab) with a given answer, on the scouting sites.

    uv run python scripts/dev_dispatch.py path/to/{answer,worksheet}.json --email bidder@domain

Scouting submissions are acknowledged by the portal but never count, so this exercises the whole signing flow.
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
from janteiros.dispatcher import Dispatcher, final_mission, prepare_mission  # noqa: E402
from janteiros.planner import make_plan  # noqa: E402
from janteiros.run_context import load  # noqa: E402
from janteiros.specialist import Specialist  # noqa: E402
from janteiros.team import Team, tan_mission  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("--email", required=True)
    ap.add_argument("--skip-prepare", action="store_true")
    a = ap.parse_args()
    bundle = json.loads(Path(a.bundle).read_text())
    answer, ws = bundle["answer"], bundle["worksheet"]
    ctx = load()
    team = Team(Path("runs") / ("dev-dispatch-" + time.strftime("%H%M%S")))
    team.brief = ctx.brief
    bp = await make_plan(team.llm, ctx.brief, ctx.access, ctx.submission, team.log)
    team.set_plan(bp)
    team.notebook.add("mail_identity", {"address": a.email, "domain": a.email.split("@")[1]}, agent="dev", step=0, origin={})
    role_of = {v: k for k, v in bp.sites.items()}
    pool = await BrowserPool().start(2)
    try:
        ttab = Tab(next(x for x in ctx.access if x["siteId"] == role_of["tender"]), await pool.page(0), team.log, "tender")
        mtab = Tab(next(x for x in ctx.access if x["siteId"] == role_of["mail"]), await pool.page(1), team.log, "mail")

        async def fetch_tan(request_no, requested_at):
            spec = Specialist("mail-tan", mtab, team, team.manuals["mail"])
            m = tan_mission(team, request_no, requested_at)
            m.start_url = mtab.base_url + "/"
            await spec.run(m)
            tans = [f for f in team.notebook.ok("mail_fact") if f.data.get("kind") == "tan" and f.at >= requested_at - 1]
            return str(tans[-1].data.get("tan")) if tans else None

        d = Dispatcher("dispatcher", ttab, team, team.manuals["dispatcher"], fetch_tan)
        if not a.skip_prepare:
            print("PREPARE", await d.run(prepare_mission(team, ttab.base_url + "/")))
        extra = {p: {"bore": c["attrs"].get("bore_mm"), "stroke": c["attrs"].get("stroke_mm"), "pressure": c["attrs"].get("pressure_bar"),
                     "mounting": c["attrs"].get("mounting"), "certification": c["attrs"].get("certification")}
                 for p, c in ws["positions"].items()}
        extra["_omit"] = [p for p, c in ws["positions"].items() if c.get("readiness") == "omit"]
        d.phase = "final"
        d.set_answer(answer, extra)
        table = d.bid_table(answer, extra)
        print(table)
        print("FINAL", await d.run(final_mission(team, table)))
        print("RECEIPT", d.receipt)
    finally:
        await pool.close()


asyncio.run(main())
