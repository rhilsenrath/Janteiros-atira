// Leitor: turns one frame's DOM into compact text for the model.
// - numbers every interactive element ([12]) via data-jid so helpers can act on it later
// - drops decoys (aria-hidden, [hidden], inline display:none / visibility:hidden)
// - keeps true data that browsers hide (tooltips, closed <details>, aria-label of images/canvas)
// - strips zero-width characters, flattens tables into "| a | b |" rows
// - leaves {{FRAME:n}} placeholders where iframes are, so Python can inline them
(args) => {
  const startId = args.startId || 1;
  const frameTag = args.frameTag || "f";
  let nextId = startId;
  let frameCount = 0;
  const ZW = /[​-‏⁠﻿­]/g;
  const signals = { password: false, dialogs: [], gates: [], decoys: 0, frames: [] };

  for (const el of document.querySelectorAll("[data-jid]")) el.removeAttribute("data-jid");
  for (const el of document.querySelectorAll("[data-jframe]")) el.removeAttribute("data-jframe");

  const clean = (s) => (s || "").replace(ZW, "").replace(/[ \t\r\f\v ]+/g, " ");
  const oneLine = (s) => clean(s).replace(/\s+/g, " ").trim();
  const BLOCK = new Set(["DIV", "P", "SECTION", "ARTICLE", "LI", "UL", "OL", "TR", "TABLE", "THEAD", "TBODY",
    "H1", "H2", "H3", "H4", "H5", "H6", "DL", "DT", "DD", "FORM", "FIELDSET", "LEGEND", "DETAILS", "PRE",
    "HEADER", "FOOTER", "NAV", "ASIDE", "MAIN", "BLOCKQUOTE", "FIGURE", "FIGCAPTION", "CAPTION", "DIALOG", "HR", "ADDRESS"]);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "HEAD", "META", "LINK"]);

  const inlineStyle = (el) => (el.getAttribute && el.getAttribute("style")) || "";
  function isDecoy(el) {
    const role = el.getAttribute("role") || "";
    const keepRole = role === "tooltip" || role === "menu" || role === "menuitem" || role === "listbox";
    if (el.getAttribute("aria-hidden") === "true") return true;
    if (el.hasAttribute("hidden") && !keepRole) return true;
    const st = inlineStyle(el);
    if (!keepRole && (/display\s*:\s*none/i.test(st) || /visibility\s*:\s*hidden/i.test(st))) return true;
    if (el.tagName !== "DETAILS" && el.tagName !== "DIALOG" && !keepRole) {
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" && el.parentElement && getComputedStyle(el.parentElement).visibility !== "hidden") return true;
      if (cs.fontSize === "0px" && oneLine(el.textContent).length > 0) return true;
    }
    return false;
  }

  function visible(el) {
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  function mark(el) {
    const id = nextId++;
    el.setAttribute("data-jid", String(id));
    return id;
  }

  function shortHref(a) {
    const raw = a.getAttribute("href") || "";
    if (!raw || raw.startsWith("javascript:") || raw === "#") return "";
    try {
      const u = new URL(raw, document.baseURI);
      return u.origin === location.origin ? u.pathname + u.search + u.hash : u.href;
    } catch (e) { return raw; }
  }

  function labelText(el) {
    const al = el.getAttribute("aria-label");
    if (al) return oneLine(al);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean).map((n) => n.textContent).join(" ");
      if (oneLine(t)) return oneLine(t);
    }
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map((l) => {
        const c = l.cloneNode(true);
        for (const x of c.querySelectorAll("input,select,textarea,button,script,style")) x.remove();
        return c.textContent || l.getAttribute("aria-label") || "";
      }).join(" ");
      if (oneLine(t)) return oneLine(t);
    }
    return "";
  }

  function disabledMark(el) {
    const fs = el.closest && el.closest("fieldset[disabled]");
    const inert = el.closest && el.closest("[inert]");
    return el.disabled || fs || inert ? " (disabled)" : "";
  }

  function control(el) {
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "INPUT" && type === "hidden") return "";
    const id = mark(el);
    const dis = disabledMark(el);
    const name = el.getAttribute("name");
    const nm = name ? ` name=${name}` : "";
    if (tag === "A") {
      const text = oneLine(innerText(el)) || labelText(el) || "(no text)";
      const href = shortHref(el);
      const tgt = el.getAttribute("target") && el.getAttribute("target") !== "_self" ? ` target=${el.getAttribute("target")}` : "";
      return `[${id}] link "${text}"${href ? " → " + href : ""}${tgt}${dis}`;
    }
    if (tag === "SELECT") {
      const opts = Array.from(el.options);
      const shown = opts.slice(0, 70).map((o) => {
        const t = oneLine(o.textContent);
        const v = o.value;
        return (v === t ? t : `${t} {${v}}`) + (o.selected ? "*" : "");
      });
      const more = opts.length > 70 ? ` | …(${opts.length - 70} more)` : "";
      return `[${id}] select${nm}${labelOwn(el)}${dis} options: ${shown.join(" | ")}${more}`;
    }
    if (tag === "TEXTAREA") {
      return `[${id}] textarea${nm} value="${oneLine(el.value).slice(0, 200)}"${dis}`;
    }
    if (tag === "INPUT") {
      if (type === "checkbox") return `[${id}] checkbox ${el.checked ? "☑" : "☐"}${nm}${labelOwn(el)}${dis}`;
      if (type === "radio") return `[${id}] radio ${el.checked ? "◉" : "○"}${nm} value=${el.value}${labelOwn(el)}${dis}`;
      if (type === "range") return `[${id}] slider min=${el.min || 0} max=${el.max || 100} value=${el.value}${labelOwn(el)}${dis}`;
      if (type === "submit" || type === "button" || type === "reset" || type === "image") {
        return `[${id}] button "${oneLine(el.value) || labelText(el)}"${dis}`;
      }
      if (type === "password") signals.password = signals.password || visible(el);
      const ph = el.getAttribute("placeholder");
      return `[${id}] input${type && type !== "text" ? "(" + type + ")" : ""}${nm}${labelOwn(el)} value="${type === "password" ? (el.value ? "•••" : "") : oneLine(el.value)}"${ph ? ` placeholder="${oneLine(ph)}"` : ""}${dis}`;
    }
    if (tag === "SUMMARY") {
      const det = el.parentElement;
      return `[${id}] ${det && det.open ? "▾" : "▸"} "${oneLine(innerText(el))}"${det && det.open ? "" : " (collapsed; content shown below)"}`;
    }
    // button or role=button/menuitem/tab
    const text = oneLine(innerText(el)) || labelText(el) || "(no text)";
    const al = el.getAttribute("aria-label");
    const extra = al && oneLine(al) !== text ? ` aria-label="${oneLine(al)}"` : "";
    const val = el.getAttribute("value");
    const v = tag === "BUTTON" && name && val ? ` {${name}=${val}}` : "";
    const exp = el.getAttribute("aria-expanded") ? ` expanded=${el.getAttribute("aria-expanded")}` : "";
    return `[${id}] button "${text}"${extra}${v}${exp}${dis}`;
  }

  // label of a control, unless the control sits inside its <label> (then the text follows anyway)
  function labelOwn(el) {
    if (el.closest && el.closest("label") && !el.getAttribute("aria-label")) return "";
    const lab = labelText(el);
    return lab ? ` "${lab}"` : "";
  }

  function innerText(el) {
    // text of an element without its decoys, respecting aria-label of role=img children
    let s = "";
    for (const n of el.childNodes) {
      if (n.nodeType === 3) s += n.nodeValue;
      else if (n.nodeType === 1) {
        if (SKIP.has(n.tagName) || isDecoy(n)) continue;
        const role = n.getAttribute("role");
        if (role === "img" && n.getAttribute("aria-label")) s += n.getAttribute("aria-label");
        else s += innerText(n);
      }
    }
    return s;
  }

  function isInteractive(el) {
    const tag = el.tagName;
    if (tag === "A" && el.hasAttribute("href")) return true;
    if (tag === "BUTTON" || tag === "SELECT" || tag === "TEXTAREA" || tag === "SUMMARY") return true;
    if (tag === "INPUT") return true;
    const role = el.getAttribute("role");
    if (role === "button" || role === "menuitem" || role === "tab" || role === "link" || role === "option") return true;
    return false;
  }

  function walk(node, inTable) {
    if (node.nodeType === 3) return clean(node.nodeValue);
    if (node.nodeType !== 1) return "";
    const el = node;
    const tag = el.tagName;
    if (SKIP.has(tag)) return "";
    if (isDecoy(el)) { signals.decoys++; return ""; }

    if (tag === "DIALOG") {
      if (!el.open) return "";
      const modal = el.matches(":modal");
      signals.dialogs.push({ label: oneLine(el.getAttribute("aria-label") || ""), modal });
      return `\n[${modal ? "MODAL DIALOG" : "DIALOG"} "${oneLine(el.getAttribute("aria-label") || "")}"]\n${children(el, inTable)}\n[/DIALOG]\n`;
    }
    if (tag === "IFRAME" || tag === "FRAME") {
      const n = `${frameTag}${frameCount++}`;
      el.setAttribute("data-jframe", n);
      signals.frames.push(n);
      return `\n{{FRAME:${n}}}\n`;
    }
    if (tag === "CANVAS") {
      const al = el.getAttribute("aria-label");
      const id = mark(el);
      return al ? ` «image: ${oneLine(al)}» ` : ` «image [${id}] without text; use look() to read it» `;
    }
    if (tag === "IMG") {
      const alt = el.getAttribute("alt") || el.getAttribute("aria-label");
      if (!alt) return "";
      return ` «image: ${oneLine(alt)}» `;
    }
    const role = el.getAttribute("role");
    if (role === "img") {
      const al = el.getAttribute("aria-label") || el.getAttribute("data-label");
      if (al) return ` ${oneLine(al)} `;
    }
    if (role === "tooltip") return ` (tooltip: ${oneLine(innerText(el))}) `;
    if (tag === "BR") return "\n";
    if (tag === "TIME") {
      const dt = el.getAttribute("datetime");
      const t = oneLine(innerText(el));
      return dt && dt !== t ? ` ${t} ⟨${dt}⟩ ` : ` ${t} `;
    }

    if (isInteractive(el)) {
      const c = control(el);
      if (tag === "SELECT" || tag === "TEXTAREA" || tag === "INPUT") return ` ${c} `;
      if (tag === "A" || tag === "BUTTON" || role === "button" || role === "menuitem" || role === "tab" || role === "link") {
        // nested content of buttons/links (e.g. role=img) is already in the label
        return ` ${c} `;
      }
      if (tag === "SUMMARY") return `\n${c}\n`;
      return ` ${c} `;
    }

    if (tag === "TR") {
      const cells = Array.from(el.children).filter((c) => !isDecoy(c)).map((c) => oneLine(children(c, true)));
      return "\n| " + cells.join(" | ") + " |\n";
    }
    if (tag === "DT") return "\n" + oneLine(children(el, inTable)) + ": ";
    if (tag === "DD") return oneLine(children(el, inTable)) + "\n";
    if (/^H[1-6]$/.test(tag)) return "\n" + "#".repeat(Number(tag[1])) + " " + oneLine(children(el, inTable)) + "\n";
    if (tag === "LI") return "\n- " + children(el, inTable).trim() + "\n";
    if (tag === "DETAILS") return "\n" + children(el, inTable) + "\n";

    let inner = children(el, inTable);
    if (el.shadowRoot) inner += children(el.shadowRoot, inTable);
    if (!oneLine(inner) && el.getAttribute("data-label")) inner = el.getAttribute("data-label");
    if (BLOCK.has(tag)) return "\n" + inner + "\n";
    return inner;
  }

  function children(el, inTable) {
    let kids = Array.from(el.childNodes);
    // flex/grid "order" or aria-posinset can scramble the visual order: restore the logical order
    if (kids.some((k) => k.nodeType === 1 && k.hasAttribute && k.hasAttribute("aria-posinset"))) {
      kids = kids.slice().sort((a, b) => {
        const pa = a.nodeType === 1 && a.getAttribute("aria-posinset") ? Number(a.getAttribute("aria-posinset")) : 0;
        const pb = b.nodeType === 1 && b.getAttribute("aria-posinset") ? Number(b.getAttribute("aria-posinset")) : 0;
        return pa - pb;
      });
    }
    let s = "";
    for (const k of kids) s += walk(k, inTable);
    return s;
  }

  // gates: a slider + checkbox guarding a disabled area
  for (const sec of document.querySelectorAll("section, div, form, fieldset")) {
    const range = sec.querySelector(":scope > label input[type=range], :scope > input[type=range]");
    if (!range) continue;
    const box = sec.querySelector(":scope > label input[type=checkbox], :scope > input[type=checkbox]");
    const guarded = sec.querySelector(":scope > fieldset");
    signals.gates.push({ locked: !!(guarded && (guarded.disabled || guarded.hasAttribute("inert"))) });
  }

  const body = document.body;
  let text = body ? walk(body, false) : "";
  text = text.replace(/[ \t]+\n/g, "\n").replace(/\n[ \t]+/g, "\n").replace(/\n{3,}/g, "\n\n").replace(/ {2,}/g, " ").trim();
  return { text, nextId, signals, title: document.title || "", url: location.href };
}
