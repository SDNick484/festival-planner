// Budget (#/budget): the net so far against the goal, totals by kind and state, items grouped by
// workstream, and forms to add and edit items and to set the net goal. GET /api/budget feeds it.
//
// Money is integer cents end to end. Typed amounts are parsed from their digits (never through a
// float), and display goes through Intl.NumberFormat with the instance's currency, which the API
// sends as `currency`. A cost's states read estimated, committed and paid; they are stored as
// estimated, pledged and received like a revenue item's.
import { h, mount } from "../dom.js";
import * as api from "../api.js";

const KINDS = ["revenue", "cost"];
const STATES = ["estimated", "pledged", "received"];
const STATE_WORDS = {
  revenue: { estimated: "estimated", pledged: "pledged", received: "received" },
  cost: { estimated: "estimated", pledged: "committed", received: "paid" },
};
const MAX_CENTS = 10 ** 12;
// The server's field names -> the names of this screen's fields.
const FIELD_NAMES = { amount_cents: "amount", net_goal_cents: "goal" };
const NO_WORKSTREAM = "";

export function formatMoney(cents, currency) {
  const amount = cents / 100;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency}`;
  }
}

// "12.50" -> { cents: 1250 }; anything else -> { error }. Parsed from the digits, not as a float.
export function parseAmount(raw, { blank = "Enter an amount." } = {}) {
  const text = raw.trim();
  if (text === "") return { error: blank };
  if (text.startsWith("-")) return { error: "Amounts cannot be negative." };
  if (/^\d*\.\d{3,}$/.test(text)) return { error: "Use at most two decimal places." };
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(text);
  if (!match) return { error: "Enter an amount such as 12.50." };
  const [, whole, fraction = ""] = match;
  if (whole.replace(/^0+/, "").length > 10) return { error: "That amount is too large." };
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return cents > MAX_CENTS ? { error: "That amount is too large." } : { cents };
}

function plainAmount(cents) {
  return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

function capital(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function errorText(error) {
  return error instanceof api.ApiError ? error.message : "Something went wrong. Try again.";
}

// --- form plumbing -------------------------------------------------------------------------

function field(label, name, input, wide = false) {
  return h(
    "label",
    { class: wide ? "mg-field mg-wide" : "mg-field", "data-field": name },
    h("span", { class: "mg-label" }, label),
    input,
    h("span", { class: "mg-error", role: "alert" }),
  );
}

const statusLine = () => h("p", { class: "mg-message", role: "status", "data-state": "idle" });

function say(form, state, words) {
  const box = form.querySelector(".mg-message");
  box.dataset.state = state;
  box.textContent = words;
}

function setFieldError(form, name, words) {
  const wrapper = form.querySelector(`[data-field='${name}']`);
  if (!wrapper) return false;
  wrapper.querySelector(".mg-error").textContent = words;
  wrapper.classList.toggle("mg-invalid", words !== "");
  return true;
}

function clearErrors(form) {
  for (const wrapper of form.querySelectorAll("[data-field]")) setFieldError(form, wrapper.dataset.field, "");
  say(form, "idle", "");
}

function showApiError(form, error) {
  const fields = error instanceof api.ApiError ? error.fields : {};
  const unplaced = [];
  for (const [name, words] of Object.entries(fields)) {
    if (!setFieldError(form, FIELD_NAMES[name] ?? name, words)) unplaced.push(words);
  }
  say(form, "error", unplaced.length ? unplaced.join(" ") : Object.keys(fields).length ? "Check the highlighted fields." : errorText(error));
}

// Runs `action` with the form's buttons disabled. Resolves to its result, or undefined when the
// server refused it (the message is shown and the input is left as typed).
async function submit(form, action) {
  const buttons = [...form.querySelectorAll("button")];
  buttons.forEach((b) => (b.disabled = true));
  clearErrors(form);
  try {
    return await action();
  } catch (error) {
    if (!(error instanceof api.ApiError)) throw error;
    showApiError(form, error);
    return undefined;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function button(label, attrs, onClick) {
  return h("button", { type: "button", class: "mg-btn", ...attrs, on: { click: onClick } }, label);
}

function kindSelect(value) {
  return h("select", { name: "kind" }, KINDS.map((kind) => h("option", { value: kind, selected: kind === value }, capital(kind))));
}

function stateSelect(kind, value) {
  const select = h("select", { name: "state" });
  fillStates(select, kind, value);
  return select;
}

function fillStates(select, kind, value) {
  const keep = value ?? select.value ?? "estimated";
  mount(select, STATES.map((state) => h("option", { value: state, selected: state === keep }, capital(STATE_WORDS[kind][state]))));
}

function workstreamSelect(workstreams, value) {
  return h(
    "select",
    { name: "workstream_id" },
    h("option", { value: NO_WORKSTREAM }, "No workstream"),
    workstreams.map((ws) => h("option", { value: String(ws.id), selected: ws.id === value }, ws.name)),
  );
}

// The fields shared by the add form and an item's edit form. Returns the controls and a `read()`
// that gives the request body, or null (after showing the amount problem) when it cannot be sent.
function itemFields(form, item, workstreams) {
  const kind = kindSelect(item?.kind ?? "cost");
  const state = stateSelect(kind.value, item?.state ?? "estimated");
  kind.addEventListener("change", () => fillStates(state, kind.value));
  const label = h("input", { type: "text", name: "label", value: item?.label ?? "", required: true, maxlength: 200, autocomplete: "off" });
  const amount = h("input", { type: "text", name: "amount", value: item ? plainAmount(item.amount_cents) : "", inputmode: "decimal", autocomplete: "off", placeholder: "0.00" });
  const workstream = workstreamSelect(workstreams, item?.workstream_id ?? null);
  const notes = h("textarea", { name: "notes", rows: "2", maxlength: 20000 }, item?.notes ?? "");
  const nodes = [
    field("Label", "label", label, true),
    field("Kind", "kind", kind),
    field("Amount", "amount", amount),
    field("State", "state", state),
    field("Workstream", "workstream_id", workstream),
    field("Notes", "notes", notes, true),
  ];
  const read = () => {
    const parsed = parseAmount(amount.value);
    if (parsed.error) {
      setFieldError(form, "amount", parsed.error);
      say(form, "error", "Check the highlighted fields.");
      return null;
    }
    return {
      label: label.value.trim(),
      kind: kind.value,
      amount_cents: parsed.cents,
      state: state.value,
      workstream_id: workstream.value === NO_WORKSTREAM ? null : Number(workstream.value),
      notes: notes.value,
    };
  };
  return { nodes, read, controls: { label, amount, notes } };
}

// --- summary -------------------------------------------------------------------------------

function pctText(pct) {
  return `${Number.isInteger(pct) ? pct : pct.toFixed(1)}% of goal`;
}

function summaryCard(data) {
  const { summary, currency } = data;
  const money = (cents) => formatMoney(cents, currency);
  const pct = summary.pct_of_goal;
  const card = h(
    "section",
    { class: "bg-summary", "data-section": "summary", "aria-label": "Budget summary" },
    h(
      "div",
      { class: "bg-figures" },
      h("div", { class: "bg-figure" }, h("span", { class: "bg-caption muted" }, "Projected net"), h("strong", { class: "bg-big", "data-role": "net" }, money(summary.net_projected))),
      h("div", { class: "bg-figure" }, h("span", { class: "bg-caption muted" }, "Net goal"), h("strong", { class: "bg-big", "data-role": "goal" }, summary.goal === null ? "No goal set" : money(summary.goal))),
      h("div", { class: "bg-figure" }, h("span", { class: "bg-caption muted" }, "Actual net (received less paid)"), h("strong", { "data-role": "actual" }, money(summary.net_actual))),
    ),
  );
  if (pct !== null) {
    const shown = Math.max(0, Math.min(100, Math.round(pct)));
    card.append(
      h("progress", { class: "bg-bar", max: "100", value: String(shown), "data-role": "bar", "aria-label": `Projected net: ${pctText(pct)}` }),
      h("p", { class: "bg-pct muted", "data-role": "pct" }, pctText(pct)),
    );
  }
  card.append(
    h(
      "div",
      { class: "bg-totals" },
      KINDS.map((kind) =>
        h(
          "div",
          { class: "bg-total", "data-kind": kind },
          h("span", { class: "bg-total-kind" }, capital(kind)),
          STATES.map((state) => h("span", { class: "bg-total-state" }, `${STATE_WORDS[kind][state]} `, h("strong", null, money(summary[kind][state])))),
        ),
      ),
    ),
  );
  return card;
}

function goalForm(data, redraw) {
  const { summary, currency } = data;
  const goal = h("input", { type: "text", name: "goal", value: summary.goal === null ? "" : plainAmount(summary.goal), inputmode: "decimal", autocomplete: "off", placeholder: "No goal" });
  const form = h(
    "form",
    { class: "mg-form bg-goal", "data-form": "goal", novalidate: true },
    field(`Net goal (${currency}); leave empty for none`, "goal", goal),
    h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Save goal"), statusLine()),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearErrors(form);
    const parsed = goal.value.trim() === "" ? { cents: null } : parseAmount(goal.value);
    if (parsed.error) {
      setFieldError(form, "goal", parsed.error);
      return;
    }
    const saved = await submit(form, () => api.put("/api/event", { net_goal_cents: parsed.cents }));
    if (saved) await redraw();
  });
  return form;
}

// --- items ---------------------------------------------------------------------------------

function itemRow(item, data, workstreams, redraw, open) {
  const money = formatMoney(item.amount_cents, data.currency);
  const form = h("form", { class: "mg-form", "data-form": "edit-item", novalidate: true });
  const { nodes, read } = itemFields(form, item, workstreams);
  const slot = h("span", { class: "mg-delete" });
  const idle = () => mount(slot, button("Delete", { class: "mg-btn mg-danger-quiet" }, ask));
  const ask = () =>
    mount(
      slot,
      h(
        "span",
        { class: "mg-confirm", role: "group" },
        h("span", { class: "mg-confirm-text wrap" }, `Delete "${item.label}"?`),
        button("Confirm delete", { class: "mg-btn mg-danger" }, async () => {
          const gone = await submit(form, async () => {
            await api.del(`/api/budget/items/${item.id}`);
            return true;
          });
          if (gone) await redraw();
          else idle();
        }),
        button("Cancel", {}, idle),
      ),
    );
  idle();
  mount(form, nodes, h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Save"), slot, statusLine()));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearErrors(form);
    const body = read();
    if (!body) return;
    const saved = await submit(form, () => api.patch(`/api/budget/items/${item.id}`, body));
    if (saved) {
      open.delete(item.id);
      await redraw();
    }
  });
  const row = h(
    "details",
    { class: "mg-item bg-item", "data-item": "", "data-id": item.id, "data-kind": item.kind, open: open.has(item.id) },
    h(
      "summary",
      null,
      h("span", { class: "bg-label truncate" }, item.label),
      h("span", { class: "bg-state muted" }, STATE_WORDS[item.kind][item.state]),
      h("span", { class: "bg-amount" }, money),
    ),
    form,
  );
  row.addEventListener("toggle", () => (row.open ? open.add(item.id) : open.delete(item.id)));
  return row;
}

// Groups follow the workstream order; items whose workstream is gone or unset go last.
function groups(items, workstreams) {
  const known = new Set(workstreams.map((ws) => ws.id));
  const out = workstreams.map((ws) => ({ name: ws.name, items: items.filter((i) => i.workstream_id === ws.id) }));
  out.push({ name: "No workstream", items: items.filter((i) => !known.has(i.workstream_id)) });
  return out.filter((g) => g.items.length);
}

function itemsSection(data, workstreams, redraw, open) {
  const section = h("section", { class: "bg-items", "data-section": "items" }, h("h2", null, "Items"));
  if (!data.items.length) {
    section.append(h("p", { class: "muted empty" }, "No budget items yet. Add the first one below."));
    return section;
  }
  for (const group of groups(data.items, workstreams)) {
    section.append(
      h(
        "div",
        { class: "bg-group", "data-group": "" },
        h("h3", null, group.name),
        group.items.map((item) => itemRow(item, data, workstreams, redraw, open)),
      ),
    );
  }
  return section;
}

function addForm(workstreams, redraw) {
  const form = h("form", { class: "mg-form bg-add", "data-form": "add-item", novalidate: true });
  const { nodes, read, controls } = itemFields(form, null, workstreams);
  mount(form, nodes, h("div", { class: "mg-actions" }, h("button", { type: "submit", class: "mg-btn mg-primary" }, "Add item"), statusLine()));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearErrors(form);
    const body = read();
    if (!body) return;
    const added = await submit(form, () => api.post("/api/budget/items", body));
    if (!added) return;
    controls.label.value = "";
    controls.amount.value = "";
    controls.notes.value = "";
    await redraw();
    say(form, "ok", "Added");
  });
  return h("section", { class: "bg-add-section", "data-section": "add" }, h("h2", null, "Add an item"), form);
}

// --- screen --------------------------------------------------------------------------------

export async function render(root) {
  let workstreams;
  let data;
  try {
    [data, workstreams] = await Promise.all([api.get("/api/budget"), api.get("/api/workstreams?limit=500")]);
  } catch (error) {
    mount(root, h("h1", null, "Budget"), h("p", { class: "muted" }, `Could not load the budget. ${errorText(error)}`));
    return;
  }
  const open = new Set();
  const live = h("div", { class: "bg-live" });
  const redraw = async () => {
    const fresh = await api.get("/api/budget");
    mount(live, summaryCard(fresh), goalForm(fresh, redraw), itemsSection(fresh, workstreams, redraw, open));
  };
  mount(live, summaryCard(data), goalForm(data, redraw), itemsSection(data, workstreams, redraw, open));
  mount(
    root,
    h("div", { class: "bg-top" }, h("h1", null, "Budget"), h("a", { class: "manage-link", href: "#/" }, "Home")),
    live,
    addForm(workstreams, redraw),
    h("p", null, h("a", { class: "mg-btn mg-export", href: "/api/export/budget.csv", download: "" }, "Export CSV")),
  );
}
