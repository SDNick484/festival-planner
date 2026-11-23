// Safe DOM construction. Every screen builds its elements with h(); user- and email-supplied text
// reaches the page only as text nodes, never as markup. There is no HTML-string sink anywhere in
// static/ (tests/ui/test_shell.py greps for them).

// Elements that run or embed code are never created from data.
const FORBIDDEN_TAGS = new Set(["script", "style", "iframe", "frame", "object", "embed", "base", "link", "meta"]);
const URL_ATTRS = new Set(["href", "src", "action", "formaction", "poster", "cite"]);
const SAFE_URL = /^(?:https?:|mailto:|tel:|[/#?.]|[^:]*$)/i;
const TAG = /^[a-z][a-z0-9-]*$/i;

// A URL a user typed (a task link, an email address) may only be a web, mail or phone link, or a
// relative one. script and data URLs, among others, are dropped. Control characters and spaces are
// stripped first, because browsers ignore them inside a scheme ("java\tscript:").
export function isSafeUrl(value) {
  const cleaned = String(value).replace(/[\u0000- \u007f-\u009f]/g, "");
  return SAFE_URL.test(cleaned);
}

// h(tag, attrs, ...children) -> HTMLElement
//   attrs     null, or an object. Values go through setAttribute; false/null/undefined skip the
//             attribute, true sets it empty. `on` takes {event: listener}, the only way to attach
//             listeners. Attribute names starting with "on", and "style" (the CSP blocks it:
//             use classes), throw.
//   children  strings and numbers become text nodes; null, undefined and booleans are skipped;
//             arrays are flattened; Nodes are appended as they are.
export function h(tag, attrs, ...children) {
  if (typeof tag !== "string" || !TAG.test(tag) || FORBIDDEN_TAGS.has(tag.toLowerCase())) {
    throw new Error(`h: cannot create <${tag}>`);
  }
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (name === "on") {
      for (const [type, listener] of Object.entries(value ?? {})) {
        if (typeof listener !== "function" || !/^[a-z][a-z0-9:_-]*$/i.test(type)) {
          throw new Error(`h: bad listener "${type}"`);
        }
        el.addEventListener(type, listener);
      }
      continue;
    }
    const lower = name.toLowerCase();
    if (lower.startsWith("on") || lower === "style" || !/^[a-z][\w:.-]*$/i.test(name)) {
      throw new Error(`h: attribute "${name}" is not allowed`);
    }
    if (value === false || value === null || value === undefined) continue;
    if (URL_ATTRS.has(lower) && !isSafeUrl(value)) continue;
    el.setAttribute(name, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || typeof child === "boolean") continue;
    if (Array.isArray(child)) append(el, child);
    else if (child instanceof Node) el.append(child);
    else el.append(document.createTextNode(String(child)));
  }
}

// Replace everything inside `el` with `children` (same rules as h).
export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}
