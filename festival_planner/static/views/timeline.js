// Timeline (#/timeline): the milestones in date order, a "Today" marker between past and future,
// and the tasks attached to each milestone. A milestone whose tasks are all done is green.
//
// A milestone's date is derived here exactly as the server derives it: the event date minus its
// days before the event, so moving the event date moves every milestone. "Today" is the calendar
// date in the event's time zone (never the browser's), as everywhere else in the app.
import { h, mount } from "../dom.js";
import * as api from "../api.js";

const PAGE = 500;

async function fetchAll(path) {
  const out = [];
  for (;;) {
    const batch = await api.get(`${path}?limit=${PAGE}&offset=${out.length}`);
    out.push(...batch);
    if (batch.length < PAGE) return out;
  }
}

// YYYY-MM-DD in the given IANA time zone; UTC if the browser does not know the zone.
function todayIn(timeZone) {
  const options = { year: "numeric", month: "2-digit", day: "2-digit" };
  try {
    return new Intl.DateTimeFormat("en-CA", { ...options, timeZone }).format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

function minusDays(iso, days) {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

function pretty(iso) {
  const date = new Date(`${iso}T00:00:00Z`);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function taskRow(task) {
  return h(
    "li",
    { class: "tl-task", "data-status": task.status },
    h("a", { href: `#/task/${task.id}`, class: "tl-task-title wrap" }, task.title),
    h("span", { class: "tl-status" }, task.status),
  );
}

function milestoneItem(stone, date, tasks, today) {
  // "T-14 · Name": a name that only repeats the label ("T-14") is shown once.
  const label = `T-${stone.days_before_event}`;
  const title = stone.name.trim() === label ? label : `${label} · ${stone.name}`;
  const done = tasks.length > 0 && tasks.every((task) => task.status === "done");
  const classes = ["tl-item", done && "tl-done", date < today && "tl-past"].filter(Boolean).join(" ");
  const head = [
    h("span", { class: "tl-dot", "aria-hidden": "true" }),
    h("span", { class: "tl-title wrap" }, title),
    h("span", { class: "tl-date" }, pretty(date)),
    h("span", { class: "tl-count" }, tasks.length ? plural(tasks.length, "task") : "No tasks"),
  ];
  const body = tasks.length
    ? h("details", { class: "tl-details" }, h("summary", { class: "tl-head" }, head), h("ul", { class: "tl-tasks" }, tasks.map(taskRow)))
    : h("div", { class: "tl-head" }, head);
  return h(
    "li",
    {
      class: classes,
      "data-tl": "milestone",
      "data-id": stone.id,
      "data-days": stone.days_before_event,
      "data-date": date,
    },
    body,
  );
}

export async function render(root, params) {
  const [event, milestones, tasks] = await Promise.all([
    api.get("/api/event"),
    fetchAll("/api/milestones"),
    fetchAll("/api/tasks"),
  ]);
  const today = todayIn(event.timezone);
  const byMilestone = new Map();
  for (const task of tasks) {
    if (task.milestone_id === null) continue;
    if (!byMilestone.has(task.milestone_id)) byMilestone.set(task.milestone_id, []);
    byMilestone.get(task.milestone_id).push(task);
  }

  const dated = milestones
    .map((stone) => ({ stone, date: minusDays(event.event_date, stone.days_before_event) }))
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.stone.id - b.stone.id));

  const marker = h(
    "li",
    { class: "tl-today", "data-tl": "today", "data-date": today },
    h("span", { class: "tl-today-label" }, "Today"),
    h("span", { class: "tl-date" }, pretty(today)),
  );
  const items = [];
  let placed = false;
  for (const { stone, date } of dated) {
    // A milestone dated today is still ahead: the marker goes above it.
    if (!placed && date >= today) {
      items.push(marker);
      placed = true;
    }
    items.push(milestoneItem(stone, date, byMilestone.get(stone.id) ?? [], today));
  }
  if (!placed) items.push(marker);
  items.push(
    h(
      "li",
      { class: "tl-event", "data-tl": "event", "data-date": event.event_date },
      h("span", { class: "tl-title wrap" }, event.name),
      h("span", { class: "tl-date" }, pretty(event.event_date)),
    ),
  );

  mount(
    root,
    h("h1", null, "Timeline"),
    h("p", { class: "muted" }, "Milestones count back from the event date. Tasks are listed under the milestone they belong to."),
    milestones.length ? null : h("p", { class: "muted" }, "No milestones yet. Add them on the Manage screen."),
    h("ol", { class: "tl-list" }, items),
    h("p", null, h("a", { href: "#/manage" }, "Manage milestones")),
  );
}
