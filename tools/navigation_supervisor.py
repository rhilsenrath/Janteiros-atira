"""Deterministic navigation supervision; destinations and document choices stay model-directed."""
import asyncio
import hashlib
import json
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import run as run_contract


class NavigationBlocked(RuntimeError):
    pass


class NavigationDeadline(NavigationBlocked):
    pass


class AuthenticationFailure(RuntimeError):
    pass


class ActionRejected(ValueError):
    pass


class MissingRecord(RuntimeError):
    pass


AUTH_ERROR = re.compile(r'invalid credentials|incorrect (?:password|credentials)|wrong password|'
                        r'anmeldung fehlgeschlagen|ungültige anmelde|benutzername oder passwort.*(?:falsch|ungültig)|'
                        r'passwort.*(?:falsch|ungültig)|account locked|konto gesperrt', re.I)
EXPIRED = re.compile(r'session (?:has )?expired|session timeout|sitzung.*(?:abgelaufen|beendet)|'
                     r'erneut anmelden|please (?:log|sign) in again', re.I)
LOGIN = re.compile(r'anmelden|einloggen|login|log in|sign in', re.I)
MISSING = re.compile(r'no (?:matching )?(?:records?|results?)|keine (?:treffer|ergebnisse|datensätze)|'
                     r'nichts gefunden|material.*nicht gefunden', re.I)
ERROR_VIEW = re.compile(r'access denied|permission denied|zugriff verweigert|nicht berechtigt|'
                        r'internal server error|temporarily unavailable|vorübergehend nicht verfügbar', re.I)
VOLATILE = re.compile(r'\b(?:feedback|bewertung|umfrage|survey|wartungshinweis|maintenance notice|tour|tipp)\b', re.I)
CLOCK = re.compile(r'\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b')
TARGET_OPERATIONS = {'click', 'fill', 'select', 'press', 'set', 'reveal'}


def controls(observation):
    return [c for f in observation.get('frames', []) for c in f.get('controls', [])]


def text(observation):
    return '\n'.join(f.get('text', '') for f in observation.get('frames', []))


def session_expired(observation):
    fields = controls(observation)
    # A password change form alone is not an unexpected login screen.
    login_form = any(c.get('type') == 'password' for c in fields) and any(
        LOGIN.search(c.get('label') or '') for c in fields)
    return bool(login_form or (EXPIRED.search(text(observation)) and any(
        LOGIN.search(c.get('label') or '') for c in fields)) or
        (observation.get('http_status') in (401, 403) and any(LOGIN.search(c.get('label') or '') for c in fields)))


def identity(control):
    return tuple(control.get(k) for k in ('tag', 'role', 'type', 'name', 'label', 'href', 'row_key'))


def stable_url(url):
    if not url:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.casefold() not in
             ('session', 'sessionid', 'sid', 'csrf', 'nonce', 'timestamp', 'ts', '_', 'cachebuster')]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def normalized_state(observation):
    """Order, clocks and disposable notices cannot manufacture progress; business dialogs can."""
    frames = []
    for f in observation.get('frames', []):
        notices = {line.strip() for line in (f.get('overlay_text') or '').splitlines()
                   if VOLATILE.search(f.get('overlay_text') or '')}
        lines = sorted({CLOCK.sub('[clock]', ' '.join(line.split())) for line in f.get('text', '').splitlines()
                        if line.strip() and line.strip() not in notices})
        fields = []
        for c in f.get('controls', []):
            if c.get('in_overlay') and VOLATILE.search(c.get('label') or ''):
                continue
            fields.append((c.get('tag'), c.get('role'), c.get('type'), CLOCK.sub('[clock]', c.get('label') or ''),
                           stable_url(c.get('href')), c.get('row_key'), c.get('checked'), c.get('expanded'), c.get('disabled'),
                           c.get('readonly'), c.get('filled'),
                           None if c.get('type') == 'password' else c.get('value')))
        frames.append((lines, sorted(fields, key=repr), sorted(f.get('dom_text') or [], key=repr),
                       sorted(f.get('image_text') or [])))
    # URL changes alone, row order and tab reordering are not evidence of successful navigation.
    return digest((frames, sorted(d.get('name', '') for d in observation.get('downloads', []))))


@dataclass
class Checkpoint:
    objective: str = ''
    stage: str = 'authentication'
    url: str | None = None
    state: str | None = None
    completed: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)


class NavigationSupervisor:
    def __init__(self, agent_id, config, logger=None):
        self.agent_id, self.config, self.logger = agent_id, config, logger
        self.checkpoint = Checkpoint()
        self.recent = deque(maxlen=16)
        self.seen = set()
        self.failures = Counter()
        self.action_contexts = {}
        self.documents = set()
        self.evidence_facts = set()
        self.stagnant = 0
        self.recoveries = 0
        self.sessions = 0
        self.actions = 0
        self.last_observation = None
        self.last_outcome = None
        self.deadline = float('inf')
        self.recovery_required = False
        self.pending_evidence = False
        self.phase = 'authentication'
        self.resume_required = False
        self.resume_actions = 0

    def emit(self, event, **kwargs):
        if self.logger:
            self.logger.emit(event, agent=self.agent_id, **kwargs)

    def start(self, runtime, objective, remaining=()):
        self.deadline = time.monotonic() + min(self.config.task_timeout,
            max(0, run_contract.seconds_left(runtime) - self.config.deadline_reserve))
        self.checkpoint.objective = objective
        self.checkpoint.remaining = list(remaining)
        self.check_budget()

    def check_budget(self):
        if time.monotonic() >= self.deadline:
            raise NavigationDeadline('Navigation deadline reached; submission reserve preserved')
        if self.actions > self.config.max_supervised_actions:
            raise NavigationBlocked('Navigation action budget exhausted')
        if self.stagnant >= self.config.navigation_no_progress_limit:
            raise NavigationBlocked('Navigation did not make verified progress within its recovery budget')

    def timeout(self, limit):
        self.check_budget()
        return max(.001, min(limit, self.deadline - time.monotonic()))

    async def observe(self, navigator, offset=0):
        async with asyncio.timeout(self.timeout(self.config.action_timeout_ms / 1000)):
            observation = await navigator.observe(offset)
        self.last_observation = observation
        if not any(f.get('text', '').strip() or f.get('controls') for f in observation.get('frames', [])):
            if any('Timeout' in f.get('error_type', '') for f in observation.get('frames', [])):
                raise TimeoutError('Live page observation timed out')
            raise ActionRejected('No readable live page state')
        state = normalized_state(observation)
        self.seen.add(state)
        if self.checkpoint.stage != 'authentication' and self.checkpoint.state is None and not session_expired(observation) and (observation.get('http_status') or 0) < 400:
            self.save(observation, 'discovery')
        return observation

    def save(self, observation, stage):
        self.checkpoint.url = observation.get('url')
        self.checkpoint.state = normalized_state(observation)
        self.checkpoint.stage = stage
        self.checkpoint.snapshot = {'frames': [{'text': f.get('text', '')[:1500],
            'controls': [{k: c.get(k) for k in ('tag', 'label', 'row_key', 'checked', 'value')}
                         for c in f.get('controls', []) if c.get('type') != 'password'][:20]}
            for f in observation.get('frames', [])][:4]}
        self.emit('navigation_checkpoint', operation=stage, count=len(self.checkpoint.evidence))

    def signature(self, action, observation):
        c = next((c for c in controls(observation) if c.get('target') == action.target), {})
        signature = digest((action.operation, c.get('tag'), c.get('role'), c.get('type'),
                       CLOCK.sub('[clock]', c.get('label') or ''), stable_url(c.get('href')), c.get('row_key'),
                       stable_url(action.url), action.tab, action.offset,
                       # Values stay internal hashed identities, never logs or failure text.
                       action.value, action.secret, action.download, action.member,
                       sorted(action.source_ids) if action.operation == 'finish' else []))
        self.action_contexts[signature] = {'operation': action.operation, 'label': c.get('label'),
                                           'row_key': c.get('row_key'), 'url': action.url or c.get('href')}
        return signature

    def failure(self, error, *, extracting=False, authenticating=False):
        category = self.classify(error, extracting=extracting, authenticating=authenticating)
        self.record('extract' if extracting else 'observe_or_decide', None, category, False)
        return category

    def validate(self, action, observation, *, authenticating=False):
        self.check_budget()
        if self.actions >= self.config.max_supervised_actions:
            raise NavigationBlocked('Navigation action budget exhausted')
        if action.secret and not authenticating:
            raise ActionRejected('Credentials are restricted to authentication')
        c = next((c for c in controls(observation) if c.get('target') == action.target), None)
        if action.operation in TARGET_OPERATIONS:
            if c is None or c.get('visible') is False or c.get('disabled') or c.get('aria_disabled'):
                raise ActionRejected('Choose an enabled target from the latest live observation')
            if action.operation in ('fill', 'set') and c.get('readonly'):
                raise ActionRejected('Target is read-only')
            if action.operation == 'fill' and (c.get('tag') not in ('input', 'textarea') and not c.get('editable')):
                raise ActionRejected('Fill requires an observed editable field')
        signature = self.signature(action, observation)
        if self.failures[signature] >= 2:
            raise ActionRejected('This action repeatedly failed; choose a different live control or route')
        if not authenticating and self.recovery_required and action.operation not in ('goto', 'switch_tab', 'back', 'reload', 'read_document', 'inspect', 'finish'):
            raise ActionRejected('Recovery requires a different route, checkpoint or evidence inspection')
        return signature

    def rejected(self, action, observation, category='blocked_navigation'):
        signature = self.signature(action, observation)
        self.failures[signature] += 1
        self.record(action.operation, signature, category, False)

    def record(self, operation, signature, category, progress):
        self.last_outcome = category
        self.recent.append({'operation': operation, 'outcome': category, 'progress': progress})
        if progress:
            self.stagnant = 0
            self.recovery_required = False
            self.checkpoint.completed = [*self.checkpoint.completed, {'operation': operation, 'outcome': category}][-12:]
        else:
            self.stagnant += 1
            if self.stagnant == 2:
                self.emit('navigation_strategy_change', operation='alternative_route')
            if self.stagnant >= 4 and not self.recovery_required:
                self.recoveries += 1
                if self.recoveries > self.config.navigation_recovery_attempts:
                    raise NavigationBlocked('Navigation recovery attempts exhausted')
                self.recovery_required = True
                self.emit('navigation_recovery', operation='checkpoint')
        self.emit('navigation_verified' if progress else 'navigation_failure', operation=operation,
                  status=category, count=self.stagnant)
        self.check_budget()

    @staticmethod
    def classify(error, *, authenticating=False, extracting=False):
        if isinstance(error, AuthenticationFailure):
            return 'authentication_failure'
        if isinstance(error, NavigationDeadline):
            return 'deadline'
        if isinstance(error, TimeoutError) or 'Timeout' in type(error).__name__:
            return 'timeout'
        if isinstance(error, MissingRecord):
            return 'missing_records'
        if extracting:
            return 'extraction_error'
        if authenticating:
            return 'authentication_failure'
        return 'blocked_navigation'

    @staticmethod
    def expected_effect(action, before, after):
        chosen = next((c for c in controls(before) if c.get('target') == action.target), None)
        form_change = action.operation in ('fill', 'select', 'set')
        check_click = action.operation == 'click' and chosen and chosen.get('checked') is not None
        if not form_change and not check_click:
            return True
        matches = [c for c in controls(after) if chosen and identity(c) == identity(chosen)]
        if len(matches) != 1:
            return False
        current = matches[0]
        if action.secret:
            return current.get('filled') is True
        if check_click:
            return current.get('checked') is not chosen.get('checked') and current.get('checked') is not None
        if action.operation == 'set' and current.get('checked') is not None:
            expected = str(action.value).casefold() in ('true', '1', 'yes', 'ja', 'on', 'checked')
            return current['checked'] == expected
        expected = str(action.value)
        if action.operation == 'select' or chosen.get('tag') == 'select':
            labels = {str(o['label']) for o in chosen.get('options') or [] if str(o.get('value')) == expected}
            return str(current.get('value')) in {expected, *labels}
        return str(current.get('value')) == expected

    async def perform(self, navigator, action, observation, *, authenticating=False):
        try:
            signature = self.validate(action, observation, authenticating=authenticating)
        except ActionRejected:
            self.rejected(action, observation)
            raise
        before = normalized_state(observation)
        self.seen.add(before)
        known = set(self.seen)
        self.actions += 1
        self.emit('navigation_decision', operation=action.operation, count=self.actions)
        result = None
        try:
            async with asyncio.timeout(self.timeout(self.config.action_timeout_ms / 1000)):
                result = await navigator.act(action, authenticating=authenticating)
            # Every action is checked immediately, including access controls, tab changes and document reads.
            after = await self.observe(navigator, action.offset if action.operation == 'inspect' else 0)
            state = normalized_state(after)
            # observe() registers the state; novelty must be checked against the states before this action.
            novel = state not in known
            if state == before and action.operation not in ('inspect', 'finish', 'read_document'):
                await asyncio.sleep(min(.2, self.timeout(.2)))
                after = await self.observe(navigator)
                state = normalized_state(after)
                novel = state not in known
            category = 'progress' if novel else 'no_effect'
            progress = novel
            if self.recovery_required and before != self.checkpoint.state and state == self.checkpoint.state and \
                    action.operation in ('goto', 'back', 'switch_tab'):
                progress, category = True, 'checkpoint_recovered'
            if session_expired(after):
                category = 'authentication_pending' if authenticating else 'session_expired'
                progress = novel if authenticating else False
            elif (after.get('http_status') or 0) >= 400:
                category, progress = ('missing_records' if after.get('http_status') == 404 else 'blocked_navigation'), False
            elif ERROR_VIEW.search(text(after)):
                category, progress = 'blocked_navigation', False
            elif MISSING.search(text(after)):
                category = 'missing_records'
            if category in ('progress', 'no_effect', 'authentication_pending') and not self.expected_effect(action, observation, after):
                category, progress = 'blocked_navigation', False
            if authenticating and AUTH_ERROR.search(text(after)):
                raise AuthenticationFailure('Website rejected runtime authentication')
            if result and (result.get('members') or (result.get('text') and not re.search(
                    r'not clickable|nothing new|stopped:', result['text'], re.I))):
                key = digest(result)
                if key not in self.documents:
                    self.documents.add(key)
                    progress, category = True, 'document_observed'
            if action.operation == 'inspect':
                progress = self.pending_evidence or novel
                category = 'evidence' if self.pending_evidence else ('progress' if novel else 'no_effect')
                self.pending_evidence = False
            if not progress:
                self.failures[signature] += 1
            self.record(action.operation, signature, category, progress)
            self.recent[-1]['state'] = state
            if self.resume_required and not authenticating:
                self.resume_actions += 1
                self.verify_resume(after)
                if self.resume_required and self.resume_actions >= self.config.checkpoint_recovery_steps:
                    raise NavigationBlocked('Authenticated session did not restore its verified checkpoint')
            if progress and not authenticating and category != 'missing_records' and not self.resume_required:
                self.save(after, action.operation)
            return result
        except AuthenticationFailure:
            self.emit('navigation_failure', operation=action.operation, status='authentication_failure')
            raise
        except NavigationBlocked:
            raise
        except Exception as error:
            # An exception may occur after a side effect: take a fresh observation before further decisions.
            try:
                await self.observe(navigator)
            except Exception:
                pass
            self.failures[signature] += 1
            self.record(action.operation, signature, self.classify(error, authenticating=authenticating), False)
            raise

    def evidence(self, result):
        facts = {digest((e.source_url, e.reference, e.revision, e.field, e.excerpt)) for e in result.evidence}
        added = facts - self.evidence_facts
        self.evidence_facts.update(facts)
        self.checkpoint.evidence = list(dict.fromkeys([*self.checkpoint.evidence, *(e.id for e in result.evidence)]))
        self.checkpoint.remaining = [m.question for m in result.missing]
        if added:
            self.pending_evidence = True
            self.stagnant = 0
            self.recovery_required = False
            self.emit('navigation_evidence', count=len(added))
        return bool(added)

    def session_recovery(self):
        self.sessions += 1
        self.emit('navigation_recovery', operation='reauthenticate', count=self.sessions)
        if self.sessions > self.config.session_recovery_attempts:
            raise NavigationBlocked('Repeated session expiry exhausted the recovery budget')
        self.resume_required, self.resume_actions = True, 0

    def verify_resume(self, observation):
        if self.resume_required and not session_expired(observation) and normalized_state(observation) == self.checkpoint.state \
                and stable_url(observation.get('url')) == stable_url(self.checkpoint.url):
            self.resume_required = False
            self.emit('navigation_recovery_verified', operation='checkpoint')
        return not self.resume_required

    def context(self):
        return {'objective': self.checkpoint.objective, 'phase': self.phase, 'stage': self.checkpoint.stage,
                'last_verified_url': self.checkpoint.url, 'last_verified_state': self.checkpoint.state,
                'last_verified_page': self.checkpoint.snapshot, 'resume_checkpoint_required': self.resume_required,
                'completed_steps': self.checkpoint.completed, 'evidence_ids': self.checkpoint.evidence[-30:],
                'remaining_information': self.checkpoint.remaining, 'recent_outcomes': list(self.recent),
                'avoid_actions': [self.action_contexts[key] for key, count in self.failures.items() if count >= 2],
                'stagnant_steps': self.stagnant, 'recovery_required': self.recovery_required,
                'instruction': ('Restore the last verified page and objective using observed links/controls before new investigation.'
                                if self.resume_required else
                                'Choose a different observed route or resume the verified checkpoint. Do not repeat failed controls.'
                                if self.stagnant >= 2 else 'Verify the next action against the resulting live state.')}


class SupervisedNavigator:
    """Keep the BrowserNavigator interface, including agent-specific helpers and assigned page state."""
    def __init__(self, navigator, supervisor):
        object.__setattr__(self, '_navigator', navigator)
        object.__setattr__(self, 'supervisor', supervisor)
        navigator.supervised_action = self.act

    def __getattr__(self, name):
        return getattr(self._navigator, name)

    def __setattr__(self, name, value):
        if name in ('_navigator', 'supervisor'):
            object.__setattr__(self, name, value)
        else:
            setattr(self._navigator, name, value)

    async def initialize(self):
        async with asyncio.timeout(self.supervisor.timeout(self.supervisor.config.task_timeout)):
            return await self._navigator.initialize()

    async def observe(self, offset=0):
        return await self.supervisor.observe(self._navigator, offset)

    async def act(self, action, *, authenticating=False):
        before = self.supervisor.last_observation or await self.observe()
        return await self.supervisor.perform(self._navigator, action, before, authenticating=authenticating)
