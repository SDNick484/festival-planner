// Manage (#/manage): edit the event, and add, edit, reorder and delete workstreams, milestones and
// contacts. The screen is a stack of independent sections, one builder per section (see SECTIONS
// below); each loads its own data and redraws only itself after a change.
//
// Saving: a failed save keeps what was typed and shows the server's message next to the field
// ({"error", "fields"}). Deleting asks for an inline confirmation in the row, never window.confirm.
import { h, mount } from "../dom.js";
import * as api from "../api.js";

const PAGE = 500;
const NO_WORKSTREAM = "";

// section id -> redraw function, so one section can refresh another (a renamed workstream
// changes the contact form's select).
const refreshers = new Map();
// Rows that were just saved (they show "Saved" once after the redraw) and contact items the
// user has open (they stay open across redraws).
const saved = new Set();
const opened = new Set();

async function fetchAll(path) {
  const out = [];
  for (;;) {
    const batch = await api.get(`${path}?limit=${PAGE}&offset=${out.length}`);
    out.push(...batch);
    if (batch.length < PAGE) return out;
  }
}

// --- small building blocks -----------------------------------------------------------------

function text(error) {
  return error instanceof api.ApiError ? error.message : "Something went wrong. Try again.";
}

function control(tag, name, attrs, ...children) {
  return h(tag, { name, ...attrs }, ...children);
}

// A labelled field. `name` is the API field the server's error for it is keyed by.
function field(label, name, input, wide = false) {
  return h(
    "label",
    { class: wide ? "mg-field mg-wide" : "mg-field", "data-field": name },
    h("span", { class: "mg-label" }, label),
    input,
    h("span", { class: "mg-error", role: "alert" }),
  );
}

function message(form, state, words) {
  const box = form.querySelector(".mg-message");
  box.dataset.state = state;
  box.textContent = words;
}

function showErrors(form, error) {
  const fields = error instanceof api.ApiError ? error.fields : {};
  const shown = new Set();
  for (const wrapper of form.querySelectorAll("[data-field]")) {
    const words = fields[wrapper.dataset.field] ?? "";
    wrapper.querySelector(".mg-error").textContent = words;
    wrapper.classList.toggle("mg-invalid", words !== "");
    if (words) shown.add(wrapper.dataset.field);
  }
  // Problems with a field this form does not show (a workstream that still has tasks) go in the
  // message, so nothing the server said is lost.
  const other = Object.entries(fields).filter(([name]) => !shown.has(name)).map(([, words]) => words);
  const words = other.length ? other.join(" ") : shown.size ? "Check the highlighted fields." : text(error);
  message(form, "error", words);
}

// Runs `action` for a form: disables its buttons meanwhile, clears old errors, and reports the
// outcome. Resolves to the action's result, or undefined if it failed (the input is untouched).
async function submit(form, action) {
  const buttons = [...form.querySelectorAll("button")];
  buttons.forEach((b) => (b.disabled = true));
  message(form, "idle", "");
  for (const wrapper of form.querySelectorAll("[data-field]")) {
    wrapper.querySelector(".mg-error").textContent = "";
    wrapper.classList.remove("mg-invalid");
  }
  try {
    return await action();
  } catch (error) {
    if (!(error instanceof api.ApiError)) throw error;
    showErrors(form, error);
    return undefined;
  } finally {
    // A redraw may have replaced the form; re-enable what is left of it. Buttons that start out
    // disabled (the first row cannot move up) are put back by their own `can` check on redraw.
    buttons.forEach((b) => (b.disabled = b.hasAttribute("data-locked")));
  }
}

function button(label, attrs, onClick) {
  return h("button", { type: "button", class: "mg-btn", ...attrs, on: { click: onClick } }, label);
}

// Delete with an inline confirmation: "Delete" turns into "Delete <name>? Confirm delete / Cancel".
// `remove` does the delete and `then` redraws. A refusal (a workstream that still has tasks) is
// shown in the row's message and leaves the item in place.
function deleteControl(getForm, label, remove, then) {
  const slot = h("span", { class: "mg-delete" });
  const idle = () =>
    mount(slot, button("Delete", { class: "mg-btn mg-danger-quiet" }, () => ask()));
  const ask = () =>
    mount(
      slot,
      h(
        "span",
        { class: "mg-confirm", role: "group", "data-confirm": "" },
        h("span", { class: "mg-confirm-text wrap" }, `Delete "${label}"?`),
        button("Confirm delete", { class: "mg-btn mg-danger" }, confirm),
        button("Cancel", {}, () => {
          message(getForm(), "idle", "");
          idle();
        }),
      ),
    );
  const confirm = async () => {
    const result = await submit(getForm(), async () => {
      await remove();
      return true;
    });
    if (result) await then();
    else idle();
  };
  idle();
  return slot;
}

function section(id, title, build) {
  const body = h("div", { class: "mg-body" });
  const el = h("section", { class: "mg-section", "data-section": id, "aria-labelledby": `mg-${id}` }, h("h2", { id: `mg-${id}` }, title), body);
  const refresh = async () => {
    try {
      mount(body, await build(refresh));
    } catch (error) {
      console.error(error);
      mount(body, h("p", { class: "mg-message", "data-state": "error" }, `Could not load this section. ${text(error)}`));
    }
  };
  refreshers.set(id, refresh);
  return { el, refresh };
}

// Builds a section and loads it. `build(refresh)` returns the section's content; `refresh`
// redraws the section from fresh data.
async function built(id, title, build) {
  const s = section(id, title, build);
  await s.refresh();
  return s.el;
}

const statusLine = () => h("p", { class: "mg-message", role: "status", "data-state": "idle" });

// "Saved" appears in a row once, right after its save.
function flash(form, key) {
  if (saved.delete(key)) message(form, "ok", "Saved");
}

function integer(value) {
  return value.trim() === "" ? null : Number(value);
}

function blankToNull(value) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function minusDays(iso, days) {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

// --- event ---------------------------------------------------------------------------------

function timeZones() {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

async function eventSection() {
  return built("event", "Event", async () => {
    const event = await api.get("/api/event");
    const name = control("input", "name", { type: "text", value: event.name, required: true, maxlength: 200, autocomplete: "off" });
    const date = control("input", "event_date", { type: "date", value: event.event_date, required: true });
    const zone = control("input", "timezone", { type: "text", value: event.timezone, required: true, list: "mg-zones", autocomplete: "off", autocapitalize: "off" });
    const form = h(
      "form",
      { class: "mg-form", "data-form": "event", novalidate: true },
      field("Name", "name", name, true),
      field("Date", "event_date", date),
      field("Time zone", "timezone", zone),
      h("datalist", { id: "mg-zones" }, timeZones().map((tz) => h("option", { value: tz }))),
      h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Save event"), statusLine()),
    );
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const result = await submit(form, () =>
        api.put("/api/event", { name: name.value.trim(), event_date: date.value, timezone: zone.value.trim() }),
      );
      if (!result) return;
      name.value = result.name;
      date.value = result.event_date;
      zone.value = result.timezone;
      message(form, "ok", "Saved");
      // Milestone dates follow the event date.
      refreshers.get("milestones")?.();
    });
    return form;
  });
}

// --- workstreams ---------------------------------------------------------------------------

// Reordering renumbers every workstream 0, 10, 20, ... in the new order and saves the ones whose
// sort_order changed, so equal or gappy numbers from earlier edits cannot leave it ambiguous.
async function moveWorkstream(list, index, delta) {
  const order = [...list];
  const [item] = order.splice(index, 1);
  order.splice(index + delta, 0, item);
  for (const [position, ws] of order.entries()) {
    if (ws.sort_order !== position * 10) await api.patch(`/api/workstreams/${ws.id}`, { sort_order: position * 10 });
  }
}

function workstreamRow(ws, list, index, redraw) {
  const key = `workstream-${ws.id}`;
  const name = control("input", "name", { type: "text", value: ws.name, required: true, maxlength: 200, autocomplete: "off" });
  const archived = control("input", "archived", { type: "checkbox", checked: ws.archived });
  const move = async (delta) => {
    const done = await submit(form, async () => {
      await moveWorkstream(list, index, delta);
      return true;
    });
    if (done) await redraw();
  };
  const up = button("↑", { "aria-label": `Move ${ws.name} up`, title: "Move up", "data-locked": index === 0, disabled: index === 0 }, () => move(-1));
  const down = button("↓", { "aria-label": `Move ${ws.name} down`, title: "Move down", "data-locked": index === list.length - 1, disabled: index === list.length - 1 }, () => move(1));
  const form = h(
    "form",
    { class: "mg-form mg-row", "data-item": "workstream", "data-id": ws.id, "data-name": ws.name, novalidate: true },
    field("Name", "name", name, true),
    h("label", { class: "mg-check", "data-field": "archived" }, archived, h("span", null, "Archived"), h("span", { class: "mg-error", role: "alert" })),
    h(
      "div",
      { class: "mg-actions" },
      h("button", { type: "submit", class: "mg-btn mg-primary" }, "Save"),
      h("span", { class: "mg-move" }, up, down),
      deleteControl(() => form, ws.name, () => api.del(`/api/workstreams/${ws.id}`), redraw),
      statusLine(),
    ),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = await submit(form, () => api.patch(`/api/workstreams/${ws.id}`, { name: name.value.trim(), archived: archived.checked }));
    if (!result) return;
    saved.add(key);
    await redraw();
  });
  flash(form, key);
  return form;
}

async function workstreamsSection() {
  return built("workstreams", "Workstreams", async (refresh) => {
    const list = await fetchAll("/api/workstreams");
    const redraw = async () => {
      await refresh();
      await refreshers.get("contacts")?.();
    };
    const name = control("input", "name", { type: "text", required: true, maxlength: 200, autocomplete: "off" });
    const add = h(
      "form",
      { class: "mg-form mg-add", "data-form": "add-workstream", novalidate: true },
      field("Name", "name", name, true),
      h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Add workstream"), statusLine()),
    );
    add.addEventListener("submit", async (e) => {
      e.preventDefault();
      // New workstreams go to the end of the list.
      const last = list.reduce((top, ws) => Math.max(top, ws.sort_order), -10);
      const result = await submit(add, () => api.post("/api/workstreams", { name: name.value.trim(), sort_order: last + 10 }));
      if (result) await redraw();
    });
    return [
      list.length ? h("ul", { class: "mg-list" }, list.map((ws, i) => h("li", null, workstreamRow(ws, list, i, redraw)))) : h("p", { class: "muted" }, "No workstreams yet."),
      add,
    ];
  });
}

// --- milestones ----------------------------------------------------------------------------

function milestoneRow(stone, eventDate, redraw) {
  const key = `milestone-${stone.id}`;
  const name = control("input", "name", { type: "text", value: stone.name, required: true, maxlength: 200, autocomplete: "off" });
  const days = control("input", "days_before_event", { type: "number", value: stone.days_before_event, min: 0, step: 1, inputmode: "numeric" });
  const form = h(
    "form",
    { class: "mg-form mg-row", "data-item": "milestone", "data-id": stone.id, "data-name": stone.name, "data-days": stone.days_before_event, novalidate: true },
    field("Name", "name", name, true),
    field("Days before", "days_before_event", days),
    h("p", { class: "muted mg-derived" }, `Date: ${minusDays(eventDate, stone.days_before_event)}`),
    h(
      "div",
      { class: "mg-actions" },
      h("button", { type: "submit", class: "mg-btn mg-primary" }, "Save"),
      deleteControl(() => form, stone.name, () => api.del(`/api/milestones/${stone.id}`), redraw),
      statusLine(),
    ),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = await submit(form, () => api.patch(`/api/milestones/${stone.id}`, { name: name.value.trim(), days_before_event: integer(days.value) }));
    if (!result) return;
    saved.add(key);
    await redraw();
  });
  flash(form, key);
  return form;
}

async function milestonesSection() {
  return built("milestones", "Milestones", async (refresh) => {
    const [event, list] = await Promise.all([api.get("/api/event"), fetchAll("/api/milestones")]);
    const name = control("input", "name", { type: "text", required: true, maxlength: 200, autocomplete: "off" });
    const days = control("input", "days_before_event", { type: "number", min: 0, step: 1, inputmode: "numeric" });
    const add = h(
      "form",
      { class: "mg-form mg-add", "data-form": "add-milestone", novalidate: true },
      field("Name", "name", name, true),
      field("Days before", "days_before_event", days),
      h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Add milestone"), statusLine()),
    );
    add.addEventListener("submit", async (e) => {
      e.preventDefault();
      const result = await submit(add, () => api.post("/api/milestones", { name: name.value.trim(), days_before_event: integer(days.value) }));
      if (result) await refresh();
    });
    return [
      h("p", { class: "muted" }, "A milestone's date is the event date minus its days before the event."),
      list.length ? h("ul", { class: "mg-list" }, list.map((stone) => h("li", null, milestoneRow(stone, event.event_date, refresh)))) : h("p", { class: "muted" }, "No milestones yet."),
      add,
    ];
  });
}

// --- contacts ------------------------------------------------------------------------------

const splitEmails = (value) => value.split(/[\s,;]+/).filter(Boolean);

// The fields shared by the add form and an item's edit form. Returns the inputs and a reader that
// gives the request body.
function contactFields(contact, workstreams) {
  const inputs = {
    name: control("input", "name", { type: "text", value: contact?.name, required: true, maxlength: 200, autocomplete: "off" }),
    organization: control("input", "organization", { type: "text", value: contact?.organization, maxlength: 200, autocomplete: "off" }),
    role: control("input", "role", { type: "text", value: contact?.role, maxlength: 200, autocomplete: "off" }),
    emails: control("input", "emails", { type: "text", value: (contact?.emails ?? []).join(", "), inputmode: "email", autocomplete: "off", placeholder: "one@example.com, two@example.com" }),
    phone: control("input", "phone", { type: "tel", value: contact?.phone, maxlength: 50, autocomplete: "off" }),
    workstream_id: control(
      "select",
      "workstream_id",
      {},
      h("option", { value: NO_WORKSTREAM }, "No workstream"),
      workstreams.map((ws) => h("option", { value: ws.id, selected: contact?.workstream_id === ws.id }, ws.name)),
    ),
    notes: control("textarea", "notes", { rows: 3, maxlength: 20000 }, contact?.notes ?? ""),
  };
  const nodes = [
    field("Name", "name", inputs.name, true),
    field("Organization", "organization", inputs.organization),
    field("Role", "role", inputs.role),
    field("Emails", "emails", inputs.emails, true),
    field("Phone", "phone", inputs.phone),
    field("Workstream", "workstream_id", inputs.workstream_id),
    field("Notes", "notes", inputs.notes, true),
  ];
  const read = () => ({
    name: inputs.name.value.trim(),
    organization: blankToNull(inputs.organization.value),
    role: blankToNull(inputs.role.value),
    emails: splitEmails(inputs.emails.value),
    phone: blankToNull(inputs.phone.value),
    workstream_id: inputs.workstream_id.value === NO_WORKSTREAM ? null : Number(inputs.workstream_id.value),
    notes: inputs.notes.value,
  });
  return { nodes, read };
}

function contactItem(contact, workstreams, redraw) {
  const key = `contact-${contact.id}`;
  const { nodes, read } = contactFields(contact, workstreams);
  const where = workstreams.find((ws) => ws.id === contact.workstream_id);
  const form = h(
    "form",
    { class: "mg-form", novalidate: true },
    nodes,
    h(
      "div",
      { class: "mg-actions" },
      h("button", { type: "submit", class: "mg-btn mg-primary" }, "Save"),
      deleteControl(() => form, contact.name, () => api.del(`/api/contacts/${contact.id}`), redraw),
      statusLine(),
    ),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const result = await submit(form, () => api.patch(`/api/contacts/${contact.id}`, read()));
    if (!result) return;
    saved.add(key);
    await redraw();
  });
  flash(form, key);
  const details = h(
    "details",
    { class: "mg-item", "data-item": "contact", "data-id": contact.id, "data-name": contact.name, open: opened.has(key) },
    h(
      "summary",
      null,
      h("span", { class: "mg-summary-name wrap" }, contact.name),
      h("span", { class: "muted mg-summary-note wrap" }, [contact.organization, contact.role, where?.name].filter(Boolean).join(" · ")),
    ),
    form,
  );
  details.addEventListener("toggle", () => (details.open ? opened.add(key) : opened.delete(key)));
  return details;
}

async function contactsSection() {
  return built("contacts", "Contacts", async (refresh) => {
    const [list, workstreams] = await Promise.all([fetchAll("/api/contacts"), fetchAll("/api/workstreams")]);
    const { nodes, read } = contactFields(null, workstreams);
    const add = h(
      "form",
      { class: "mg-form", "data-form": "add-contact", novalidate: true },
      nodes,
      h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Add contact"), statusLine()),
    );
    add.addEventListener("submit", async (e) => {
      e.preventDefault();
      const result = await submit(add, () => api.post("/api/contacts", read()));
      if (result) await refresh();
    });
    return [
      list.length ? h("ul", { class: "mg-list" }, list.map((c) => h("li", null, contactItem(c, workstreams, refresh)))) : h("p", { class: "muted" }, "No contacts yet."),
      h("details", { class: "mg-item mg-add-details" }, h("summary", null, "Add contact"), add),
    ];
  });
}

// --- the screen ----------------------------------------------------------------------------

// One builder per section, one per line, in the order they appear. Each builder returns its
// section element and refreshes itself. To add a section, write its builder above and add a line
// here: nothing else changes.
const SECTIONS = [
  eventSection,
  workstreamsSection,
  milestonesSection,
  contactsSection,
  // --- add new sections above this line (#24 Share links, #25 Export) ---
];

export async function render(root, params) {
  refreshers.clear();
  saved.clear();
  opened.clear();
  const sections = await Promise.all(SECTIONS.map((build) => build()));
  mount(root, h("h1", null, "Manage"), sections);
}
