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
  STATUSES,
  statusDot,
} from "./taskbits.js";

// Task 23: the List | Board choice is remembered in localStorage (which may be blocked or full:
// the screen works without it) and only applies at 900px and up.
const VIEW_KEY = "fp.ws-view";
const WIDE = "(min-width: 900px)";

function readViewMode() {
  try {
    return localStorage.getItem(VIEW_KEY) === "board" ? "board" : "list";
  } catch {
    return "list";
  }
}

function saveViewMode(mode) {
  try {
    localStorage.setItem(VIEW_KEY, mode);
  } catch {
    // Not remembered; the choice still holds until the screen is left.
  }
}

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

// --- the board (Task 23) --------------------------------------------------------------------

// One column per status. A card can be dragged onto a column, or moved with the status select on
// the card (the keyboard and screen-reader route). Both go through `moveTo`, which asks for a
// reason inline before it saves Blocked, as the task screen does. `onSaved` redraws.
function boardView(tasks, organizers, onSaved) {
  let dragId = null;
  let openPrompt = null; // closes the prompt of another card when a new one opens
  const cards = new Map();

  function buildCard(task) {
    const note =
      task.status === "blocked" && task.blocked_reason
        ? `Blocked: ${task.blocked_reason}`
        : task.status === "waiting" && task.waiting_on
          ? `Waiting on ${task.waiting_on}`
          : null;
    const meta = [task.effective_due ? formatDay(task.effective_due) : null, task.owner ? ownerLabel(organizers, task.owner) : null]
      .filter(Boolean)
      .join(" · ");
    const select = h(
      "select",
      { class: "board-status", "aria-label": `Status of ${task.title}`, on: { change: () => request(select.value) } },
      STATUSES.map((s) => h("option", { value: s.id }, s.label)),
    );
    select.value = task.status;
    const error = h("p", { class: "field-error", role: "alert" });
    let prompt = null;

    const closePrompt = () => {
      prompt?.remove();
      prompt = null;
      if (openPrompt === closePrompt) openPrompt = null;
      select.value = task.status;
    };

    async function save(status, reason, refocus) {
      try {
        const changes = status === "blocked" ? { status, blocked_reason: reason } : { status };
        Object.assign(task, await api.patch(`/api/tasks/${task.id}`, changes));
      } catch (e) {
        select.value = task.status;
        const message =
          (e instanceof api.ApiError && (e.fields.blocked_reason || e.fields.status)) || errorText(e);
        return message;
      }
      onSaved(refocus ? task.id : null);
      return null;
    }

    function askReason() {
      openPrompt?.();
      const input = h("input", { type: "text", id: `board-reason-${task.id}`, autocomplete: "off" });
      const promptError = h("p", { class: "field-error", role: "alert" });
      const submit = async () => {
        mount(promptError, "");
        const message = await save("blocked", input.value.trim(), true);
        if (message) mount(promptError, message);
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          submit();
        }
      });
      prompt = h(
        "div",
        { class: "blocked-prompt" },
        h("label", { for: input.id }, "Why is it blocked?"),
        input,
        h(
          "div",
          { class: "prompt-actions" },
          h("button", { type: "button", class: "btn btn-primary btn-small", on: { click: submit } }, "Save"),
          h("button", { type: "button", class: "btn btn-small", on: { click: () => { closePrompt(); select.focus(); } } }, "Cancel"),
        ),
        promptError,
      );
      openPrompt = closePrompt;
      el.append(prompt);
      input.focus();
    }

    // Used by the select (refocus: the board is redrawn) and by a drop.
    async function request(status, refocus = true) {
      mount(error, "");
      if (status === task.status) {
        select.value = task.status;
        return;
      }
      if (status === "blocked") {
        select.value = task.status; // the select shows the real status until a reason is saved
        askReason();
        return;
      }
      openPrompt?.();
      const message = await save(status, null, refocus);
      if (message) mount(error, message);
    }

    const el = h(
      "li",
      {
        class: "board-card",
        draggable: "true",
        "data-id": task.id,
        "data-status": task.status,
        on: {
          dragstart: (event) => {
            dragId = task.id;
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", String(task.id));
            el.classList.add("dragging");
          },
          dragend: () => {
            dragId = null;
            el.classList.remove("dragging");
            for (const col of document.querySelectorAll(".board-column.drag-over")) col.classList.remove("drag-over");
          },
        },
      },
      h(
        "a",
        { class: "board-link", href: `#/task/${task.id}`, draggable: "false" },
        h("span", { class: "task-title wrap" }, task.title),
        note ? h("span", { class: "task-note wrap" }, note) : null,
        meta ? h("span", { class: "task-meta muted" }, meta) : null,
      ),
      select,
      error,
    );
    cards.set(task.id, { request });
    return el;
  }

  function buildColumn(status) {
    const inColumn = tasks.filter((t) => t.status === status.id).sort(byEffectiveDue);
    const column = h(
      "section",
      {
        class: "board-column",
        "data-status": status.id,
        "aria-label": status.label,
        on: {
          dragover: (event) => {
            if (dragId === null) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            column.classList.add("drag-over");
          },
          dragleave: (event) => {
            if (!column.contains(event.relatedTarget)) column.classList.remove("drag-over");
          },
          drop: (event) => {
            event.preventDefault();
            column.classList.remove("drag-over");
            const id = dragId;
            dragId = null;
            if (id !== null) cards.get(id)?.request(status.id, false);
          },
        },
      },
      h("h3", { class: `board-heading status-${status.id}` }, status.label, " ", h("span", { class: "muted" }, `(${inColumn.length})`)),
      h("ul", { class: "board-cards" }, inColumn.map(buildCard)),
    );
    return column;
  }

  return h("div", { class: "board" }, STATUSES.map(buildColumn));
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
  const sectionTitle = h("h2", null, "Open tasks");
  let mode = readViewMode();
  const wide = window.matchMedia(WIDE);
  const modeButtons = [
    ["list", "List"],
    ["board", "Board"],
  ].map(([id, label]) =>
    h("button", {
      type: "button",
      class: "btn btn-small",
      "data-mode": id,
      on: {
        click: () => {
          mode = id;
          saveViewMode(mode);
          draw();
        },
      },
    }, label),
  );
  // Hidden below 900px by CSS; the board is never drawn there either, whatever was remembered.
  const toggle = h("div", { class: "view-toggle", role: "group", "aria-label": "Task view" }, modeButtons);
  const doneBox = h("details", { class: "done-tasks", "data-role": "done" });

  // The sections that depend on the task list. Quick add calls this again after it saves.
  // The List | Board toggle swaps what draws into `openBox` and hides the Done fold in Board.
  // `focusId` puts focus back on that card's status select after a keyboard change redraws.
  function draw(focusId = null) {
    const board = mode === "board" && wide.matches;
    for (const button of modeButtons) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    mount(sectionTitle, board ? "Board" : "Open tasks");
    doneBox.hidden = board;
    const open = tasks.filter((t) => t.status !== "done").sort(byEffectiveDue);
    const done = tasks.filter((t) => t.status === "done");
    mount(
      summary,
      `Owner: ${ownerLabel(organizers, ws.owner)} · ${done.length} of ${tasks.length} done`,
    );
    if (board) {
      mount(openBox, boardView(tasks, organizers, draw));
      if (focusId !== null) openBox.querySelector(`.board-card[data-id="${focusId}"] select`)?.focus();
    } else {
      mount(
        openBox,
        open.length
          ? h("ul", { class: "task-list" }, open.map((t) => taskRow(t, organizers)))
          : h("p", { class: "muted" }, "No open tasks."),
      );
    }
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
    h("section", { class: "ws-section" }, h("div", { class: "section-head" }, sectionTitle, toggle), openBox),
    addForm,
    contactSection,
    doneBox,
  );
  // Crossing 900px swaps List and Board. The listener ends itself once the screen is gone.
  const onWidth = () => (openBox.isConnected ? draw() : wide.removeEventListener("change", onWidth));
  wide.addEventListener("change", onWidth);
}
