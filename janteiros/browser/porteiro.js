// Porteiro scan: finds obstacles in one frame and tags the controls that clear them (data-porteiro).
// It never decides where to go: it only reports login forms, cookie walls, error pages,
// open dialogs/overlays and slider+checkbox gates, so the Python side can clear them.
() => {
  const ZW = /[​-‏⁠﻿]/g;
  const out = { login: null, consent: null, error: null, dialogs: [], gates: [], blank: false };
  let k = 0;
  for (const el of document.querySelectorAll("[data-porteiro]")) el.removeAttribute("data-porteiro");
  const tag = (el) => { const id = "p" + (k++); el.setAttribute("data-porteiro", id); return id; };
  const vis = (el) => !!(el && el.getClientRects().length) && getComputedStyle(el).visibility !== "hidden";
  const txt = (el) => ((el && (el.innerText || el.textContent)) || "").replace(ZW, "").replace(/\s+/g, " ").trim();
  const label = (b) => (txt(b) || b.value || b.getAttribute("aria-label") || "").trim();
  if (!document.body) return out;

  // login form
  const pw = Array.from(document.querySelectorAll("input[type=password]")).find(vis);
  if (pw) {
    const form = pw.form || pw.closest("form") || document;
    const user = Array.from(form.querySelectorAll("input:not([type]),input[type=text],input[type=email]")).find(vis);
    const buttons = Array.from(form.querySelectorAll("button,input[type=submit]")).filter(vis);
    const btn = buttons.find((b) => /anmeld|login|log in|einlogg|sign in|weiter/i.test(label(b))) || buttons.find((b) => (b.type || "submit") === "submit");
    out.login = { user: user ? tag(user) : null, pass: tag(pw), submit: btn ? tag(btn) : null };
    return out;
  }

  // error page (service unavailable and friends)
  const head = txt(document.querySelector("h1")) + " · " + (document.title || "");
  if (/nicht verfügbar|vorübergehend|temporarily|unavailable|internal server error|bad gateway|gateway time|fehler 5\d\d|error 5\d\d|zeitüberschreitung|überlastet|wartungsarbeiten/i.test(head)) {
    const retry = Array.from(document.querySelectorAll("a,button")).find((b) => vis(b) && /erneut|wiederholen|retry|neu laden|nochmal|try again|aktualisieren/i.test(label(b)));
    out.error = { retry: retry ? tag(retry) : null, text: head.slice(0, 140) };
    return out;
  }
  if (!txt(document.body)) out.blank = true;

  // cookie wall or banner: an accept-like button whose own text or nearby container mentions cookies
  const accepting = Array.from(document.querySelectorAll("button,input[type=submit],a[role=button]")).filter((b) => vis(b) && /akzeptier|zustimm|einverstanden|annehmen|accept|agree|erlauben|allow/i.test(label(b)));
  const aboutCookies = (b) => {
    if (/cookie/i.test(label(b))) return true;
    let c = b.parentElement;
    for (let i = 0; c && i < 3; i++, c = c.parentElement) {
      const t = txt(c);
      if (t.length > 1500) break;
      if (/cookie/i.test(t)) return true;
    }
    return false;
  };
  const consent = accepting.filter(aboutCookies);
  if (consent.length) {
    const pick = consent.find((b) => /notwendig|erforderlich|necessary|essential|essenziell|nur /i.test(label(b))) || consent.find((b) => !/\balle\b|\ball\b/i.test(label(b))) || consent[0];
    out.consent = { id: tag(pick), text: label(pick).slice(0, 80) };
  }

  // open dialogs and full-screen overlays
  const closeRe = /schlie|close|^×$|^x$|^✕$|später|nicht jetzt|ausblenden|dismiss|^ok$|verstanden/i;
  const trapRe = /akzeptier|fortfahren|weiter|accept|teilnehmen|umfrage starten/i;
  const findClose = (root) => Array.from(root.querySelectorAll("button,a,[role=button]")).find((b) => {
    const t = ((b.getAttribute("aria-label") || "") + " " + txt(b)).trim();
    return closeRe.test(t) && !trapRe.test(txt(b));
  });
  for (const d of document.querySelectorAll("dialog[open]")) {
    const close = findClose(d);
    out.dialogs.push({ id: tag(d), close: close ? tag(close) : null, modal: d.matches(":modal"), label: (d.getAttribute("aria-label") || txt(d)).slice(0, 80), countdown: /sekunde|seconds|schließt sich|closes automatically|countdown/i.test(txt(d)) });
  }
  const vw = innerWidth, vh = innerHeight;
  for (const el of document.querySelectorAll("body *")) {
    if (el.tagName === "DIALOG") continue;
    const cs = getComputedStyle(el);
    if (cs.position !== "fixed" || cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    const area = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (area < 0.35 * vw * vh) continue;
    const close = findClose(el);
    out.dialogs.push({ id: tag(el), close: close ? tag(close) : null, modal: true, overlay: true, label: (el.getAttribute("aria-label") || txt(el)).slice(0, 80), countdown: /sekunde|seconds|schließt sich/i.test(txt(el)) });
  }

  // receipt forms: copy a displayed receipt/confirmation number into its field (forms with choices are left to the model)
  const codeRe = /(beleg|empfangs|quittungs|best[äa]tigungs|freigabe|zugangs|receipt|confirmation)\s*-?\s*(nummer|nr\.?|code|number|no\.?)\s*:?\s*([A-Z0-9][A-Z0-9-]{3,})/i;
  for (const form of document.querySelectorAll("form")) {
    if (form.querySelector("input[type=radio], select, input[type=password]")) continue;
    const inputs = Array.from(form.querySelectorAll("input:not([type]),input[type=text],input[type=number]")).filter((i) => !i.value);
    const field = inputs.find((i) => /receipt|beleg|empfang|quittung|nummer|code|number/i.test((i.name || "") + " " + (i.id || "") + " " + (i.labels && i.labels[0] ? txt(i.labels[0]) : "")));
    if (!field || inputs.length > 1) continue;
    if (field.closest("fieldset[disabled]") || field.closest("[inert]")) continue;
    let code = null;
    for (const el of document.querySelectorAll("canvas[aria-label], [role=img][aria-label], img[alt], p, span, div, strong, output")) {
      const t = el.tagName === "CANVAS" || el.getAttribute("role") === "img" ? el.getAttribute("aria-label") : el.tagName === "IMG" ? el.getAttribute("alt") : (el.children.length ? "" : txt(el));
      const m = t && t.replace(ZW, "").match(codeRe);
      if (m) { code = m[3]; break; }
    }
    if (!code) continue;
    const boxes = Array.from(form.querySelectorAll("input[type=checkbox]")).filter((b) => !b.checked && (b.required || /gelesen|akzeptiert|bestätig|zur kenntnis|read|agree|accept/i.test(txt(b.closest("label")))));
    const submit = Array.from(form.querySelectorAll("button,input[type=submit]")).find((b) => (b.type || "submit") === "submit" && !/abbrech|cancel|löschen|zurück|reset/i.test(label(b)));
    out.receipt = { field: tag(field), code, boxes: boxes.map((b) => tag(b)), submit: submit ? tag(submit) : null };
    break;
  }

  // slider + checkbox gates that keep an area disabled
  for (const r of document.querySelectorAll("input[type=range]")) {
    const sec = r.closest("section,form,fieldset,div");
    if (!sec) continue;
    const guarded = Array.from(sec.querySelectorAll("fieldset")).find((f) => (f.disabled || f.hasAttribute("inert")) && !f.contains(r));
    const st = Array.from(sec.querySelectorAll("[role=status]")).find((s) => !(guarded && guarded.contains(s)));
    const locked = !!guarded || (st && /gesperrt|locked|blockiert/i.test(txt(st)));
    if (!locked) continue;
    const boxes = Array.from(sec.querySelectorAll("input[type=checkbox]")).filter((c) => !(guarded && guarded.contains(c)));
    out.gates.push({ range: tag(r), min: r.min || "0", max: r.max || "100", value: r.value, boxes: boxes.map((b) => ({ id: tag(b), checked: b.checked, text: txt(b.closest("label")) })) });
  }
  return out;
}
