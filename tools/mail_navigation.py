"""Mailbox-only DOM adapter; other website navigators are unchanged."""
import asyncio
import re
from urllib.parse import urlsplit, urljoin, parse_qs
from tools.navigation import BrowserAction

# Generic DOM inspection, constant code supplied by the application rather than an LLM.
CONTROL_SCRIPT = """elements => elements.map((e, index) => ({
 index, tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
 type: e.getAttribute('type'), name: e.getAttribute('name'), disabled: !!e.disabled,
 readonly: !!e.readOnly, autocomplete: e.getAttribute('autocomplete'),
 form: e.form ? Array.from(document.forms).indexOf(e.form) : null,
 in_dialog: !!e.closest('[role=dialog],dialog'),
 min: e.type === 'range' ? (e.min || '0') : null,
 max: e.type === 'range' ? (e.max || '100') : null,
 range_value: e.type === 'range' ? e.value : null,
 checked: 'checked' in e ? e.checked : e.getAttribute('aria-checked') === 'true',
 filled: ('value' in e) ? !!e.value : null,
 label: (e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.getAttribute('placeholder') ||
         e.innerText || e.getAttribute('title') ||
 Array.from(e.querySelectorAll('[aria-label],img[alt],svg title')).map(n => n.getAttribute('aria-label') || n.getAttribute('alt') || n.textContent || '').join(' ') || '').trim().slice(0, 160),
 href: e.href || e.src || e.data || null,
 options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => ({label:o.text, value:o.value})) : null,
 visible: !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden' && !e.closest('[hidden],[aria-hidden=true],[inert]')
})).filter(e => e.visible)"""
CONTROLS = "a,button,summary,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],[role=checkbox],[contenteditable=true],embed,object"

class MailNavigator:
    def __init__(self, page, site, secrets=(), action_timeout=10000, text_limit=12000):
        self.page = page
        self.site = site
        self.secrets = list(secrets)
        self.action_timeout = action_timeout
        self.text_limit = text_limit
        self.targets = {}
        self.links = set()
        self.mailbox_return_url = None
        self.downloads = []
        self.listened = set()
        self.document_cache = {}
        self._listen(page)

    def _listen(self, page):
        if id(page) not in self.listened:
            page.on("download", lambda download: self.downloads.append(download))
            # Generic modal acknowledgement; never accept confirmation automatically.
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
            self.listened.add(id(page))

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
        await self.page.route("**/*", restrict_network)
        await self.page.goto(self.site.url, wait_until="domcontentloaded", timeout=self.action_timeout)

    async def observe(self, offset=0):
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
                ancestor = frame
                visible = True
                while ancestor.parent_frame is not None:
                    if not await (await ancestor.frame_element()).is_visible():
                        visible = False
                        break
                    ancestor = ancestor.parent_frame
                if not visible:
                    continue
                text = await frame.locator("body").inner_text(timeout=self.action_timeout)
                labels = await frame.locator('[aria-label],svg title').evaluate_all(
                    "es => es.filter(e=>e.getClientRects().length || (e.tagName.toLowerCase()==='title' && e.closest('svg')?.getClientRects().length)).map(e=>e.getAttribute('aria-label') || e.textContent).filter(Boolean).join('\\n')")
                text += '\nAccessible labels:\n' + labels
                rows = await frame.locator('table tr').evaluate_all(
                    "rs => rs.filter(r=>r.getClientRects().length && !r.closest('[hidden],[aria-hidden=true]')).map(r=>Array.from(r.cells).map(c=>c.innerText+' '+Array.from(c.querySelectorAll('[aria-label],svg title')).map(e=>e.getAttribute('aria-label') || e.textContent).join(' ')).join(' | ')).join('\\n')")
                text += '\nDisplayed table rows:\n' + rows
                controls = await frame.locator(CONTROLS).evaluate_all(CONTROL_SCRIPT)
                entries = []
                for control in controls[:remaining_controls]:
                    target = f"f{frame_number}:e{control['index']}"
                    self.targets[target] = (frame, control['index'], control)
                    if control.get('href') and self.allowed(control['href']):
                        self.links.add(control['href'])
                    entries.append({"target": target, **{k:v for k,v in control.items() if k not in ('index','visible')}})
                remaining_controls -= len(entries)
                frames.append({"frame": frame_number, "url": frame.url,
                    "text": redact(text[offset:offset+remaining_chars], self.secrets),
                    "offset": offset, "total_characters": len(text), "controls": entries})
                remaining_chars = max(0, remaining_chars - len(frames[-1]["text"]))
            except Exception as error:
                frames.append({"frame": frame_number, "error_type": type(error).__name__})
        search_visible=any(re.search(r'Suchbegriff|Search',c.get('label',''),re.I)
                           for f in frames for c in f.get('controls',[]))
        parsed=urlsplit(self.page.url)
        if (search_visible and self.allowed(self.page.url)
                and not parsed.path.startswith('/messages/')
                and not parse_qs(parsed.query).get('message')):
            self.mailbox_return_url=self.page.url
        tabs = [{"tab": i, "url": page.url} for i,page in enumerate(self.page.context.pages) if self.allowed(page.url)]
        observation = {"url": self.page.url, "frames": frames, "tabs": tabs,
                       "mailbox_return_url": self.mailbox_return_url,
                       "downloads": [{"index": i, "name": d.suggested_filename} for i,d in enumerate(self.downloads)]}
        # Labels/URLs can contain account values too; redact the entire observation representation.
        import json
        return json.loads(redact(json.dumps(observation, ensure_ascii=False), self.secrets))

    async def act(self, action: BrowserAction, *, authenticating=False):
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
                result = read_document(await response.body(), urlsplit(action.url).path, action.member)
                self.document_cache[cache_key] = result
                return dict(result)
            finally:
                await response.dispose()
        if action.operation == "goto":
            url = urljoin(self.page.url, action.url)
            if not self.allowed(url) or (url not in self.links and url != self.mailbox_return_url):
                raise ValueError("Only observed same-site links are navigable")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.action_timeout)
        elif action.operation == "switch_tab":
            pages = self.page.context.pages
            if action.tab >= len(pages) or not self.allowed(pages[action.tab].url):
                raise ValueError("Tab is outside assigned site")
            self.page = pages[action.tab]
            self._listen(self.page)
        elif action.operation == "back":
            await self.page.go_back(wait_until="domcontentloaded", timeout=self.action_timeout)
            if not self.allowed(self.page.url):
                raise ValueError("Back navigation left assigned site")
        elif action.operation == "scroll":
            await self.page.mouse.wheel(0, 600)
        else:
            if action.target not in self.targets:
                raise ValueError("Target not present in the current observation")
            frame, index, control = self.targets[action.target]
            if action.operation in ('fill','select','press') and (control.get('readonly') or control.get('disabled')):
                raise ValueError('Target is read-only or disabled; use an observed link or mailbox_return_url')
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
            if action.operation in ('fill','select','press') and (current.get('readonly') or current.get('disabled')):
                raise ValueError('Target became read-only or disabled; observe again')
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
                if re.fullmatch(r'Abmelden|Logout|Log out|Sign out', control.get('label',''), re.I):
                    raise ValueError('Keep the mailbox session authenticated')
                href = control.get('href')
                if href and not self.allowed(href) and not href.startswith(('#', 'javascript:')):
                    raise ValueError("Cross-site or unsupported link refused")
                await locator.click(timeout=self.action_timeout)
            elif action.operation == "fill":
                if control.get('type') == 'range':
                    if value != control.get('max'):
                        raise ValueError('Unlock slider requires its observed maximum')
                    await locator.press('End', timeout=self.action_timeout)
                else:
                    await locator.fill(value, timeout=self.action_timeout)
            elif action.operation == "select":
                await locator.select_option(value=value, timeout=self.action_timeout)
            elif action.operation == "press":
                await locator.press(value, timeout=self.action_timeout)
        await asyncio.sleep(.15)
        if self.page.url not in ("about:blank", "") and not self.allowed(self.page.url):
            raise ValueError("Navigation left assigned site")
        # A model must select a newly opened tab before its content is read.
        for page in self.page.context.pages:
            self._listen(page)

async def observe(page) -> str:
    return await page.locator("body").inner_text()
