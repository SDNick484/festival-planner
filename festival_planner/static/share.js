// The volunteer share page (/s/<token>): a read-only list of milestones and tasks. The token is
// in this page's own path; the data comes from <path>/data. Nothing is stored in the browser, no
// referrer is sent, and every value reaches the page as text (dom.js h()).
import { h, mount } from "/static/dom.js";

const STATUS = { todo: "To do", doing: "Doing", waiting: "Waiting", blocked: "Blocked", done: "Done" };

function formatDate(iso) {
  if (!iso) return "No date";
  const date = new Date(`${iso}T00:00:00Z`);
  return date.toLocaleDateString(undefined, { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" });
}

function milestones(list) {
  if (!list.length) return null;
  return h(
    "section",
    { class: "sh-section", "data-share": "milestones" },
    h("h2", null, "Milestones"),
    h(
      "ul",
      { class: "sh-list" },
      list.map((m) =>
        h("li", { class: "sh-item", "data-share-item": "milestone" }, h("span", { class: "sh-title wrap" }, m.name), h("span", { class: "muted sh-meta" }, formatDate(m.date))),
      ),
    ),
  );
}

function tasks(list) {
  return h(
    "section",
    { class: "sh-section", "data-share": "tasks" },
    h("h2", null, "Tasks"),
    list.length
      ? h(
          "ul",
          { class: "sh-list" },
          list.map((t) =>
            h(
              "li",
              { class: "sh-item", "data-share-item": "task", "data-status": t.status },
              h("span", { class: "sh-title wrap" }, t.title),
              h(
                "span",
                { class: "sh-meta" },
                h("span", { class: "sh-status" }, STATUS[t.status] ?? t.status),
                h("span", { class: "muted" }, formatDate(t.effective_due)),
                t.owner ? h("span", { class: "muted" }, t.owner) : null,
              ),
            ),
          ),
        )
      : h("p", { class: "muted" }, "No tasks yet."),
  );
}

async function load() {
  const page = document.getElementById("share");
  const body = document.getElementById("share-body");
  let data;
  try {
    const res = await fetch(`${location.pathname.replace(/\/+$/, "")}/data`, {
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    page.dataset.state = "error";
    mount(body, h("p", { class: "sh-error", role: "alert" }, "This link is not available. It may have expired or been turned off."));
    return;
  }
  mount(body, milestones(data.milestones ?? []), tasks(data.tasks ?? []));
  page.dataset.state = "ready";
}

load();
