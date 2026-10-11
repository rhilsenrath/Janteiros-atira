"""The specialist loop: observe → decide (model) → act (helper) → verify → record.

Every specialist is this same loop with a different manual. The model chooses every navigation target,
search and document; helpers only execute on what it chose. A code watcher follows each step and wakes the
supervisor only when it sees trouble (no progress, repetition, login loop, budget). Each step runs under a
watchdog so one hanging page can never stall a mission.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .browser.kit import Observation, Tab, clear_obstacles, short_url
from .tools import FACT_NAMES, FACT_TOOLS, NAV_TOOLS, TALK_TOOLS, Ctx, args_of

PAGE_CHANGING = {"click", "goto", "back", "press_key", "read_all_pages", "search"}
NO_REPEAT_CHECK = FACT_NAMES | {"done", "site_note", "ask_supervisor", "request_tan", "sign_and_submit"}
STEP_TIMEOUT_S = 200


@dataclass
class Mission:
    id: str
    kind: str  # tender | supplier | erp | mail | dispatcher
    goal: str
    checklist: list[str]
    context: Callable[[], str]
    max_steps: int = 40
    budget_s: float = 420.0
    start_url: str | None = None
    thinking: bool = False
    done_check: Callable[[], list[str]] | None = None
    traps: str = ""
    deadline: float | None = None  # monotonic time by which the mission's result must exist
    stop_when_complete: bool = False  # finish as soon as the notebook satisfies the checklist


@dataclass
class MissionResult:
    mission: str
    status: str
    summary: str = ""
    not_found: list[str] = field(default_factory=list)
    steps: int = 0
    seconds: float = 0.0


@dataclass
class LoopState:
    t0: float
    history: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    big_output: str = ""
    keep_big: int = 0
    image: str | None = None
    last_progress: int = 0
    seen_urls: set[str] = field(default_factory=set)
    last_sigs: list[str] = field(default_factory=list)
    read_offset: int = 0
    facts_before: int = 0
    last_fact_step: int = 0
    warned: set[str] = field(default_factory=set)
    status: str = "steps"
    summary: str = ""
    not_found: list[str] = field(default_factory=list)


class Specialist:
    def __init__(self, name: str, tab: Tab, team: Any, manual: str):
        self.name = name
        self.tab = tab
        self.team = team
        self.manual = manual
        self.llm = team.llm
        self.log = team.log
        self.recorder = team.recorder
        self.cancelled = False

    # ---------- hooks (the dispatcher overrides them) ----------
    def system_prompt(self, mission: Mission) -> str:
        return (
            self.team.common_manual
            + "\n\n# Your website and how to work it\n" + self.manual
            + ("\n\n# Traps that matter for this mission\n" + mission.traps if mission.traps else "")
        )

    def tools(self, mission: Mission) -> list[dict]:
        return NAV_TOOLS + FACT_TOOLS.get(mission.kind, []) + TALK_TOOLS

    async def before_step(self, mission: Mission, st: LoopState, obs: Observation) -> None:
        """Called after each observation; may set st.status to stop the mission."""

    async def execute_special(self, mission: Mission, st: LoopState, name: str, args: dict, obs: Observation) -> str | None:
        """Tools only some specialists have; return None when the tool is not special."""
        return None

    # ---------- the loop ----------
    async def run(self, mission: Mission) -> MissionResult:
        st = LoopState(t0=time.monotonic(), facts_before=len([f for f in self.team.notebook.facts if f.agent == self.name]))
        self.log("mission_start", agent=self.name, mission=mission.id, goal=mission.goal[:300])
        if mission.start_url:
            try:
                await asyncio.wait_for(self.tab.goto(mission.start_url), timeout=60)
            except asyncio.TimeoutError:
                await self.tab.recover(1)
        step = 0
        sys_prompt, tools = self.system_prompt(mission), self.tools(mission)
        while step < mission.max_steps and st.status == "steps":
            if self.cancelled:
                st.status = "cancelled"
                break
            if time.monotonic() - st.t0 > mission.budget_s:
                st.status = "timeout"
                break
            step += 1
            try:
                await asyncio.wait_for(self._step(mission, st, step, sys_prompt, tools), timeout=STEP_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.log("step_watchdog", agent=self.name, mission=mission.id, step=step)
                st.history.append(f"{step}. (this step hung and was aborted; the page was reloaded)")
                await self._safe_recover()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a broken step must never end the mission
                self.log("step_error", agent=self.name, mission=mission.id, step=step, error=repr(e)[:300])
                st.history.append(f"{step}. (internal error: {str(e).splitlines()[0][:100]}; continuing)")
                await self._safe_recover()
        res = MissionResult(mission=mission.id, status=st.status, summary=st.summary, not_found=st.not_found, steps=step,
                            seconds=round(time.monotonic() - st.t0, 1))
        self.log("mission_end", agent=self.name, mission=mission.id, status=st.status, steps=step, seconds=res.seconds, summary=st.summary[:300])
        return res

    async def _safe_recover(self) -> None:
        try:
            await asyncio.wait_for(self.tab.recover(1), timeout=40)
        except Exception:  # noqa: BLE001
            pass

    async def _step(self, mission: Mission, st: LoopState, step: int, sys_prompt: str, tools: list[dict]) -> None:
        tab = self.tab
        try:
            notes = await asyncio.wait_for(clear_obstacles(tab), timeout=70)
        except asyncio.TimeoutError:
            notes = []
            await tab.recover(1)
        if any(n.startswith("logged in") for n in notes) and step > 1:
            back = [n for n in notes if n.startswith("returned to")]
            st.hints.append("The session had expired; the kit logged in again" + (f" and {back[0]}" if back else "") + ". Check where you are and continue.")
        obs = await tab.observe()
        tz = _site_tz(obs.text) or getattr(tab, "tz", None)
        if tz:
            tab.tz = tz
        await self.before_step(mission, st, obs)
        if st.status != "steps":
            return
        url_key = short_url(obs.url)
        page_sig = url_key + "|" + str(hash(re.sub(r"\[\d+\]", "", obs.text)[:4000]))
        if page_sig not in st.seen_urls:  # a page (or page state) never seen before counts as progress
            st.seen_urls.add(page_sig)
            st.last_progress = step
        n_facts = len([f for f in self.team.notebook.facts if f.agent == self.name])
        if n_facts > st.facts_before:
            st.facts_before = n_facts
            st.last_progress = step
            st.last_fact_step = step
        if mission.stop_when_complete and mission.done_check and step > 1 and not mission.done_check():
            st.status, st.summary = "done", "finished: the checklist is complete"
            return
        # supervision by exception: checklist complete in the notebook and nothing new for a while → finish
        if mission.done_check and step - st.last_fact_step >= 4 and step > 4 and not mission.done_check():
            st.status, st.summary = "done", "finished by the supervisor: checklist complete, no new facts in the last steps"
            self.log("auto_done", agent=self.name, mission=mission.id, step=step)
            return
        if step - st.last_progress >= 6 and step % 3 == 0:
            hint = await self.team.supervisor_hint(self.name, mission, st.history[-8:], obs.render(limit=5000))
            if hint:
                st.hints.append("Supervisor: " + hint)
        user = self._user_message(mission, st.history, st.hints, obs.render(offset=st.read_offset), st.big_output, step)
        st.hints, st.read_offset = [], 0
        st.keep_big -= 1
        if st.keep_big <= 0:
            st.big_output = ""
        content: Any = user
        if st.image:
            content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + st.image}}]
            st.image = None
        try:
            msg = await self.llm.chat([{"role": "system", "content": sys_prompt}, {"role": "user", "content": content}],
                                      tools, agent=self.name, thinking=mission.thinking, max_tokens=7000)
        except RuntimeError as e:
            st.history.append(f"{step}. (model unavailable: {str(e)[:80]})")
            await asyncio.sleep(2)
            return
        calls = list(msg.tool_calls or [])
        if not calls:
            st.history.append(f"{step}. (no tool call) {(msg.content or '')[:120]}")
            st.hints.append("Always answer with tool calls.")
            return
        page_changed = False
        for call in calls[:14]:
            name = call.function.name
            args = args_of(call)
            sig = name + json.dumps(args, sort_keys=True, ensure_ascii=False)[:200] + "@" + url_key
            if name not in NO_REPEAT_CHECK and st.last_sigs.count(sig) >= 2:
                st.history.append(f"{step}. {name}{_short(args)} → blocked: repeated three times on the same page without effect; choose another action")
                continue
            if page_changed and name not in NO_REPEAT_CHECK and name != "wait":
                st.history.append(f"{step}. {name}{_short(args)} → skipped: the page changed after the previous action; look again")
                continue
            try:
                result, changed = await self._execute(mission, st, step, name, args, obs, tz)
            except asyncio.TimeoutError:
                result, changed = "the page did not respond in time; look again", True
                await tab.recover(0)
            except Exception as e:  # noqa: BLE001 - report every helper failure to the model
                result, changed = f"error: {str(e).splitlines()[0][:160]}", False
            page_changed = page_changed or changed
            st.last_sigs = (st.last_sigs + [sig])[-6:]
            st.history.append(f"{step}. {name}{_short(args)} → {result}")
            self.log("step", agent=self.name, mission=mission.id, step=step, tool=name, args=_short(args, 300), result=str(result)[:300], url=url_key)
            if st.status != "steps":
                break

    async def _execute(self, mission: Mission, st: LoopState, step: int, name: str, args: dict, obs: Observation, tz) -> tuple[str, bool]:
        tab = self.tab
        special = await self.execute_special(mission, st, name, args, obs)
        if special is not None:
            return special, name in ("request_tan", "sign_and_submit", "click", "fill_cascade")
        if name in FACT_NAMES:
            ctx = Ctx(agent=self.name, site=tab.site_id, url=obs.url, step=step, tz=tz)
            return self.recorder.record(name, args, ctx), False
        if name == "done":
            missing = mission.done_check() if mission.done_check else []
            # soft items (that may legitimately not exist) block done only once; hard items block until the budget runs out
            hard = [m for m in missing if not m.startswith("SOFT:")]
            soft = [m for m in missing if m.startswith("SOFT:") and m not in st.warned]
            st.warned.update(soft)
            if (hard or soft) and step < mission.max_steps - 2 and time.monotonic() - st.t0 < mission.budget_s * 0.9:
                return "not finished, still missing: " + "; ".join(m.replace("SOFT: ", "") for m in (hard + soft)[:8]), False
            st.summary = str(args.get("summary") or "")
            st.not_found = [str(x) for x in (args.get("not_found") or [])]
            st.status = "done"
            return "mission finished", False
        if name == "site_note":
            self.team.add_site_note(tab.site_id, str(args.get("note") or ""), self.name)
            return "noted for the team", False
        if name == "ask_supervisor":
            return "supervisor: " + await self.team.supervisor_answer(self.name, mission, str(args.get("question") or ""), obs.render(limit=5000)), False
        if name == "read_more":
            st.read_offset = int(args.get("offset") or 0)
            return f"next view starts at offset {st.read_offset}", False
        if name == "look":
            st.image = await tab.look()
            return "screenshot attached to the next view", False
        if name == "wait":
            await asyncio.sleep(min(float(args.get("seconds") or 2), 8))
            return "waited", False
        if name == "read_all_pages":
            text = await asyncio.wait_for(paginate(tab, int(args.get("id") or 0), int(args.get("max_pages") or 12)), timeout=150)
            if text.startswith("("):
                return text, True
            st.big_output, st.keep_big = text, 3
            return f"read {text.count('=== page')} pages; their text is shown below (kept for your next turns)", True
        if name == "read_pages":
            urls = [str(u) for u in (args.get("urls") or []) if u]
            text = await asyncio.wait_for(tab.read_pages(urls), timeout=200)
            st.big_output, st.keep_big = text[:40000], 2
            return f"read {len(urls[:8])} pages; their text is shown below", False
        if name == "open_document":
            kind, text = await asyncio.wait_for(tab.fetch_document(str(args.get("url") or "")), timeout=60)
            st.big_output, st.keep_big = f"=== document {args.get('url')} ({kind})\n{text[:24000]}", 3
            return f"{kind} document read; its text is shown below", False
        result = await asyncio.wait_for(self._nav(name, args), timeout=60)
        changed = name in PAGE_CHANGING
        if changed and (name in ("goto", "back") or "now at" in result):
            st.keep_big = min(st.keep_big, 1)
        return result, changed

    async def _nav(self, name: str, a: dict) -> str:
        tab = self.tab
        if name == "click":
            return await tab.click(int(a["id"]))
        if name == "fill":
            return await tab.fill(int(a["id"]), str(a.get("text", "")))
        if name == "search":
            return await tab.search(int(a["id"]), str(a.get("text", "")))
        if name == "select":
            return await tab.select(int(a["id"]), str(a.get("option", "")))
        if name == "check":
            return await tab.check(int(a["id"]), bool(a.get("checked", True)))
        if name == "set_slider":
            return await tab.set_slider(int(a["id"]), str(a.get("value", "100")))
        if name == "press_key":
            return await tab.press(str(a.get("key") or "Enter"), int(a["id"]) if a.get("id") is not None else None)
        if name == "hover":
            return await tab.hover(int(a["id"]))
        if name == "goto":
            return await tab.goto(str(a.get("url") or ""))
        if name == "back":
            return await tab.back()
        return f"unknown tool {name}"

    def _user_message(self, mission: Mission, history: list[str], hints: list[str], page: str, big_output: str, step: int) -> str:
        parts = [f"# Mission ({mission.id})\n{mission.goal}"]
        ctx = mission.context()
        if ctx:
            parts.append("# What the team already knows\n" + ctx)
        notes = self.team.site_notes(self.tab.site_id)
        if notes:
            parts.append("# Notes from teammates about this website\n" + notes)
        if mission.checklist:
            missing = mission.done_check() if mission.done_check else []
            parts.append("# Checklist\n" + "\n".join(f"- {c}" for c in mission.checklist)
                         + ("\nStill missing according to the notebook: " + "; ".join(m.replace("SOFT: ", "") for m in missing[:10]) if missing
                            else ("\nEVERYTHING ON THE CHECKLIST IS RECORDED. Call done now (with a short summary) unless you know of a page "
                                  "with required data that you have not read yet. Do not re-read or re-check pages." if mission.done_check else "")))
        mine = self.team.my_facts(self.name)
        if mine:
            parts.append("# Facts you recorded so far (do not record them again)\n" + mine)
        if history:
            parts.append("# Your last steps\n" + "\n".join(history[-12:]))
        if hints:
            parts.append("# Notes\n" + "\n".join(hints))
        parts.append(f"# Step {step} of at most {mission.max_steps}")
        if mission.deadline is not None:
            left = int(mission.deadline - time.monotonic())
            parts.append(f"# Time left: {left} seconds" + (" — HURRY: omit every position you have not started yet and go straight to signing." if left < 150 else ""))
        if big_output:
            parts.append("# Output of your last helper call\n" + big_output[:30000])
        parts.append("# Current page\n" + page)
        return "\n\n".join(parts)


def _short(args: dict, limit: int = 140) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return "(" + (s[:limit] + "…" if len(s) > limit else s) + ")"


def _site_tz(text: str) -> str | None:
    m = re.search(r"Zeit(?:zone|angaben)\s*:\s*([A-Z][A-Za-z_]+/[A-Za-z_]+|UTC)", text)
    return m.group(1) if m else None


async def paginate(tab: Tab, jid: int, max_pages: int = 12) -> str:
    """Helper: follow a model-chosen next/load-more control and collect every page's text."""
    obs = tab.obs
    if obs is None:
        return "(no page observed)"
    m = re.search(r"\[%d\] (link|button) \"([^\"]*)\"" % jid, obs.text)
    if not m:
        return f"(element [{jid}] is not a link or button on this page)"
    kind, label = m.group(1), m.group(2)
    pages = [obs.text]
    logins_before = tab.logins
    for _ in range(max(1, min(max_pages, 20)) - 1):
        jid_now = None
        if tab.obs is not None:
            mm = re.search(r"\[(\d+)\] %s \"%s\"(?! \(disabled\))" % (kind, re.escape(label)), tab.obs.text)
            if mm:
                jid_now = int(mm.group(1))
        if jid_now is None:
            break
        before = tab.obs.text if tab.obs else ""
        await tab.click(jid_now)
        await clear_obstacles(tab)
        if tab.logins != logins_before:
            pages.append("(the session expired while paging; re-open the list and continue from here)")
            break
        new = await tab.observe()
        if new.text == before:
            break
        pages.append(new.text)
        if kind == "button":
            pages = [new.text]  # load-more appends to the same page: the last view holds everything
    if len(pages) == 1:
        return "=== page 1\n" + pages[0]
    first_lines = set(pages[0].splitlines())
    out = ["=== page 1\n" + pages[0]]
    for i, p in enumerate(pages[1:], start=2):
        lines = [ln for ln in p.splitlines() if ln.startswith("|") or ln not in first_lines]
        out.append(f"=== page {i}\n" + "\n".join(lines))
    return "\n".join(out)
