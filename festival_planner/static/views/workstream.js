// Workstream screens: `#/ws` lists every workstream, `#/ws/:id` shows one (design §6.2).
import { h, mount } from "../dom.js";
import * as api from "../api.js";
import {
  byEffectiveDue,
  errorText,
  fetchAll,
  formatDay,
  loadOrganizers,
  ownerLabel,
  statusDot,
} from "./taskbits.js";

export async function render(root, params) {
  if (params.id === undefined) return renderList(root);
  return renderDetail(root, Number(params.id));
}

function notFound(root) {
  mount(
    root,
    h("h1", null, "Workstream not found"),
    h("p", { class: "muted" }, "It may have been deleted."),
    h("a", { href: "#/ws" }, "All workstreams"),
  );
}

// --- the list -------------------------------------------------------------------------------

async function renderList(root) {
  const [workstreams, tasks, organizers] = await Promise.all([
    fetchAll("/api/workstreams"),
    fetchAll("/api/tasks"),
    loadOrganizers(),
  ]);
  const counts = new Map();
  for (const task of tasks) {
    const count = counts.get(task.workstream_id) ?? { done: 0, total: 0 };
    count.total += 1;
    if (task.status === "done") count.done += 1;
    counts.set(task.workstream_id, count);
  }
  const cards = workstreams.map((ws) => {
    const { done, total } = counts.get(ws.id) ?? { done: 0, total: 0 };
    return h(
      "li",
      { class: "ws-card", "data-id": ws.id },
      h(
        "a",
        { href: `#/ws/${ws.id}` },
        h("span", { class: "ws-name wrap" }, ws.name),
        h(
          "span",
          { class: "ws-meta muted" },
          [ownerLabel(organizers, ws.owner), `${done} of ${total} done`, ws.archived ? "Archived" : null]
            .filter(Boolean)
            .join(" · "),
        ),
      ),
    );
  });
  mount(
    root,
    h("h1", null, "Workstreams"),
    cards.length ? h("ul", { class: "ws-list" }, cards) : h("p", { class: "muted" }, "No workstreams yet."),
  );
}

// --- one workstream -------------------------------------------------------------------------

function taskRow(task, organizers) {
  const note =
    task.status === "blocked" && task.blocked_reason
      ? `Blocked: ${task.blocked_reason}`
      : task.status === "waiting" && task.waiting_on
        ? `Waiting on ${task.waiting_on}`
        : null;
  const meta = [task.effective_due ? formatDay(task.effective_due) : null, task.owner ? ownerLabel(organizers, task.owner) : null]
    .filter(Boolean)
    .join(" · ");
  return h(
    "li",
    { class: "task-row", "data-id": task.id, "data-status": task.status, "data-due": task.effective_due ?? "" },
    h(
      "a",
      { href: `#/task/${task.id}` },
      statusDot(task.status),
      h(
        "span",
        { class: "task-main" },
        h("span", { class: "task-title wrap" }, task.title),
        note ? h("span", { class: "task-note wrap" }, note) : null,
        meta ? h("span", { class: "task-meta muted" }, meta) : null,
      ),
    ),
  );
}

function contactItem(contact) {
  const count = contact.emails.length;
  const sub = [contact.organization, contact.role].filter(Boolean).join(" · ");
  return h(
    "li",
    { class: "contact" },
    h("span", { class: "contact-name wrap" }, contact.name),
    sub ? h("span", { class: "muted wrap" }, sub) : null,
    count ? h("span", { class: "muted" }, `${count} ${count === 1 ? "email" : "emails"}`) : null,
    contact.emails.map((email) => h("a", { class: "wrap", href: `mailto:${email}` }, email)),
    contact.phone ? h("a", { href: `tel:${contact.phone}` }, contact.phone) : null,
  );
}

async function renderDetail(root, id) {
  let ws;
  try {
    ws = await api.get(`/api/workstreams/${id}`);
  } catch (error) {
    if (error instanceof api.ApiError && error.status === 404) return notFound(root);
    throw error;
  }
  const [tasks, contacts, organizers] = await Promise.all([
    fetchAll(`/api/tasks?workstream=${id}`),
    fetchAll(`/api/contacts?workstream=${id}`),
    loadOrganizers(),
  ]);

  const summary = h("p", { class: "muted", "data-role": "summary" });
  const openBox = h("div", { "data-role": "open" });
  const doneBox = h("details", { class: "done-tasks", "data-role": "done" });

  // The sections that depend on the task list. Quick add calls this again after it saves.
  // Task 23 adds a board toggle: it swaps what draws into `openBox` and leaves the rest alone.
  function draw() {
    const open = tasks.filter((t) => t.status !== "done").sort(byEffectiveDue);
    const done = tasks.filter((t) => t.status === "done");
    mount(
      summary,
      `Owner: ${ownerLabel(organizers, ws.owner)} · ${done.length} of ${tasks.length} done`,
    );
    mount(
      openBox,
      open.length
        ? h("ul", { class: "task-list" }, open.map((t) => taskRow(t, organizers)))
        : h("p", { class: "muted" }, "No open tasks."),
    );
    mount(
      doneBox,
      h("summary", null, `Done (${done.length})`),
      done.length ? h("ul", { class: "task-list" }, done.map((t) => taskRow(t, organizers))) : null,
    );
  }

  const titleInput = h("input", { type: "text", id: "new-task-title", autocomplete: "off", "aria-label": "New task title", placeholder: "New task title" });
  const addError = h("p", { class: "field-error", role: "alert" });
  const addButton = h("button", { type: "submit", class: "btn btn-primary" }, "+ Add task");
  const addForm = h(
    "form",
    {
      class: "add-task",
      on: {
        submit: async (event) => {
          event.preventDefault();
          const title = titleInput.value.trim();
          if (!title) {
            mount(addError, "Enter a title.");
            return;
          }
          addButton.disabled = true;
          try {
            const created = await api.post("/api/tasks", { workstream_id: id, title });
            mount(addError);
            titleInput.value = "";
            tasks.push(created);
            draw();
          } catch (error) {
            // The title stays in the box so it can be fixed.
            mount(addError, (error instanceof api.ApiError && error.fields.title) || errorText(error));
          } finally {
            addButton.disabled = false;
          }
          titleInput.focus();
        },
      },
    },
    h("div", { class: "add-row" }, titleInput, addButton),
    addError,
  );

  const contactSection = h(
    "section",
    { class: "ws-section" },
    h("h2", null, "Contacts"),
    contacts.length
      ? h("ul", { class: "contacts", "data-role": "contacts" }, contacts.map(contactItem))
      : h("p", { class: "muted" }, "No contacts yet. Add them under ", h("a", { href: "#/manage" }, "Manage"), "."),
  );

  draw();
  mount(
    root,
    h("a", { class: "back", href: "#/ws" }, "← Workstreams"),
    h("h1", { class: "wrap" }, ws.name),
    summary,
    h("section", { class: "ws-section" }, h("h2", null, "Open tasks"), openBox),
    addForm,
    contactSection,
    doneBox,
  );
}
