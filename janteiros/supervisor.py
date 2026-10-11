"""O Supervisor: plans from the brief, sends missions by need, watches the clock, gives the go.

The code half watches every specialist and the notebook all the time; the model half is only called by the
watcher (hints) or by a specialist's question. The supervisor never browses, never prices and never submits.
"""

from __future__ import annotations

import asyncio
import json
import time

import jsonschema

from .browser.kit import Tab, clear_obstacles
from .browser.pool import BrowserPool
from .dispatcher import Dispatcher, final_mission, prepare_mission
from .normalize import loose_id
from .planner import make_plan
from .run_context import RunContext
from .specialist import Mission, MissionResult, Specialist
from .team import MAIL_GROUPS, MAIL_SUBJECTS, Team, erp_mission, mail_mission, supplier_mission, tan_mission, tender_mission

RUN_BUDGET_S = 20 * 60
FREEZE_LEFT_S = 8 * 60      # stop collecting when this much time is left
BROWSER_OF = {"tender": 0, "supplier": 1, "erp": 2, "mail": 3}
MAX_TABS = {"tender": 1, "supplier": 2, "erp": 4, "mail": 4}


class Supervisor:
    def __init__(self, team: Team, ctx: RunContext):
        self.team = team
        self.ctx = ctx
        self.t0 = time.monotonic()
        self.pool = BrowserPool()
        self.tasks: dict[str, asyncio.Task] = {}
        self.specialists: dict[str, Specialist] = {}
        self.results: dict[str, object] = {}
        self.erp_started: set[str] = set()
        self.followups_done = 0
        self.tabs: dict[str, list[Tab]] = {}
        self.free: dict[str, list[Tab]] = {}
        self.dispatcher: Dispatcher | None = None
        self.prep_task: asyncio.Task | None = None
        self.go_given = False
        self.sites: dict[str, dict] = {}
        self.missions: dict[str, tuple[str, Mission]] = {}
        self.relaunched: set[str] = set()

    # ---------- time ----------
    def left(self) -> float:
        own = RUN_BUDGET_S - (time.monotonic() - self.t0)
        return min(own, self.ctx.seconds_left())

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    # ---------- tabs ----------
    async def _new_tab(self, role: str) -> Tab:
        page = await self.pool.page(BROWSER_OF[role])
        tab = Tab(self.sites[role], page, self.team.log, f"{role}{len(self.tabs.get(role, []))}")
        self.tabs.setdefault(role, []).append(tab)
        return tab

    async def _acquire(self, role: str) -> Tab:
        free = self.free.setdefault(role, [])
        if free:
            return free.pop(0)
        return await self._new_tab(role)

    def _release(self, role: str, tab: Tab) -> None:
        if self.dispatcher is not None and tab is self.dispatcher.tab:
            return
        self.free.setdefault(role, []).append(tab)

    def _launch(self, key: str, role: str, mission: Mission, tab: Tab | None = None) -> None:
        self.missions[key] = (role, mission)

        async def go() -> MissionResult:
            t = tab or await self._acquire(role)
            if mission.start_url is None:
                mission.start_url = t.base_url + "/"
            spec = Specialist(key, t, self.team, self.team.manuals[role])
            self.specialists[key] = spec
            try:
                return await spec.run(mission)
            finally:
                self._release(role, t)
        self.tasks[key] = asyncio.create_task(go(), name=key)

    # ---------- run ----------
    async def run(self) -> dict:
        team, log = self.team, self.team.log
        team.brief = self.ctx.brief
        bp = await make_plan(team.llm, self.ctx.brief, self.ctx.access, self.ctx.submission, log)
        team.set_plan(bp)
        for a in self.ctx.access:
            role = bp.sites.get(a["siteId"])
            if role and role not in self.sites:
                self.sites[role] = a
        missing_roles = [r for r in ("tender", "supplier", "erp", "mail") if r not in self.sites]
        if missing_roles:
            log("plan_problem", missing_roles=missing_roles)
        await self.pool.start(4)

        # wave 1: tender, supplier and mail need only the brief; the ERP logs in early and waits for the groups
        if "tender" in self.sites:
            self._launch("tender", "tender", tender_mission(team))
        if "supplier" in self.sites:
            self._launch("supplier", "supplier", supplier_mission(team))
        if "mail" in self.sites:
            for i, (mid, kinds) in enumerate(MAIL_GROUPS):
                self._launch(mid, "mail", mail_mission(team, mid, kinds, i == 0))
        if "erp" in self.sites:
            self.tasks["erp-warmup"] = asyncio.create_task(self._warm_erp())

        try:
            await asyncio.wait_for(self._collect(), timeout=max(30, self.left() - FREEZE_LEFT_S + 30))
        except asyncio.TimeoutError:
            log("collect_watchdog", elapsed=round(self.elapsed()))
        except Exception as e:  # noqa: BLE001 - never lose the bid because of a supervision bug
            log("supervisor_error", error=repr(e)[:400])
        return await self._finish()

    async def _warm_erp(self) -> None:
        tab = await self._acquire("erp")
        try:
            await asyncio.wait_for(tab.goto(tab.base_url + "/"), timeout=60)
            await asyncio.wait_for(clear_obstacles(tab), timeout=90)
        except Exception:  # noqa: BLE001 - the ERP missions will retry
            pass
        finally:
            self._release("erp", tab)

    # ---------- collection ----------
    async def _collect(self) -> None:
        team, log = self.team, self.team.log
        last_report = 0.0
        while True:
            await asyncio.sleep(1.5)
            self._maybe_start_erp()
            self._maybe_prepare()
            for k, t in list(self.tasks.items()):
                if t.done() and k not in self.results:
                    try:
                        self.results[k] = t.result()
                    except Exception as e:  # noqa: BLE001
                        self.results[k] = repr(e)
                        log("mission_crash", mission=k, error=repr(e)[:400])
                        if k in self.missions and k not in self.relaunched and self.left() > FREEZE_LEFT_S + 60:
                            self.relaunched.add(k)
                            self.results.pop(k, None)
                            role, m = self.missions[k]
                            m.start_url = None
                            self._launch(k, role, m)
                            log("relaunch", mission=k)
            running = [k for k, t in self.tasks.items() if not t.done() and k != "erp-warmup"]
            res = team.compute()
            if self.elapsed() - last_report > 20:
                last_report = self.elapsed()
                log("progress", elapsed=round(self.elapsed()), left=round(self.left()), running=running,
                    readiness=res.readiness, missing=res.missing[:12], facts=len(team.notebook.facts),
                    llm_calls=team.llm.usage.calls, cost=round(team.llm.usage.cost, 4))
            complete = res.positions and all(v == "complete" for v in res.readiness.values()) and not res.missing
            if complete and not running:
                log("collection_complete", elapsed=round(self.elapsed()))
                return
            if self.left() < FREEZE_LEFT_S:
                log("freeze", elapsed=round(self.elapsed()), running=running, missing=res.missing[:20])
                return
            if not running and self._wave_done():
                todo = res.missing + self._uncovered_mail()
                if self.followups_done < 2 and self.left() > FREEZE_LEFT_S + 90 and todo:
                    self.followups_done += 1
                    if self._followups(todo):
                        continue
                log("collection_end", elapsed=round(self.elapsed()), missing=res.missing[:20])
                return

    def _uncovered_mail(self) -> list[str]:
        """Mail subjects whose mission did not finish: the engine cannot tell 'none exists' from 'not searched'."""
        out = []
        for mid, kinds in MAIL_GROUPS:
            r = self.results.get(mid)
            if isinstance(r, MissionResult) and r.status == "done":
                continue
            for k in kinds:
                if not [f for f in self.team.notebook.ok("mail_fact") if f.data.get("kind") == k]:
                    out.append(f"mail: {MAIL_SUBJECTS[k][0]} messages for the exact reference (search not completed)")
        return out

    def _wave_done(self) -> bool:
        need = [k for k in ["tender", "supplier"] + [m for m, _ in MAIL_GROUPS] if k in self.tasks]
        groups_pending = self._erp_groups_wanted() - self.erp_started
        return all(k in self.results for k in need) and not groups_pending

    def _erp_groups_wanted(self) -> set[str]:
        team = self.team
        cur = [f for f in team.notebook.ok("spec_revision") if f.data.get("is_current")]
        if not cur:
            return set()
        rev = loose_id(cur[-1].data.get("revision"))
        reqs = [f for f in team.notebook.ok("requirement") if loose_id(f.data.get("revision")) == rev and f.data.get("catalog_group")]
        wanted = {loose_id(f.data.get("requirement")) for f in team.notebook.ok("position")}
        if wanted:
            reqs = [r for r in reqs if loose_id(r.data.get("requirement")) in wanted]
        return {loose_id(r.data.get("catalog_group")) for r in reqs}

    def _maybe_start_erp(self) -> None:
        if "erp" not in self.sites:
            return
        team = self.team
        cur = [f for f in team.notebook.ok("spec_revision") if f.data.get("is_current")]
        if not cur:
            return
        rev = loose_id(cur[-1].data.get("revision"))
        reqs = [f for f in team.notebook.ok("requirement") if loose_id(f.data.get("revision")) == rev and f.data.get("catalog_group")]
        if not reqs:
            return
        wanted = {loose_id(f.data.get("requirement")) for f in team.notebook.ok("position")}
        if wanted:
            reqs = [r for r in reqs if loose_id(r.data.get("requirement")) in wanted]
        elif "supplier" not in self.results and self.elapsed() < 150:
            return  # wait a little for the positions, so unused requirements are not searched
        groups: dict[str, list] = {}
        for r in reqs:
            groups.setdefault(r.data.get("catalog_group"), []).append(r)
        running = [k for k, t in self.tasks.items() if k.startswith("erp-") and k != "erp-warmup" and not t.done()]
        for g, rs in groups.items():
            if loose_id(g) in self.erp_started or len(running) >= MAX_TABS["erp"]:
                continue
            self.erp_started.add(loose_id(g))
            key = f"erp-{g}"
            running.append(key)
            self._launch(key, "erp", erp_mission(team, g, rs))

    def _maybe_prepare(self) -> None:
        if self.prep_task or "tender" not in self.results:
            return
        if not self.team.bidder_address() and self.elapsed() < 120:
            return
        tab = self.tabs["tender"][0]
        if tab in self.free.get("tender", []):
            self.free["tender"].remove(tab)
        self.dispatcher = Dispatcher("dispatcher", tab, self.team, self.team.manuals["dispatcher"], self._fetch_tan)
        tf = self.team.notebook.ok("tender")
        url = tf[-1].origin.get("url") if tf else None
        self.prep_task = asyncio.create_task(self._prepare(url))

    async def _prepare(self, url: str | None) -> None:
        res = await self.dispatcher.run(prepare_mission(self.team, url))
        self.results["dispatch-prepare"] = res
        while not self.go_given:  # keep the portal session alive until the go
            await asyncio.sleep(40)
            if self.go_given:
                break
            try:
                await asyncio.wait_for(self.dispatcher.tab.page.reload(wait_until="domcontentloaded"), timeout=30)
                await asyncio.wait_for(clear_obstacles(self.dispatcher.tab), timeout=60)
            except Exception:  # noqa: BLE001
                pass

    def _followups(self, missing: list[str]) -> bool:
        """Re-route what is still missing to the site that most likely has it (nova rota)."""
        team, log = self.team, self.team.log
        by_site: dict[str, list[str]] = {}
        for item in missing:
            low = item.lower()
            if "(erp)" in low or any(w in low for w in ("price condition", "stock and lead", "status of", "erp search", "details of erp", "price scales")):
                by_site.setdefault("erp", []).append(item)
            elif low.startswith("mail:") or any(w in low for w in ("reserve", "exchange rate", "pool", "packaging", "delivery commitment", "bonus", "konditionsregeln", "metal index", "mailbox domain")):
                by_site.setdefault("mail", []).append(item)
            elif any(w in low for w in ("requirement", "revision", "pieces per set")):
                by_site.setdefault("supplier", []).append(item)
            elif "position" in low and not self.dispatcher:
                by_site.setdefault("tender", []).append(item)
        started = False
        for site, items in by_site.items():
            if site not in self.sites:
                continue
            key = f"followup-{site}-{self.followups_done}"
            goal = ("The rules engine still misses these items for the bid:\n- " + "\n- ".join(items[:10]) +
                    "\nFind them on this website (they may be in another place than usual: search with the key or the reference, check "
                    "other folders, tabs, documents and records) and record them. If an item is not on this website, say so in done.not_found.")
            m = Mission(id=key, kind=site, goal=goal, checklist=items[:10], context=team.base_context, max_steps=25,
                        budget_s=min(240, max(60, self.left() - FREEZE_LEFT_S)))
            self._launch(key, site, m)
            log("followup", mission=key, items=items[:10])
            started = True
        return started

    # ---------- TAN ----------
    async def _fetch_tan(self, request_no: str | None, requested_at: float) -> str | None:
        if "mail" not in self.sites:
            return None
        tab = await self._acquire("mail")
        try:
            spec = Specialist("mail-tan", tab, self.team, self.team.manuals["mail"])
            m = tan_mission(self.team, request_no, requested_at)
            m.start_url = tab.base_url + "/"
            await spec.run(m)
        finally:
            self._release("mail", tab)
        tans = [f for f in self.team.notebook.ok("mail_fact") if f.data.get("kind") == "tan" and f.at >= requested_at - 1]
        tans = [f for f in tans if loose_id(f.data.get("reference")) in ("", loose_id(self.team.plan.reference))]
        if request_no:
            exact = [f for f in tans if loose_id(f.data.get("request_no")) == loose_id(request_no)]
            tans = exact or tans
        return str(tans[-1].data.get("tan")).strip() if tans else None

    # ---------- go ----------
    async def _finish(self) -> dict:
        team, log = self.team, self.team.log
        for k, s in self.specialists.items():
            if k != "dispatcher":
                s.cancelled = True
        res = team.compute()
        answer = res.answer
        try:
            jsonschema.validate(answer, self.ctx.answer_schema or {"type": "object"})
        except jsonschema.ValidationError as e:
            log("schema_problem", error=str(e)[:300])
        out = team.out_dir
        (out / "answer.json").write_text(json.dumps(answer, indent=2, ensure_ascii=False))
        (out / "worksheet.json").write_text(json.dumps(res.worksheet(), indent=2, ensure_ascii=False, default=str))
        log("go", decision=answer["decision"], positions=len(answer["positions"]), total=answer["total_net"],
            discount=answer["discount_percent"], readiness=res.readiness, missing=res.missing[:20], notes=res.notes[:10])
        self.go_given = True
        if self.prep_task and not self.prep_task.done() and "dispatch-prepare" not in self.results:
            # the preparation is still running: give it a little time to reach the bid-type step
            try:
                await asyncio.wait_for(asyncio.shield(self.prep_task), timeout=min(90, max(10, self.left() - 300)))
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        if self.prep_task and not self.prep_task.done():
            self.prep_task.cancel()
        if not self.dispatcher:
            tab = self.tabs["tender"][0] if self.tabs.get("tender") else await self._new_tab("tender")
            self.dispatcher = Dispatcher("dispatcher", tab, team, team.manuals["dispatcher"], self._fetch_tan)
            tf = team.notebook.ok("tender")
            pm = prepare_mission(team, tf[-1].origin.get("url") if tf else tab.base_url + "/")
            pm.budget_s = min(pm.budget_s, max(60, self.left() - 240))
            await self.dispatcher.run(pm)
        extra: dict = {}
        for p, c in res.positions.items():
            extra[p] = {"bore": c.attrs.get("bore_mm"), "stroke": c.attrs.get("stroke_mm"), "pressure": c.attrs.get("pressure_bar"),
                        "mounting": c.attrs.get("mounting"), "certification": c.attrs.get("certification")}
        extra["_omit"] = [p for p, r in res.readiness.items() if r == "omit"] if answer["decision"] == "bid" else sorted(res.readiness)
        self.dispatcher.phase = "final"
        table = self.dispatcher.bid_table(answer, extra)
        status = "not started"
        attempt = 0
        # never end the run without a signed bid while there is time: re-run the dispatcher from wherever the wizard is
        while not self.dispatcher.receipt and self.left() > 20 and attempt < 6:
            attempt += 1
            fm = final_mission(team, table)
            fm.budget_s = max(30, self.left() - 15)
            fm.deadline = time.monotonic() + fm.budget_s
            if attempt > 1:
                fm.goal += "\nYou were interrupted: the wizard keeps what was saved. Look where it is now and continue from there."
            try:
                result = await asyncio.wait_for(self.dispatcher.run(fm), timeout=fm.budget_s + 20)
                status = result.status
            except asyncio.TimeoutError:
                status = "watchdog"
            except Exception as e:  # noqa: BLE001
                status = f"error {e!r}"[:200]
            log("dispatch_result", attempt=attempt, status=status, receipt=self.dispatcher.receipt, left=round(self.left()))
        summary = {"answer": answer, "receipt": self.dispatcher.receipt, "elapsed": round(self.elapsed(), 1),
                   "llm_calls": team.llm.usage.calls, "llm_cost_usd": round(team.llm.usage.cost, 4), "missing": res.missing}
        (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return summary

    async def close(self) -> None:
        await self.pool.close()
