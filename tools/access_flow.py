"""Small UI-only helpers for unambiguous login forms and access notices."""
import re
from urllib.parse import urlsplit
from tools.navigation import BrowserAction


def controls_in(observation):
    return [control for frame in observation.get('frames', []) for control in frame.get('controls', [])]


def access_action(observation):
    controls = [c for c in controls_in(observation) if not c.get('disabled') and not c.get('readonly')]
    text = '\n'.join(frame.get('text','') for frame in observation.get('frames', []))
    for control in controls:
        label = control.get('label', '')
        if re.search(r'notwendige.*cookies.*akzeptieren|accept.*necessary.*cookies', label, re.I):
            return BrowserAction(operation='click', target=control['target'])
        if control.get('in_dialog') and re.fullmatch(r'Schließen|Close|Fechar|×', label.strip(), re.I):
            return BrowserAction(operation='click', target=control['target'])
        if re.search(r'vorübergehend nicht verfügbar|temporarily unavailable', text, re.I) and re.fullmatch(
                r'Erneut versuchen|Try again|Retry', label.strip(), re.I):
            return BrowserAction(operation='click', target=control['target'])
    for control in controls:
        if not re.search(r'entsperr|unlock|roboter|robot', control.get('label', ''), re.I):
            continue
        if control.get('type') == 'range' and control.get('range_value') != control.get('max'):
            return BrowserAction(operation='fill', target=control['target'], value=control.get('max') or '100')
        if (control.get('type') == 'checkbox' or control.get('role') == 'checkbox') and not control.get('checked'):
            return BrowserAction(operation='click', target=control['target'])
    # This gate occurs only after the model selected a document. It requests
    # permission to read that document, without selecting a revision or business facts.
    if (urlsplit(observation.get('url','')).path.startswith('/documents/') and
            'Dokumentenüberlassung' in text and 'Leseberechtigung' in text):
        receipts=[c for c in controls if c.get('tag')=='input' and
                  (c.get('label','').strip()=='Belegnummer übertragen' or
                   (not c.get('label','').strip() and c.get('name')=='receipt'))]
        code=re.search(r'Belegnummer\s*:\s*([\w-]+)',text)
        if len(receipts)==1 and code:
            receipt=receipts[0]
            peers=[c for c in controls if c['target'].split(':')[0]==receipt['target'].split(':')[0]
                   and c.get('form')==receipt.get('form')]
            purposes=[c for c in peers if c.get('type')=='radio' and
                      c.get('label','').strip()=='Verbindliche Unterlagen zur Kenntnis genommen']
            acknowledgements=[c for c in peers if c.get('type')=='checkbox' and
                              c.get('label','').strip()=='Überlassungsbedingungen gelesen']
            submits=[c for c in peers if c.get('type')=='submit' and
                     c.get('label','').strip()=='Leseberechtigung anfordern']
            if len(purposes)==len(acknowledgements)==len(submits)==1:
                if not purposes[0].get('checked'):
                    return BrowserAction(operation='click',target=purposes[0]['target'])
                if not receipt.get('filled'):
                    return BrowserAction(operation='fill',target=receipt['target'],value=code.group(1))
                if not acknowledgements[0].get('checked'):
                    return BrowserAction(operation='click',target=acknowledgements[0]['target'])
                return BrowserAction(operation='click',target=submits[0]['target'])
    # A read-only access acknowledgement, with a code explicitly shown in the UI.
    receipts = [c for c in controls if c.get('tag') == 'input' and re.fullmatch(
        r'Empfangsnummer|Receipt number',c.get('label','').strip(),re.I)]
    receipt_code = re.search(r'(?:Empfangsnummer|Receipt number)\s*:\s*([\w-]+)', text, re.I)
    if len(receipts) == 1 and receipt_code and re.search(r'Nur Anzeigezugriff|read.only access',text,re.I):
        receipt = receipts[0]
        if not receipt.get('filled'):
            return BrowserAction(operation='fill',target=receipt['target'],value=receipt_code.group(1))
        acknowledgements = [c for c in controls if c.get('form') == receipt.get('form') and
            c.get('type') == 'checkbox' and c.get('label','').strip() == 'Betriebsbedingungen gelesen']
        if len(acknowledgements) == 1:
            if not acknowledgements[0].get('checked'):
                return BrowserAction(operation='click',target=acknowledgements[0]['target'])
            submits = [c for c in controls if c.get('form') == receipt.get('form') and c.get('type') == 'submit'
                       and c.get('label','').strip() in ('Weiter','Continue')]
            if len(submits) == 1:
                return BrowserAction(operation='click',target=submits[0]['target'])
    return None


def login_actions(observation):
    """Only batch a uniquely identified username/password/submit form."""
    controls = [c for c in controls_in(observation) if not c.get('disabled') and not c.get('readonly')]
    passwords = [c for c in controls if c.get('type') == 'password']
    if len(passwords) != 1:
        return []
    password = passwords[0]
    frame = password['target'].split(':')[0]
    same_form = [c for c in controls if c['target'].startswith(frame + ':') and c.get('form') == password.get('form')]
    usernames = [c for c in same_form if c.get('tag') == 'input' and c.get('type') != 'password' and (
        c.get('autocomplete') == 'username' or re.search(
            r'benutzer|username|user.?name|login|e.?mail', c.get('label', '') + ' ' + (c.get('name') or ''), re.I))]
    submits = [c for c in same_form if c.get('tag') in ('button','input') and (
        c.get('type') == 'submit' or re.fullmatch(r'Anmelden|Login|Log in|Sign in|Einloggen|Entrar', c.get('label','').strip(), re.I))]
    if len(usernames) != 1 or len(submits) != 1:
        return []
    return [BrowserAction(operation='fill', target=usernames[0]['target'], secret='username'),
            BrowserAction(operation='fill', target=password['target'], secret='password'),
            BrowserAction(operation='click', target=submits[0]['target'])]


def unresolved_access(observation):
    for control in controls_in(observation):
        if control.get('type') == 'password':
            return True
        label = control.get('label', '')
        if re.search(r'empfangsnummer|receipt.*number', label, re.I) and control.get('tag') == 'input':
            return True
        if re.search(r'roboter|robot', label, re.I) and (
                control.get('type') == 'checkbox' or control.get('role') == 'checkbox') and not control.get('checked'):
            return True
    return False


def business_access_ready(observation, role):
    """Logout plus a role's visible business tools; a notice alone is insufficient."""
    if unresolved_access(observation) or access_action(observation):
        return False
    controls = [c for c in controls_in(observation) if not c.get('disabled')]
    has_logout = any(re.fullmatch(r'Abmelden|Log out|Logout|Sign out',c.get('label','').strip(),re.I) for c in controls)
    capabilities = {'tender':r'Suchen|Search', 'supplier':r'Dokumente|Documents|Spezifikationen',
                    'erp':r'Transaktionscode|Transaktionen|Transactions', 'mail':r'Suchbegriff|Nachrichten durchsuchen|Search'}
    return has_logout and any(re.fullmatch(capabilities[role],c.get('label','').strip(),re.I) for c in controls)
