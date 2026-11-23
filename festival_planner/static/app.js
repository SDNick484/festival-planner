// The shell: a hash router and the bottom tab bar. Each view is a module in views/ that exports
//   render(root, params)   // may be async; build the screen inside `root` with h()
// `params` holds the route's captured values ({id: "12"}, digits as a string) and `query`
// (a URLSearchParams for anything after "?").
//
// To ship a view, add static/views/<view>.js and set that route's `ready` to true: one line in
// ROUTES. A route that is not ready shows a "coming soon" placeholder and loads nothing.
import { h, mount } from "./dom.js";
import { registerServiceWorker } from "./offline.js";

// tab: which tab is highlighted. Patterns: ":id" matches digits, a trailing "?" makes it optional.
export const ROUTES = [
  { path: "/", view: "home", ready: true, tab: "home", title: "Home" }, // #10
  { path: "/ws/:id?", view: "workstream", ready: true, tab: "ws", title: "Workstreams" }, // #11
  { path: "/task/:id", view: "task", ready: true, tab: "ws", title: "Task" }, // #11
  { path: "/timeline", view: "timeline", ready: true, tab: "timeline", title: "Timeline" }, // #12
  { path: "/manage", view: "manage", ready: true, tab: "home", title: "Manage" }, // #12
  { path: "/inbox", view: "inbox", ready: true, tab: "inbox", title: "Inbox" }, // #16
];

const TABS = [
  { id: "home", label: "Home", href: "#/" },
  { id: "ws", label: "Workstreams", href: "#/ws" },
  { id: "timeline", label: "Timeline", href: "#/timeline" },
  { id: "inbox", label: "Inbox", href: "#/inbox" },
];
const APP_NAME = "Festival Planner";

function compile(path) {
  const names = [];
  const source = path
    .split("/")
    .filter(Boolean)
    .map((part) => {
      if (!part.startsWith(":")) return `/${part}`;
      const optional = part.endsWith("?");
      names.push(part.slice(1, optional ? -1 : undefined));
      return optional ? "(?:/(\\d+))?" : "/(\\d+)";
    })
    .join("");
  return { regex: new RegExp(`^${source || "/"}$`), names };
}

const COMPILED = ROUTES.map((route) => ({ route, ...compile(route.path) }));

// "#/ws/3?x=1" -> { route, params, query } or null when nothing matches.
export function resolve(hash) {
  const raw = hash.replace(/^#/, "") || "/";
  const [path, search = ""] = raw.split("?", 2);
  const normal = path.length > 1 ? path.replace(/\/+$/, "") : path;
  for (const { route, regex, names } of COMPILED) {
    const match = regex.exec(normal);
    if (!match) continue;
    const params = {};
    names.forEach((name, i) => {
      if (match[i + 1] !== undefined) params[name] = match[i + 1];
    });
    return { route, params, query: new URLSearchParams(search) };
  }
  return null;
}

const main = document.getElementById("view");
let navigation = 0;

function tabBar() {
  return h(
    "nav",
    { class: "tabbar", "aria-label": "Primary" },
    TABS.map((tab) => h("a", { href: tab.href, "data-tab": tab.id }, tab.label)),
  );
}

function markCurrent(tabId) {
  for (const link of document.querySelectorAll(".tabbar a")) {
    if (link.dataset.tab === tabId) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function placeholder(title, note) {
  return [h("h1", null, title), h("p", { class: "muted" }, note)];
}

async function show() {
  const token = ++navigation;
  const found = resolve(location.hash);
  // A fresh element per navigation: a slow view that finishes after the user moved on writes into
  // a detached node instead of the current screen.
  const root = h("section", { class: "view", tabindex: "-1" });
  let title = "Not found";
  let tab = null;

  if (!found) {
    root.dataset.view = "not-found";
    mount(root, placeholder("Not found", "There is nothing at this address."), h("a", { href: "#/" }, "Go home"));
  } else {
    const { route, params, query } = found;
    title = route.title;
    tab = route.tab;
    root.dataset.view = route.view;
    root.classList.add(`view-${route.view}`);
    if (params.id) root.dataset.id = params.id;
    if (!route.ready) {
      mount(root, placeholder(route.title, "This screen is coming soon."));
    } else {
      try {
        const module = await import(`./views/${route.view}.js`);
        if (token !== navigation) return;
        await module.render(root, { ...params, query });
      } catch (error) {
        console.error(error);
        mount(root, placeholder("Something went wrong", "This screen could not be shown. Reload to try again."));
      }
    }
  }
  if (token !== navigation) return;
  main.replaceChildren(root);
  markCurrent(tab);
  document.title = `${title} · ${APP_NAME}`;
  window.scrollTo(0, 0);
  root.focus({ preventScroll: true });
}

document.getElementById("app").append(tabBar());
window.addEventListener("hashchange", show);
show();
registerServiceWorker(import.meta.url);
