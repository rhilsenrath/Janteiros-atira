"""The browser kit: observe a page as clean text, act on numbered elements, clear obstacles.

Everything here is a helper. Helpers read the page the model chose, act on the element the model
picked and clear generic obstacles (login, cookie wall, error page, pop-up, slider gate). They never
decide where to go or what matters.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PWError
from playwright.async_api import Frame, Page

READER_JS = (Path(__file__).parent / "reader.js").read_text(encoding="utf-8")
PORTEIRO_JS = (Path(__file__).parent / "porteiro.js").read_text(encoding="utf-8")

MAX_OBS_CHARS = 16000


@dataclass
class Observation:
    url: str
    title: str
    text: str
    signals: dict
    ranges: list[tuple[int, int, Frame]] = field(default_factory=list)
    taken_at: float = field(default_factory=time.monotonic)

    def render(self, offset: int = 0, limit: int = MAX_OBS_CHARS) -> str:
        body = self.text[offset: offset + limit]
        more = ""
        if offset + limit < len(self.text):
            more = f"\n… (page continues: {len(self.text) - offset - limit} more characters; call read_more(offset={offset + limit}))"
        sig = []
        if self.signals.get("dialogs"):
            sig.append("open dialog: " + "; ".join(d.get("label", "")[:60] for d in self.signals["dialogs"]))
        if self.signals.get("password"):
            sig.append("login form visible")
        if self.signals.get("gates"):
            locked = [g for g in self.signals["gates"] if g.get("locked")]
            if locked:
                sig.append(f"{len(locked)} locked gate(s)")
        if self.signals.get("decoys"):
            sig.append(f"{self.signals['decoys']} hidden decoy element(s) removed")
        head = f"URL: {self.url}\nTITLE: {self.title}\n"
        if sig:
            head += "SIGNALS: " + " | ".join(sig) + "\n"
        return head + "PAGE:\n" + body + more


def same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


def short_url(url: str) -> str:
    p = urlparse(url)
    return p.path + (("?" + p.query) if p.query else "")


async def observe(page: Page) -> Observation:
    """Read every frame of the page (iframes inlined where they sit) into one text."""
    counter = [1]
    ranges: list[tuple[int, int, Frame]] = []
    signals: dict[str, Any] = {"password": False, "dialogs": [], "gates": [], "decoys": 0}

    async def read(frame: Frame, tag: str, depth: int) -> str:
        start = counter[0]
        try:
            res = await asyncio.wait_for(frame.evaluate(READER_JS, {"startId": start, "frameTag": tag}), timeout=12)
        except asyncio.TimeoutError:
            return "(frame still loading; look again in a moment)"
        except PWError as e:
            return f"(frame not readable: {str(e).splitlines()[0][:120]})"
        counter[0] = res["nextId"]
        ranges.append((start, counter[0], frame))
        sg = res["signals"]
        signals["password"] = signals["password"] or sg.get("password", False)
        signals["dialogs"] += sg.get("dialogs", [])
        signals["gates"] += sg.get("gates", [])
        signals["decoys"] += sg.get("decoys", 0)
        text = res["text"]
        if depth < 5:
            for child in frame.child_frames:
                try:
                    el = await asyncio.wait_for(child.frame_element(), timeout=5)
                    key = await asyncio.wait_for(el.get_attribute("data-jframe"), timeout=5)
                except (PWError, asyncio.TimeoutError):
                    continue
                if not key or ("{{FRAME:%s}}" % key) not in text:
                    continue
                sub = await read(child, key + "_", depth + 1)
                text = text.replace("{{FRAME:%s}}" % key, f"\n┌─ frame {short_url(child.url)}\n{sub}\n└─ end of frame\n", 1)
        return re.sub(r"\{\{FRAME:[^}]+\}\}", "", text)

    text = await read(page.main_frame, "f", 0)
    title = ""
    try:
        title = await asyncio.wait_for(page.title(), timeout=5)
    except (PWError, asyncio.TimeoutError):
        pass
    return Observation(url=page.url, title=title, text=text, signals=signals, ranges=ranges)


class Tab:
    """One browser tab working on one website, with popups followed automatically."""

    def __init__(self, site: dict, page: Page, log: Callable[..., None], name: str):
        self.site = site
        self.site_id = site["siteId"]
        self.base_url = site["url"].rstrip("/")
        self.creds = site.get("credentials") or {}
        self.page = page
        self.name = name
        self.log = log
        self.obs: Observation | None = None
        self.stack: list[Page] = []
        self.logins = 0
        self.confirm_armed = False
        self.dialog_log: list[str] = []
        self.error_retries: dict[str, int] = {}
        self.return_to: str | None = None
        self.last_good_url: str | None = None
        self._attach(page)

    # ---------- page plumbing ----------
    def _attach(self, page: Page) -> None:
        page.on("dialog", lambda d: asyncio.ensure_future(self._on_dialog(d)))
        page.on("popup", lambda p: asyncio.ensure_future(self._on_popup(p)))

    async def _on_dialog(self, dialog) -> None:
        msg = f"{dialog.type}: {dialog.message}"
        self.dialog_log.append(msg)
        self.log("browser_dialog", tab=self.name, message=msg, armed=self.confirm_armed)
        try:
            if dialog.type == "confirm" and not self.confirm_armed:
                await dialog.dismiss()
            else:
                await dialog.accept()
        except PWError:
            pass

    async def _on_popup(self, popup: Page) -> None:
        self._attach(popup)
        self.stack.append(self.page)
        self.page = popup
        self.log("popup_opened", tab=self.name, url=popup.url)

    def _current(self) -> Page:
        while self.page.is_closed() and self.stack:
            self.page = self.stack.pop()
        return self.page

    async def settle(self, ms: int = 450) -> None:
        page = self._current()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PWError:
            pass
        await asyncio.sleep(ms / 1000)
        self._current()

    # ---------- reading ----------
    async def observe(self) -> Observation:
        """Observe the page; a blank, frozen or unreadable tab is reloaded and finally replaced by a fresh one."""
        for attempt in range(4):
            page = self._current()
            try:
                self.obs = await asyncio.wait_for(observe(page), timeout=35)
                if self.obs.text.strip() and page.url.startswith("http"):
                    self.last_good_url = page.url
                    return self.obs
            except (PWError, asyncio.TimeoutError):
                pass
            if attempt == 3:
                break
            await self.recover(attempt + (1 if not page.url.startswith("http") else 0))
        if self.obs is None:
            self.obs = Observation(url=self._current().url, title="", text="(the page could not be read)", signals={})
        return self.obs

    async def recover(self, attempt: int = 0) -> None:
        """A page that hangs: stop it, reload it, and finally replace it with a fresh tab on the last good URL."""
        page = self._current()
        url = page.url if page.url.startswith("http") else (self.last_good_url or self.base_url + "/")
        self.log("tab_recover", tab=self.name, attempt=attempt, url=url)
        try:
            if attempt == 0:
                await asyncio.wait_for(page.evaluate("window.stop()"), timeout=3)
                await asyncio.sleep(0.5)
                if not page.url.startswith("http"):
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                return
            if attempt == 1:
                if page.url.startswith("http"):
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                else:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                return
        except (PWError, asyncio.TimeoutError):
            pass
        try:
            fresh = await asyncio.wait_for(page.context.new_page(), timeout=15)
            self._attach(fresh)
            try:
                await asyncio.wait_for(page.close(), timeout=5)
            except (PWError, asyncio.TimeoutError):
                pass
            self.page = fresh
            self.stack = []
            await fresh.goto(url, wait_until="domcontentloaded", timeout=20000)
        except (PWError, asyncio.TimeoutError):
            pass

    async def look(self) -> str:
        page = self._current()
        try:
            png = await page.screenshot(type="jpeg", quality=60, timeout=12000)
        except PWError:
            await self.recover(2)
            png = await self._current().screenshot(type="jpeg", quality=60, timeout=12000)
        return base64.b64encode(png).decode()

    # ---------- element lookup ----------
    def locate(self, jid: int):
        if not self.obs:
            return None
        for start, end, frame in self.obs.ranges:
            if start <= jid < end:
                if frame.is_detached():
                    return None
                return frame.locator(f'[data-jid="{jid}"]').first
        return None

    # ---------- actions ----------
    async def click(self, jid: int) -> str:
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        before = self._current().url
        try:
            await loc.click(timeout=4000)
        except PWError as e:
            msg = str(e)
            if "intercepts pointer events" in msg:
                await clear_obstacles(self)
                try:
                    await loc.click(timeout=3000)
                except PWError:
                    await self._js_click(loc)
            else:
                await self._js_click(loc)
        await self.settle()
        after = self._current().url
        return "clicked" + (f"; now at {short_url(after)}" if after != before else "")

    async def _js_click(self, loc) -> None:
        try:
            await loc.evaluate("e => e.click()")
        except PWError:
            pass

    async def fill(self, jid: int, text: str) -> str:
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        try:
            await loc.fill(text, timeout=4000)
        except PWError:
            try:
                await loc.evaluate("(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles:true})); e.dispatchEvent(new Event('change', {bubbles:true})); }", text)
            except PWError as e:
                return f"could not fill [{jid}]: {str(e).splitlines()[0][:120]}"
        return "filled"

    async def search(self, jid: int, text: str) -> str:
        """Fill a field and submit its form: Enter first, then the form's submit button if nothing happened."""
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        res = await self.fill(jid, text)
        if res != "filled":
            return res
        before = self._current().url
        try:
            await loc.press("Enter", timeout=3000)
        except PWError:
            pass
        await self.settle(500)
        if self._current().url == before:
            try:
                btn = loc.locator("xpath=ancestor::form[1]//*[self::button or (self::input and (@type='submit' or @type='image'))]").first
                if await btn.count():
                    await btn.click(timeout=4000)
                    await self.settle(500)
            except PWError:
                pass
        after = self._current().url
        return f"searched {text!r}" + (f"; now at {short_url(after)}" if after != before else "")

    async def select(self, jid: int, choice: str) -> str:
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        try:
            opts = await loc.evaluate("e => Array.from(e.options).map(o => [o.value, o.textContent.trim()])")
        except PWError as e:
            return f"[{jid}] is not a select: {str(e).splitlines()[0][:100]}"
        want = choice.strip()
        value = None
        for v, t in opts:
            if want == v or want == t:
                value = v
                break
        if value is None:
            low = want.lower()
            for v, t in opts:
                if low and (low in t.lower() or low == v.lower()):
                    value = v
                    break
        if value is None:
            return f"option {choice!r} not found; options are: " + " | ".join(f"{t} {{{v}}}" for v, t in opts[:60])
        try:
            await loc.select_option(value=value, timeout=4000)
        except PWError:
            await loc.evaluate("(e, v) => { e.value = v; e.dispatchEvent(new Event('change', {bubbles:true})); }", value)
        await self.settle(250)
        return f"selected {value!r}"

    async def check(self, jid: int, checked: bool = True) -> str:
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        try:
            await loc.set_checked(checked, timeout=4000)
        except PWError:
            try:
                cur = await loc.evaluate("e => e.checked")
                if cur != checked:
                    await loc.click(timeout=3000, force=True)
            except PWError as e:
                return f"could not set [{jid}]: {str(e).splitlines()[0][:120]}"
        await self.settle(200)
        return "checked" if checked else "unchecked"

    async def set_slider(self, jid: int, value: str) -> str:
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        return await _slide(loc, value, self._current())

    async def press(self, key: str, jid: int | None = None) -> str:
        if jid is not None:
            loc = self.locate(jid)
            if loc is None:
                return f"element [{jid}] is not on the current page; observe again"
            await loc.press(key, timeout=4000)
        else:
            await self._current().keyboard.press(key)
        await self.settle()
        return f"pressed {key}"

    async def hover(self, jid: int) -> str:
        loc = self.locate(jid)
        if loc is None:
            return f"element [{jid}] is not on the current page; observe again"
        try:
            await loc.hover(timeout=3000)
        except PWError as e:
            return f"could not hover: {str(e).splitlines()[0][:100]}"
        await asyncio.sleep(0.3)
        return "hovering"

    async def goto(self, url: str) -> str:
        page = self._current()
        target = urljoin(page.url if page.url.startswith("http") else self.base_url + "/", url)
        if not same_site(target, self.base_url):
            return f"refused: {target} is not on this website ({self.base_url})"
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=25000)
        except PWError as e:
            await self.recover(0)
            return f"navigation failed ({str(e).splitlines()[0][:80]}); the page may be slow: look again or retry"
        await self.settle()
        return f"now at {short_url(self._current().url)}"

    async def back(self) -> str:
        page = self._current()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
        except PWError:
            pass
        await self.settle()
        return f"now at {short_url(self._current().url)}"

    async def read_pages(self, urls: list[str], max_pages: int = 8) -> str:
        """Helper: read several model-chosen pages of this site in a scratch tab and return their text."""
        page = self._current()
        scratch = None
        out = []
        try:
            scratch = await asyncio.wait_for(page.context.new_page(), timeout=15)
            helper = Tab(self.site, scratch, self.log, self.name + "-read")
            helper.tz = getattr(self, "tz", None)
            for raw in urls[:max_pages]:
                target = urljoin(page.url if page.url.startswith("http") else self.base_url + "/", str(raw))
                if not same_site(target, self.base_url):
                    out.append(f"=== {raw}\n(refused: not on this website)")
                    continue
                try:
                    await scratch.goto(target, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.wait_for(clear_obstacles(helper), timeout=60)
                    if short_url(helper._current().url) != short_url(target) and "login" not in target:
                        await helper._current().goto(target, wait_until="domcontentloaded", timeout=20000)
                        await asyncio.wait_for(clear_obstacles(helper), timeout=40)
                    obs = await helper.observe()
                    out.append((short_url(obs.url), _strip_chrome(obs.text)))
                except (PWError, asyncio.TimeoutError) as e:
                    out.append((str(raw), f"(could not read: {str(e).splitlines()[0][:80]})"))
        finally:
            if scratch is not None:
                try:
                    await asyncio.wait_for(scratch.close(), timeout=5)
                except (PWError, asyncio.TimeoutError):
                    pass
        # lines every page repeats (menus, headers, footers) are shown once, on the first page only
        if len(out) > 1:
            sets = [set(t.splitlines()) for _, t in out]
            common = set.intersection(*sets)
            trimmed = [out[0]] + [(u, "\n".join(ln for ln in t.splitlines() if ln not in common or ln.startswith("|"))) for u, t in out[1:]]
        else:
            trimmed = out
        return "\n".join(f"=== {u}\n{t}" for u, t in trimmed)

    async def fetch_document(self, url: str) -> tuple[str, str]:
        """Fetch a model-selected document with the tab's session and return (kind, text)."""
        page = self._current()
        target = urljoin(page.url, url)
        if not same_site(target, self.base_url):
            return "refused", f"{target} is not on this website"
        # fetch inside the page: the browser's own network path (proxy) and session cookies
        got = await page.evaluate("""async (u) => {
            const r = await fetch(u, {credentials: 'include'});
            const buf = new Uint8Array(await r.arrayBuffer());
            let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
            return {status: r.status, type: r.headers.get('content-type') || '', b64: btoa(bin)};
        }""", target)
        body = base64.b64decode(got["b64"])
        resp_headers = {"content-type": got["type"]}
        ctype = (resp_headers.get("content-type") or "").lower()
        name = urlparse(target).path.lower()
        if "zip" in ctype or name.endswith(".zip") or body[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(body))
            parts = []
            for info in zf.infolist()[:30]:
                data = zf.read(info)
                parts.append(f"=== {info.filename} ({info.file_size} bytes)\n" + _decode_file(info.filename, data)[:6000])
            return "zip", "\n".join(parts)
        if "pdf" in ctype or name.endswith(".pdf") or body[:4] == b"%PDF":
            return "pdf", _pdf_text(body)
        text = body.decode("utf-8", errors="replace")
        if "html" in ctype:
            return "html", text[:20000]
        return "text", text[:20000]


def _strip_chrome(text: str) -> str:
    """Drop element numbers from helper reads: they belong to a scratch tab and cannot be clicked."""
    return re.sub(r"\[\d+\] ", "", text)


def _decode_file(name: str, data: bytes) -> str:
    if name.lower().endswith(".pdf") or data[:4] == b"%PDF":
        return _pdf_text(data)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)[:20000]
    except Exception as e:  # noqa: BLE001 - any parse problem is reported to the model
        return f"(pdf could not be parsed: {e})"


async def _slide(loc, value: str, page: Page) -> str:
    try:
        await loc.fill(str(value), timeout=3000)
    except PWError:
        pass
    try:
        cur = await loc.evaluate("e => e.value")
        if str(cur) == str(value):
            return f"slider at {cur}"
    except PWError:
        pass
    # fall back to a real mouse drag across the track
    try:
        box = await loc.bounding_box()
        if box:
            y = box["y"] + box["height"] / 2
            await page.mouse.move(box["x"] + 2, y)
            await page.mouse.down()
            await page.mouse.move(box["x"] + box["width"] + 5, y, steps=12)
            await page.mouse.up()
        cur = await loc.evaluate("e => e.value")
        return f"slider at {cur}"
    except PWError as e:
        return f"could not move slider: {str(e).splitlines()[0][:100]}"


# ---------------- porteiro: generic obstacle hygiene ----------------

ROBOT_RE = re.compile(r"kein roboter|not a robot|mensch|human|ich bin kein", re.I)


async def clear_obstacles(tab: Tab, max_rounds: int = 10) -> list[str]:
    """Clear login forms, cookie walls, error pages, dialogs, receipt forms and slider gates, in every frame.

    After a forced re-login the kit returns to the page the session expired on."""
    notes: list[str] = []
    for _ in range(max_rounds):
        page = tab._current()
        acted = False
        for frame in list(page.frames):
            if frame.is_detached():
                continue
            try:
                st = await asyncio.wait_for(frame.evaluate(PORTEIRO_JS), timeout=8)
            except (PWError, asyncio.TimeoutError):
                continue
            try:
                note = await _clear_frame(tab, frame, st)
            except PWError as e:
                note = f"obstacle not cleared ({str(e).splitlines()[0][:80]})"
                tab.log("porteiro_error", tab=tab.name, error=str(e)[:200])
            if note:
                notes.append(note)
                acted = True
                break
        if not acted:
            break
        await tab.settle(350)
    if tab.return_to and any(n.startswith("logged in") for n in notes):
        page = tab._current()
        target = tab.return_to
        tab.return_to = None
        if short_url(page.url) != short_url(target) and same_site(target, tab.base_url):
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=20000)
                await tab.settle(300)
                notes.append(f"returned to {short_url(target)}")
                more = await clear_obstacles(tab, max_rounds=4)
                notes += more
            except PWError:
                pass
    if notes:
        tab.log("porteiro", tab=tab.name, notes=notes, url=tab._current().url)
    return notes


async def _clear_frame(tab: Tab, frame: Frame, st: dict) -> str | None:
    loc = lambda pid: frame.locator(f'[data-porteiro="{pid}"]').first  # noqa: E731
    if st.get("login"):
        lg = st["login"]
        if not tab.creds:
            return None
        m = re.search(r"[?&]returnTo=([^&#]+)", frame.url)
        if m:
            from urllib.parse import unquote
            back = unquote(m.group(1))
            if back not in ("/", "") and not back.startswith("/login"):
                tab.return_to = urljoin(tab.base_url + "/", back)
        if lg.get("user"):
            await loc(lg["user"]).fill(tab.creds.get("username", ""))
        await loc(lg["pass"]).fill(tab.creds.get("password", ""))
        tab.logins += 1
        if lg.get("submit"):
            await loc(lg["submit"]).click(timeout=5000)
        else:
            await loc(lg["pass"]).press("Enter")
        await tab.settle(600)
        return f"logged in (login #{tab.logins})"
    if st.get("error"):
        key = short_url(frame.url)
        n = tab.error_retries.get(key, 0) + 1
        tab.error_retries[key] = n
        if n > 4:
            return None
        await asyncio.sleep(min(0.6 * n, 2.5))
        if st["error"].get("retry"):
            try:
                await loc(st["error"]["retry"]).click(timeout=4000)
            except PWError:
                await frame.page.reload(wait_until="domcontentloaded")
        else:
            await frame.page.reload(wait_until="domcontentloaded")
        return f"error page '{st['error']['text'][:60]}' → retried ({n})"
    if st.get("consent"):
        await loc(st["consent"]["id"]).click(timeout=5000)
        return f"cookie consent: clicked '{st['consent']['text']}'"
    for d in st.get("dialogs") or []:
        if d.get("countdown"):
            await asyncio.sleep(2.0)
        closed = False
        if d.get("close"):
            try:
                await loc(d["close"]).click(timeout=2500)
                closed = True
            except PWError:
                try:
                    await loc(d["close"]).click(timeout=1500, force=True)
                    closed = True
                except PWError:
                    pass
        if not closed:
            try:
                await frame.page.keyboard.press("Escape")
            except PWError:
                pass
            try:
                still = await loc(d["id"]).evaluate("e => e.tagName === 'DIALOG' ? e.open : getComputedStyle(e).display !== 'none'")
            except PWError:
                still = False
            if still:
                try:
                    await loc(d["id"]).evaluate("e => { if (e.tagName === 'DIALOG') e.close(); else e.style.display = 'none'; }")
                except PWError:
                    pass
        return f"closed {'overlay' if d.get('overlay') else 'dialog'} '{d.get('label', '')[:50]}'"
    for g in st.get("gates") or []:
        res = await _slide(loc(g["range"]), g.get("max") or "100", frame.page)
        boxes = g.get("boxes") or []
        chosen = [b for b in boxes if ROBOT_RE.search(b.get("text") or "")] or (boxes[:1] if len(boxes) == 1 else [])
        for b in chosen:
            if not b.get("checked"):
                try:
                    await loc(b["id"]).check(timeout=3000)
                except PWError:
                    try:
                        await loc(b["id"]).click(timeout=2000, force=True)
                    except PWError:
                        pass
        return f"unlocked gate ({res}; {len(chosen)} checkbox)"
    if st.get("receipt"):
        rc = st["receipt"]
        await loc(rc["field"]).fill(rc["code"])
        for b in rc.get("boxes") or []:
            try:
                await loc(b).check(timeout=2000)
            except PWError:
                pass
        if rc.get("submit"):
            await loc(rc["submit"]).click(timeout=5000)
        await tab.settle(400)
        return f"receipt form: entered {rc['code']} and confirmed"
    return None
