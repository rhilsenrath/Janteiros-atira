"""Bounded live DOM observations and validated actions; never execute model-generated code."""
import asyncio
import re
from typing import Literal
from urllib.parse import urlsplit, urljoin
from pydantic import Field, model_validator
from core.contracts import Record
from tools.logging import redact

class BrowserAction(Record):
    operation: Literal["inspect", "click", "fill", "select", "press", "scroll", "back", "reload", "expand", "reveal", "set", "goto", "switch_tab", "read_document", "finish"]
    target: str | None = None
    value: str | None = None
    secret: Literal["username", "password"] | None = None
    url: str | None = None
    download: int | None = Field(default=None, ge=0)
    member: str | None = None
    tab: int | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0, le=1000000)
    source_ids: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_shape(self):
        if self.operation in ("click", "fill", "select", "press", "reveal", "set") and not self.target:
            raise ValueError("Action requires a currently observed target")
        if self.operation in ("fill", "select", "press", "set") and self.value is None and self.secret is None:
            raise ValueError("Form action requires a value or secret reference")
        if self.secret is not None and (self.operation != "fill" or self.value is not None):
            raise ValueError("Credentials may only be injected into a fill action")
        if self.operation == "press" and self.value not in ("Enter", "Tab", "Escape", "ArrowDown", "ArrowUp",
                "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Space"):
            raise ValueError("Unsupported key")
        if self.operation == "goto" and not self.url:
            raise ValueError("Navigation requires an observed URL")
        if self.operation == "switch_tab" and self.tab is None:
            raise ValueError("Tab action requires tab index")
        return self

# Zero-width characters that portals insert into identifiers ("V\u200bG-...") to defeat copying/searching.
INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")
DOCUMENT_TYPES = {"text/csv": ".csv", "application/pdf": ".pdf", "application/zip": ".zip",
    "application/x-zip-compressed": ".zip", "text/html": ".html", "text/plain": ".txt", "application/json": ".json",
    "text/xml": ".xml", "application/xml": ".xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


def clean(text):
    return INVISIBLE.sub("", text) if isinstance(text, str) else text


def document_name(url, headers):
    """A served document's name: Content-Disposition filename, else URL path plus a Content-Type suffix
    (portals serve e.g. /documents/0 as CSV)."""
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
    if match:
        return match.group(1).strip()
    path = urlsplit(url).path
    suffix = DOCUMENT_TYPES.get(headers.get("content-type", "").split(";")[0].strip().lower(), "")
    return path if not suffix or path.lower().endswith(suffix) else path + suffix


# Expands collapsed content only: never follows links, submits forms or clicks overlays' actions.
EXPAND_SCRIPT = """() => {
  let opened = 0;
  document.querySelectorAll('details:not([open])').forEach(d => { d.open = true; opened++; });
  document.querySelectorAll('[aria-expanded="false"]').forEach(e => {
    if (opened >= 80 || e.closest('a[href]') || (e.closest('form') && e.type === 'submit')) return;
    e.click(); opened++;
  });
  return opened;
}"""

# Closes rating/feedback/survey popups only, and only through a dismiss control: never submits a rating, never
# touches consent, login or access forms. Returns the dismissed popups' text for the observation.
DISMISS_FEEDBACK_SCRIPT = """() => {
  const ASK = /bewert|feedback|umfrage|zufrieden|hilfreich|wie gefällt|sterne|rating|rate (this|us)|survey|satisf|helpful/i;
  const NEVER = /cookie|anmeld|login|passwort|password|benutzername|beleg|kenntnis|bedingungen|zustimm|consent/i;
  const DISMISS = /^(schließen|schliessen|×|✕|x|später|spaeter|nicht jetzt|nein,? danke|überspringen|ausblenden|close|not now|no,? thanks|skip|dismiss|later)$/i;
  const overlays = Array.from(document.querySelectorAll('body *')).slice(0, 4000).filter(e => {
    if (!e.getClientRects().length) return false;
    const st = getComputedStyle(e);
    return e.matches('[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]') ||
           (st.position === 'fixed' && (parseInt(st.zIndex) || 0) > 0);
  }).filter((e, i, all) => !all.some(o => o !== e && o.contains(e)));
  const dismissed = [];
  for (const o of overlays) {
    const text = (o.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!ASK.test(text) || NEVER.test(text) || o.querySelector('input[type=password]')) continue;
    const button = Array.from(o.querySelectorAll('button,a,[role=button]')).find(b =>
      DISMISS.test((b.innerText || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim()));
    if (button) { button.click(); dismissed.push(text.slice(0, 160)); }
    else { o.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); }
  }
  return dismissed;
}"""

# Text of overlays (dialogs, fixed popups/notices): volatile, so it is not counted as page content.
OVERLAY_TEXT_SCRIPT = """() => {
  const out = [];
  const nodes = Array.from(document.querySelectorAll('body *')).slice(0, 4000);
  for (const e of nodes) {
    if (out.some(o => o.contains(e))) continue;
    const st = getComputedStyle(e);
    if (e.matches('[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]') ||
        (st.position === 'fixed' && (parseInt(st.zIndex) || 0) > 0)) out.push(e);
  }
  return out.map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 10);
}"""

# Generic DOM inspection, constant code supplied by the application rather than an LLM.
CONTROL_SCRIPT = """elements => elements.map((e, index) => ({
 index, tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
 type: e.getAttribute('type'), name: e.getAttribute('name'), disabled: !!e.disabled,
 filled: ('value' in e) ? !!e.value : null,
 label: (e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.getAttribute('placeholder') ||
         e.innerText || e.getAttribute('title') || '').trim().slice(0, 160),
 href: e.href || e.src || e.data || null,
 options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => ({label:o.text, value:o.value})) : null,
 row: (e.closest('tr')?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 140) || null,
 hint: (e.getAttribute('title') || e.getAttribute('aria-description') || e.getAttribute('data-tooltip') ||
        e.getAttribute('data-title') || '').trim().slice(0, 160) || null,
 checked: (e.type === 'checkbox' || e.type === 'radio') ? e.checked
   : (e.hasAttribute('aria-checked') ? e.getAttribute('aria-checked') === 'true' : null),
 value: e.type === 'password' ? null : (e.tagName === 'SELECT' ? (e.selectedOptions?.[0]?.text || null)
   : (('value' in e && ['INPUT', 'TEXTAREA'].includes(e.tagName) && !['checkbox', 'radio', 'button', 'submit'].includes(e.type))
      ? String(e.value).slice(0, 80) : (e.getAttribute('aria-valuenow') ?? e.getAttribute('aria-valuetext')))),
 range: (e.type === 'range' || e.getAttribute('role') === 'slider')
   ? {min: e.min || e.getAttribute('aria-valuemin'), max: e.max || e.getAttribute('aria-valuemax'),
      step: e.step || null} : null,
 required: (e.required || e.getAttribute('aria-required') === 'true') || null,
 expanded: e.hasAttribute('aria-expanded') ? e.getAttribute('aria-expanded') === 'true'
   : (e.tagName === 'SUMMARY' ? !!e.parentElement?.open : null),
 in_overlay: !!e.closest('[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]') || (() => {
   for (let a = e; a && a !== document.body; a = a.parentElement) {
     const st = getComputedStyle(a);
     if (st.position === 'fixed' && (parseInt(st.zIndex) || 0) > 0) return true;
   }
   return false; })(),
 visible: !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden'
})).filter(e => e.visible)"""
CONTROLS = ("summary,[aria-expanded],a,button,input:not([type=hidden]),select,textarea,[role=button],[role=link],"
            "[role=tab],[role=checkbox],[role=radio],[role=switch],[role=slider],[role=spinbutton],[role=combobox],"
            "[role=option],[role=menuitem],[role=menuitemcheckbox],label:has(input[type=checkbox]),"
            "label:has(input[type=radio]),[contenteditable=true],embed,object")

# Sets a form control to a model-chosen value and fires the events frameworks listen to.
SET_VALUE_SCRIPT = """(e, value) => {
  const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  setter ? setter.call(e, value) : (e.value = value);
  e.dispatchEvent(new Event('input', {bubbles: true}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
  return e.value;
}"""

# Images carry information only through their accessible text (alt/title); no image recognition is done.
IMAGE_TEXT_SCRIPT = """() => Array.from(document.images).filter(i => i.getClientRects().length)
  .map(i => (i.alt || i.title || '').trim()).filter(Boolean).slice(0, 20)"""

# Text that is part of the page's DOM but missing from innerText: SVG text, CSS-generated content
# (::before/::after), aria-label/title on non-control elements, image file names, readonly fields.
# Pixels of raster images/canvas are never read (no OCR).
DOM_TEXT_SCRIPT = """() => {
  const out = [], seen = new Set();
  const add = (kind, text, near) => {
    text = (text || '').replace(/\\s+/g, ' ').trim();
    if (!text || seen.has(kind + text)) return;
    seen.add(kind + text);
    out.push({kind, text: text.slice(0, 120), near: (near || '').replace(/\\s+/g, ' ').trim().slice(0, 80) || null});
  };
  const context = e => (e.closest('label,fieldset,section,form,div')?.innerText || '');
  document.querySelectorAll('svg text, svg title').forEach(t => add('svg', t.textContent, context(t)));
  const nodes = Array.from(document.querySelectorAll('body *')).slice(0, 3000);
  for (const e of nodes) {
    if (!e.getClientRects().length) continue;
    for (const pseudo of ['::before', '::after']) {
      const c = getComputedStyle(e, pseudo).content;
      if (c && c !== 'none' && c !== 'normal' && /[0-9A-Za-z]/.test(c)) add('css' + pseudo, c.replace(/^["']|["']$/g, ''), e.innerText || context(e));
    }
    const label = e.getAttribute('aria-label') || e.getAttribute('title');
    if (label && !e.matches('a,button,input,select,textarea,[role=button],[role=link]') && !(e.innerText || '').includes(label))
      add('attribute', label, context(e));
    if (e.matches('input[readonly],output')) add('readonly', e.value || e.textContent, context(e));
  }
  document.querySelectorAll('img').forEach(i => {
    if (!i.getClientRects().length || i.alt) return;
    const name = decodeURIComponent((i.currentSrc || i.src || '').split('/').pop().split('?')[0]);
    add('image_without_text', (name.startsWith('data:') ? 'inline image' : name) + ` (${i.naturalWidth}x${i.naturalHeight})`, context(i));
  });
  document.querySelectorAll('canvas').forEach(c => add('canvas', c.getAttribute('aria-label') || `canvas ${c.width}x${c.height}`, context(c)));
  return out.slice(0, 40);
}"""

class BrowserNavigator:
    def __init__(self, page, site, secrets=(), action_timeout=10000, text_limit=12000):
        self.page = page
        self.site = site
        self.secrets = list(secrets)
        self.action_timeout = action_timeout
        self.text_limit = text_limit
        self.targets = {}
        self.links = set()
        self.downloads = []
        self.listened = set()
        self.document_cache = {}
        self.statuses = {}  # url -> HTTP status of its last top-level load
        self.visited = []  # same-site pages seen during investigation, most recent last
        self.auth_paths = set()  # login/consent paths; never a useful destination
        self._listen(page)

    def _listen(self, page):
        if id(page) not in self.listened:
            page.on("download", lambda download: self.downloads.append(download))
            # Generic modal acknowledgement; never accept confirmation automatically.
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
            page.on("response", self._record_status)
            self.listened.add(id(page))

    def _record_status(self, response):
        """HTTP status of each top-level page load, so error pages are recognised without knowing their text."""
        try:
            if response.request.is_navigation_request() and response.frame.parent_frame is None:
                self.statuses[response.url] = response.status
        except Exception:
            pass

    def allowed(self, url):
        parsed, origin = urlsplit(url), urlsplit(self.site.url)
        return parsed.scheme in ("http", "https") and parsed.netloc == origin.netloc and not parsed.username and not parsed.password

    async def initialize(self):
        async def restrict_network(route):
            url = route.request.url
            if self.allowed(url) or urlsplit(url).scheme in ("data", "blob", "about"):
                await route.continue_()
            else:
                await route.abort()
        await self.page.context.route("**/*", restrict_network)
        # Portals can answer with transient error pages; retry the entry URL a bounded number of times.
        for attempt in range(3):
            try:
                await self.page.goto(self.site.url, wait_until="domcontentloaded", timeout=self.action_timeout)
                return
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1 + attempt)

    async def ensure_page(self):
        """Recover from a closed or crashed tab: continue in an open tab of the same browser, or a new one."""
        is_closed = getattr(self.page, "is_closed", None)
        if not callable(is_closed) or not is_closed():
            return
        open_pages = [page for page in self.page.context.pages if not page.is_closed()]
        self.page = open_pages[-1] if open_pages else await self.page.context.new_page()
        self._listen(self.page)
        if not self.allowed(self.page.url):
            target = next((url for url in reversed(self.visited)), self.site.url)
            await self.page.goto(target, wait_until="domcontentloaded", timeout=self.action_timeout)

    async def dismiss_feedback(self):
        """Close rating/feedback popups without a model step; they only block the page."""
        dismissed = []
        for frame in self.page.frames[:12]:
            if frame.url not in ("about:blank", "") and not self.allowed(frame.url):
                continue
            try:
                dismissed += await frame.evaluate(DISMISS_FEEDBACK_SCRIPT)
            except Exception:
                pass
        if dismissed:
            await asyncio.sleep(.2)
        return [clean(text) for text in dismissed]

    async def observe(self, offset=0):
        await self.ensure_page()
        dismissed = await self.dismiss_feedback()
        from tools.logging import redact
        self.targets = {}
        self.links = set()
        frames = []
        remaining_chars = self.text_limit
        remaining_controls = 120
        for frame_number, frame in enumerate(self.page.frames[:12]):
            if frame.url not in ("about:blank", "") and not self.allowed(frame.url):
                continue
            try:
                text = clean(await frame.locator("body").inner_text(timeout=self.action_timeout))
                if frame_number and self.allowed(frame.url):
                    self.links.add(frame.url)  # an embedded same-site frame can be opened as its own page
                controls = await frame.locator(CONTROLS).evaluate_all(CONTROL_SCRIPT)
                entries = []
                for control in controls[:remaining_controls]:
                    target = f"f{frame_number}:e{control['index']}"
                    self.targets[target] = (frame, control['index'], control)
                    if control.get('href') and self.allowed(control['href']):
                        self.links.add(control['href'])
                    entries.append({"target": target, **{k: clean(v) for k, v in control.items()
                                                         if k not in ('index', 'visible')}})
                remaining_controls -= len(entries)
                overlay = [c.get("label") for c in entries if c.get("in_overlay")]
                try:
                    image_text = [clean(t)[:160] for t in await frame.evaluate(IMAGE_TEXT_SCRIPT)]
                except Exception:
                    image_text = []
                try:
                    dom_text = [{k: clean(v) for k, v in item.items()} for item in await frame.evaluate(DOM_TEXT_SCRIPT)]
                except Exception:
                    dom_text = []
                try:
                    overlay_text = [clean(t)[:600] for t in await frame.evaluate(OVERLAY_TEXT_SCRIPT)]
                except Exception:
                    overlay_text = []
                frames.append({"frame": frame_number, "url": frame.url,
                    "overlay_controls": overlay or None,
                    "image_text": image_text or None,
                    "dom_text": dom_text or None,
                    "overlay_text": redact("\n".join(overlay_text), self.secrets) or None,
                    "text": redact(text[offset:offset+remaining_chars], self.secrets),
                    "offset": offset, "total_characters": len(text), "controls": entries})
                remaining_chars = max(0, remaining_chars - len(frames[-1]["text"]))
            except Exception as error:
                frames.append({"frame": frame_number, "error_type": type(error).__name__})
        tabs = [{"tab": i, "url": page.url} for i,page in enumerate(self.page.context.pages) if self.allowed(page.url)]
        if self.allowed(self.page.url) and urlsplit(self.page.url).path not in self.auth_paths:
            if self.page.url in self.visited:
                self.visited.remove(self.page.url)
            self.visited = [*self.visited, self.page.url][-15:]
        observation = {"url": self.page.url, "frames": frames, "tabs": tabs,
                       "visited_pages": self.visited[-10:],
                       "http_status": self.statuses.get(self.page.url),
                       "auto_dismissed": dismissed or None,
                       "downloads": [{"index": i, "name": d.suggested_filename} for i,d in enumerate(self.downloads)]}
        # Labels/URLs can contain account values too; redact the entire observation representation.
        import json
        return json.loads(redact(json.dumps(observation, ensure_ascii=False), self.secrets))

    async def set_control(self, locator, control, value):
        """Model-chosen value for checkboxes/radios/switches (true/false), selects (option label or value),
        native sliders/number fields (number) and ARIA sliders (keyboard steps towards the number)."""
        kind, role = (control.get("type") or "").lower(), control.get("role")
        truthy = str(value).strip().lower() in ("true", "1", "yes", "ja", "on", "checked")
        if kind in ("checkbox", "radio") or role in ("checkbox", "radio", "switch", "menuitemcheckbox"):
            if kind in ("checkbox", "radio"):
                await locator.set_checked(truthy, timeout=self.action_timeout)
            elif bool(control.get("checked")) != truthy:
                await locator.click(timeout=min(self.action_timeout, 4000))
        elif control.get("tag") == "select":
            try:
                await locator.select_option(label=value, timeout=self.action_timeout)
            except Exception:
                await locator.select_option(value=value, timeout=self.action_timeout)
        elif control.get("tag") in ("input", "textarea") and kind in ("range", "number", ""):
            await locator.evaluate(SET_VALUE_SCRIPT, str(value))
        elif role in ("slider", "spinbutton"):
            target = float(str(value).replace(",", "."))
            await locator.focus(timeout=self.action_timeout)
            for _ in range(200):
                now = await locator.get_attribute("aria-valuenow")
                if now is None or abs(float(now) - target) < 1e-9:
                    break
                await locator.press("ArrowRight" if float(now) < target else "ArrowLeft")
                if await locator.get_attribute("aria-valuenow") == now:
                    break  # the control does not move further
        else:
            raise ValueError("set works on checkboxes, radios, switches, selects, sliders and number fields")

    async def reveal(self, target, limit=40):
        """Model-selected repetition: click every in-page control that has the same label as the chosen one
        (e.g. one toggle per list row) and record the text each click reveals. Links are never followed;
        a URL change or a login form stops it at once (traps)."""
        if target not in self.targets:
            raise ValueError("Target not present in the current observation")
        frame, _, chosen = self.targets[target]
        def is_link(control):
            href = control.get("href")
            return bool(href) and not str(href).startswith(("#", "javascript:"))
        if is_link(chosen):
            raise ValueError("reveal works on in-page buttons/toggles, not on links")
        start_url = self.page.url
        candidates = [c for c in await frame.locator(CONTROLS).evaluate_all(CONTROL_SCRIPT)
                      if c["tag"] == chosen["tag"] and c["label"] == chosen["label"] and not is_link(c)][:limit]
        lines = lambda text: [line.strip() for line in clean(text).splitlines() if line.strip()]
        results = []
        baseline = set(lines(await frame.locator("body").inner_text(timeout=self.action_timeout)))
        for control in candidates:
            before = set(lines(await frame.locator("body").inner_text(timeout=self.action_timeout)))
            try:
                await frame.locator(CONTROLS).nth(control["index"]).click(timeout=min(self.action_timeout, 3000))
            except Exception as error:
                results.append(f"{clean(control.get('row') or control['label'])} => (not clickable: {type(error).__name__})")
                continue
            await asyncio.sleep(.25)
            if self.page.url != start_url:
                results.append("(stopped: the control navigated away from the page)")
                await self.page.go_back(wait_until="domcontentloaded", timeout=self.action_timeout)
                break
            if await self.page.locator("input[type=password]").count():
                results.append("(stopped: a login form appeared)")
                break
            after = lines(await frame.locator("body").inner_text(timeout=self.action_timeout))
            # Only text that was never on the page before (a closing neighbour is not "revealed").
            revealed = [line for line in after if line not in before and line not in baseline]
            results.append(f"{clean(control.get('row') or control['label'])} => {' | '.join(revealed) or '(nothing new)'}")
        text = redact("\n".join(results), self.secrets)
        return {"name": f"revealed by '{chosen['label']}' ({len(candidates)} controls)", "text": text}

    async def act(self, action: BrowserAction, *, authenticating=False):
        await self.ensure_page()
        if action.operation == "finish":
            return
        if action.operation == "inspect":
            return
        if action.operation == "read_document":
            cache_key = (action.download, action.url, action.member)
            if cache_key in self.document_cache:
                return dict(self.document_cache[cache_key])
            from tools.documents import read_document, MAX_BYTES
            from pathlib import Path
            if action.download is not None:
                if action.download >= len(self.downloads):
                    raise ValueError("Unknown download index")
                selected = self.downloads[action.download]
                local_path = await selected.path()
                if local_path is None or Path(local_path).stat().st_size > MAX_BYTES:
                    raise ValueError("Download missing or exceeds size limit")
                result = read_document(Path(local_path).read_bytes(), selected.suggested_filename, action.member)
                result = {k: clean(v) for k, v in result.items()}
                self.document_cache[cache_key] = result
                return dict(result)
            if not action.url or action.url not in self.links or not self.allowed(action.url):
                raise ValueError("Read only a document URL observed on the assigned site")
            response = await self.page.context.request.get(action.url, timeout=self.action_timeout)
            try:
                if not response.ok or not self.allowed(response.url):
                    raise ValueError("Selected document is unavailable or redirected outside assigned site")
                if int(response.headers.get("content-length", "0")) > MAX_BYTES:
                    raise ValueError("Selected document exceeds size limit")
                result = read_document(await response.body(), document_name(action.url, response.headers),
                                       action.member)
                result = {k: clean(v) for k, v in result.items()}
                self.document_cache[cache_key] = result
                return dict(result)
            finally:
                await response.dispose()
        if action.operation == "goto":
            url = urljoin(self.page.url, action.url)
            if not self.allowed(url) or (url not in self.links and url not in self.visited):
                raise ValueError("Only observed same-site links or visited_pages are navigable")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.action_timeout)
        elif action.operation == "switch_tab":
            pages = self.page.context.pages
            if action.tab >= len(pages) or not self.allowed(pages[action.tab].url):
                raise ValueError("Tab is outside assigned site")
            self.page = pages[action.tab]
            self._listen(self.page)
        elif action.operation == "back":
            await self.page.go_back(wait_until="domcontentloaded", timeout=self.action_timeout)
            # Skip login/consent pages left in the history by a re-login; they only lead forward again.
            for _ in range(4):
                if urlsplit(self.page.url).path not in self.auth_paths:
                    break
                if await self.page.go_back(wait_until="domcontentloaded", timeout=self.action_timeout) is None:
                    break
            if not self.allowed(self.page.url):
                raise ValueError("Back navigation left assigned site")
        elif action.operation == "reveal":
            return await self.reveal(action.target)
        elif action.operation == "expand":
            # Open every collapsed section of the current page (answers, accordions) in one step.
            for frame in self.page.frames[:12]:
                if frame.url not in ("about:blank", "") and not self.allowed(frame.url):
                    continue
                await frame.evaluate(EXPAND_SCRIPT)
            await asyncio.sleep(.3)
        elif action.operation == "reload":
            # Recovery from flaky error pages; repeats only the current same-site request.
            await self.page.reload(wait_until="domcontentloaded", timeout=self.action_timeout)
            if not self.allowed(self.page.url):
                raise ValueError("Reload left assigned site")
        elif action.operation == "scroll":
            await self.page.mouse.wheel(0, 600)
        else:
            if action.target not in self.targets:
                raise ValueError("Target not present in the current observation")
            frame, index, control = self.targets[action.target]
            locator = frame.locator(CONTROLS).nth(index)
            # Prefer observed accessible names when they uniquely identify this live element.
            if control.get("label"):
                if control["tag"] in ("input", "select", "textarea"):
                    semantic = frame.get_by_label(control["label"], exact=True)
                else:
                    role = control.get("role") or {"button":"button", "a":"link"}.get(control["tag"])
                    semantic = frame.get_by_role(role, name=control["label"], exact=True) if role else None
                if semantic is not None and await semantic.count() == 1:
                    locator = semantic
            # Recheck identity to avoid filling/clicking an element replaced since observation.
            current = await locator.evaluate("e => (" + CONTROL_SCRIPT + ")([e])[0]")
            if not current or any(current.get(k) != control.get(k) for k in ("tag", "name", "type", "label", "href")):
                raise ValueError("Target changed since observation; inspect again")
            form_action = await locator.evaluate("e => e.closest('form')?.action || null")
            if form_action and not self.allowed(form_action):
                raise ValueError("Cross-site form action refused before credential entry")
            if action.secret:
                if not authenticating or control['tag'] not in ("input", "textarea"):
                    raise ValueError("Credential fill outside authentication refused")
                value = getattr(self.site, action.secret).get_secret_value()
            else:
                value = action.value
            if action.operation == "click":
                href = control.get('href')
                if href and not self.allowed(href) and not href.startswith(('#', 'javascript:')):
                    raise ValueError("Cross-site or unsupported link refused")
                # A popup covering the target makes Playwright wait; fail fast so the model can close it.
                await locator.click(timeout=min(self.action_timeout, 4000))
            elif action.operation == "fill":
                await locator.fill(value, timeout=self.action_timeout)
            elif action.operation == "select":
                await locator.select_option(value=value, timeout=self.action_timeout)
            elif action.operation == "press":
                await locator.press(value, timeout=self.action_timeout)
            elif action.operation == "set":
                await self.set_control(locator, control, value)
        await asyncio.sleep(.15)
        if self.page.url not in ("about:blank", "") and not self.allowed(self.page.url):
            raise ValueError("Navigation left assigned site")
        # A model must select a newly opened tab before its content is read.
        for page in self.page.context.pages:
            self._listen(page)

async def observe(page) -> str:
    return await page.locator("body").inner_text()
