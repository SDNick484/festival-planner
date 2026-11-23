// Home (spec 6.1, mockup "C"): countdown and overall %, an inbox nudge, Needs attention, a
// collapsed Due this week, and workstream progress. One GET /api/digest feeds all of it.
import { get } from "../api.js";
import { h, mount } from "../dom.js";

const PROBLEMS = [
  ["overdue", "dot-danger"],
  ["blocked", "dot-danger"],
  ["waiting", "dot-warn"],
];

// "overdue 2d", "blocked", "waiting 6d". A blocked task's age is not shown (spec 6.1).
function reasonText(item) {
  return item.reason === "blocked" ? "blocked" : `${item.reason} ${item.age_days}d`;
}

// "2099-10-10" -> "Sat, Oct 10". The date is a plain calendar date, so it is formatted in UTC
// to keep the browser's own time zone from shifting it.
function shortDate(iso) {
  const when = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

function taskRow(item, dotClass, right) {
  return h(
    "a",
    { class: "row", href: `#/task/${item.id}` },
    dotClass ? h("span", { class: `dot ${dotClass}`, "aria-hidden": "true" }) : null,
    h("span", { class: "title truncate" }, item.title),
    h("span", { class: "reason" }, right),
  );
}

function header(digest, overall) {
  const { event, countdown } = digest;
  return h(
    "header",
    { class: "home-header span-all", "data-section": "header" },
    h("div", { class: "home-top" }, h("h1", null, "Home"), h("a", { class: "manage-link", href: "#/manage" }, "Manage")),
    h(
      "div",
      { class: "home-stats" },
      h(
        "div",
        { class: "stat" },
        h("div", { class: "countdown" }, countdown.label),
        h("div", { class: "stat-caption muted wrap" }, `until ${event.name}`),
      ),
      h(
        "div",
        { class: "stat stat-end" },
        h("div", { class: "overall" }, `${overall.pct}%`, h("span", { class: "overall-label" }, " done")),
        h("div", { class: "stat-caption muted" }, `${overall.done} of ${overall.total} tasks`),
      ),
    ),
  );
}

function inboxNudge(count) {
  if (!count) return null;
  return h(
    "a",
    { class: "nudge span-all", href: "#/inbox", "data-section": "inbox" },
    `${count} ${count === 1 ? "email" : "emails"} to file →`,
  );
}

function attention(items) {
  return h(
    "section",
    { class: "home-section home-attention", "data-section": "attention" },
    h("h2", null, "Needs attention"),
    items.length
      ? h(
          "div",
          { class: "rows" },
          items.map((item) => taskRow(item, PROBLEMS.find(([reason]) => reason === item.reason)?.[1] ?? "dot-warn", reasonText(item))),
        )
      : h("p", { class: "muted empty" }, "Nothing needs attention 🎉"),
  );
}

// Collapsed by default. The rows stay in the DOM and are hidden, so the count in the heading and
// the list always agree and the toggle is a plain aria-expanded button.
function dueThisWeek(items) {
  const section = h("section", { class: "home-section home-due", "data-section": "due" });
  if (!items.length) {
    return mount(section, h("h2", null, "Due this week"), h("p", { class: "muted empty" }, "Nothing due this week"));
  }
  const list = h(
    "div",
    { class: "rows", id: "due-this-week", hidden: true },
    items.map((item) => taskRow(item, null, shortDate(item.effective_due))),
  );
  const toggle = h(
    "button",
    { class: "disclosure", type: "button", "aria-expanded": "false", "aria-controls": "due-this-week" },
    h("span", { class: "disclosure-caret", "aria-hidden": "true" }),
    "Due this week",
    " ",
    h("span", { class: "count" }, items.length),
  );
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    list.hidden = !open;
  });
  return mount(section, h("h2", null, toggle), list);
}

function workstreams(rows, problems) {
  return h(
    "section",
    { class: "home-section home-workstreams", "data-section": "workstreams" },
    h("h2", null, "Workstreams"),
    h(
      "div",
      { class: "rows" },
      rows.map((ws) => {
        const mine = problems.get(ws.id) ?? {};
        const chips = PROBLEMS.filter(([reason]) => mine[reason]).map(([reason, dot]) =>
          h("span", { class: `chip ${dot.replace("dot", "chip")}` }, `${mine[reason]} ${reason}`),
        );
        return h(
          "a",
          { class: "ws-row", href: `#/ws/${ws.id}` },
          h(
            "span",
            { class: "ws-line" },
            h("span", { class: "title truncate" }, ws.name),
            h("span", { class: "ws-count muted" }, `${ws.done}/${ws.total}`),
          ),
          h("progress", { max: "100", value: String(ws.pct), "aria-label": `${ws.name}: ${ws.pct}% done` }),
          chips.length ? h("span", { class: "chips" }, chips) : null,
        );
      }),
    ),
  );
}

export async function render(root) {
  let digest;
  try {
    digest = await get("/api/digest");
  } catch (error) {
    mount(root, h("h1", null, "Home"), h("p", { class: "muted" }, `Could not load the dashboard. ${error.message}`));
    return;
  }
  const done = digest.workstreams.reduce((sum, ws) => sum + ws.done, 0);
  const total = digest.workstreams.reduce((sum, ws) => sum + ws.total, 0);
  const overall = { done, total, pct: total ? Math.round((done * 100) / total) : 0 };

  // Problem chips per workstream come from the same needs_attention list the section shows.
  const problems = new Map();
  for (const item of digest.needs_attention) {
    const counts = problems.get(item.workstream_id) ?? {};
    counts[item.reason] = (counts[item.reason] ?? 0) + 1;
    problems.set(item.workstream_id, counts);
  }

  mount(
    root,
    header(digest, overall),
    inboxNudge(digest.inbox_count),
    attention(digest.needs_attention),
    dueThisWeek(digest.due_this_week),
    workstreams(digest.workstreams, problems),
  );
}
