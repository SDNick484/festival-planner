// Task screen: status chips, fields that save as you leave them, and the activity history
// (design §6.3). Every save is a PATCH of just the changed field; a refused save shows the
// server's message under that field and leaves what the user typed where it is.
import { h, mount } from "../dom.js";
import * as api from "../api.js";
import { STATUSES, errorText, fetchAll, formatDay, loadOrganizers, ownerLabel } from "./taskbits.js";
import { bodyViewer, emailMeta, forwardNote } from "./inbox.js";

const FIELD_LABELS = {
  title: "title",
  owner: "owner",
  status: "status",
  due_date: "due_date",
  milestone_id: "milestone",
  workstream_id: "workstream",
  waiting_on: "waiting_on",
  blocked_reason: "blocked_reason",
};
const SKIP_IN_SUMMARY = new Set(["done_at"]);

export async function render(root, params) {
  const id = Number(params.id);
  let task;
  try {
    task = await api.get(`/api/tasks/${id}`);
  } catch (error) {
    if (error instanceof api.ApiError && error.status === 404) {
      mount(
        root,
        h("h1", null, "Task not found"),
        h("p", { class: "muted" }, "It may have been deleted."),
        h("a", { href: "#/ws" }, "All workstreams"),
      );
      return;
    }
    throw error;
  }
  const [ws, organizers, milestones, ev] = await Promise.all([
    api.get(`/api/workstreams/${task.workstream_id}`),
    loadOrganizers(),
    fetchAll("/api/milestones"),
    api.get("/api/event"),
  ]);
  new TaskScreen(root, { task, ws, organizers, milestones, timezone: ev.timezone }).draw();
}

// --- activity text --------------------------------------------------------------------------

function short(value) {
  if (value === null || value === undefined || value === "") return "none";
  const text = String(value);
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}

// "status: todo → doing; owner: none → organizer-b@example.com". Long text is only named.
export function summarize(item) {
  if (item.action === "create") return "created";
  if (item.action === "delete") return "deleted";
  if (item.action === "note") return `note: ${short(item.note)}`;
  const parts = [];
  for (const [key, change] of Object.entries(item.diff ?? {})) {
    if (SKIP_IN_SUMMARY.has(key)) continue;
    if (key === "notes") parts.push("notes edited");
    else if (key === "links") parts.push("links changed");
    else if (FIELD_LABELS[key]) parts.push(`${FIELD_LABELS[key]}: ${short(change[0])} → ${short(change[1])}`);
    else parts.push(`${key} changed`);
  }
  return parts.join("; ") || "updated";
}

function stamp(iso, timezone) {
  const when = new Date(iso);
  const opts = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  try {
    return when.toLocaleString(undefined, { ...opts, timeZone: timezone });
  } catch {
    return when.toLocaleString(undefined, opts);
  }
}

// --- the screen -----------------------------------------------------------------------------

class TaskScreen {
  constructor(root, { task, ws, organizers, milestones, timezone }) {
    Object.assign(this, { root, task, ws, organizers, milestones, timezone });
    this.emails = task.emails ?? []; // only GET returns them, so keep them apart from `task`, which a PATCH replaces
    this.queue = Promise.resolve(); // saves run one at a time, in the order they were made
    this.activityToken = 0;
    this.errors = {}; // field name -> its <p class="field-error">
    this.controls = {}; // field name -> the input, select or textarea
  }

  // A <p> under a field for the server's message (role=alert, so it is announced).
  errorSlot(name) {
    const slot = h("p", { class: "field-error", role: "alert" });
    this.errors[name] = slot;
    return slot;
  }

  // While the blocked prompt is open, the status and reason messages belong to it.
  setError(name, message) {
    const inPrompt = this.prompt && !this.prompt.hidden && (name === "status" || name === "blocked_reason");
    mount(inPrompt ? this.promptError : (this.errors[name] ?? this.generalError), message ?? "");
  }

  // PATCH `changes`; resolves true when the server accepted it. On a refusal, each message is
  // shown under its field (or in the general slot) and nothing the user typed is touched.
  save(changes, errorName) {
    const run = this.queue.then(async () => {
      for (const name of [...Object.keys(changes), errorName]) this.setError(name, "");
      try {
        this.task = await api.patch(`/api/tasks/${this.task.id}`, changes);
      } catch (error) {
        if (error instanceof api.ApiError && Object.keys(error.fields).length) {
          for (const [name, message] of Object.entries(error.fields)) this.setError(name === "_" ? errorName : name, message);
        } else {
          this.setError(errorName, errorText(error));
        }
        return false;
      }
      this.sync(Object.keys(changes));
      this.loadActivity();
      return true;
    });
    this.queue = run;
    return run;
  }

  // Bring the screen in line with the saved task. Only the fields just saved (and the ones the
  // server derives from the status) are rewritten, so a half-typed value elsewhere is safe.
  sync(names) {
    const { task } = this;
    const touched = new Set([...names, "status", "blocked_reason", "due_date"]);
    for (const name of touched) {
      const control = this.controls[name];
      if (control && control !== document.activeElement) control.value = task[name] ?? "";
    }
    mount(this.heading, task.title);
    for (const button of this.chipButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.status === task.status));
    }
    this.reasonRow.hidden = task.status !== "blocked";
    if (task.status !== "blocked") this.controls.blocked_reason.value = "";
    this.hidePrompt();
    mount(this.dueHint, this.dueHintText());
    mount(this.linkList, this.linkItems());
  }

  dueHintText() {
    const { task } = this;
    if (task.due_date || !task.effective_due) return "";
    const stone = this.milestones.find((m) => m.id === task.milestone_id);
    return `Due ${formatDay(task.effective_due)}${stone ? ` (milestone ${stone.name})` : ""}`;
  }

  // A text-like field that saves when the user leaves it. `nullable` turns "" into null.
  textField(name, label, { nullable = false, textarea = false } = {}) {
    const control = h(textarea ? "textarea" : "input", {
      id: `f-${name}`,
      ...(textarea ? { rows: 5 } : { type: "text", autocomplete: "off" }),
    });
    control.value = this.task[name] ?? "";
    control.addEventListener("blur", () => {
      const value = textarea ? control.value : control.value.trim();
      const next = nullable && value === "" ? null : value;
      if (next === (this.task[name] ?? (nullable ? null : ""))) {
        this.setError(name, "");
        return;
      }
      this.save({ [name]: next });
    });
    this.controls[name] = control;
    return h("div", { class: `field field-${name.replace("_", "-")}` }, h("label", { for: `f-${name}` }, label), control, this.errorSlot(name));
  }

  ownerField() {
    const { task, organizers } = this;
    const known = new Set(organizers.map((o) => o.email));
    const options = [h("option", { value: "" }, "Unassigned")];
    for (const o of organizers) options.push(h("option", { value: o.email }, o.display_name || o.email));
    if (task.owner && !known.has(task.owner)) options.push(h("option", { value: task.owner }, task.owner));
    const select = h("select", { id: "f-owner" }, options);
    select.value = task.owner ?? "";
    select.addEventListener("change", () => this.save({ owner: select.value || null }));
    this.controls.owner = select;
    return h("div", { class: "field field-owner" }, h("label", { for: "f-owner" }, "Owner"), select, this.errorSlot("owner"));
  }

  dueField() {
    const input = h("input", { type: "date", id: "f-due_date" });
    input.value = this.task.due_date ?? "";
    input.addEventListener("change", () => {
      const next = input.value || null;
      if (next !== this.task.due_date) this.save({ due_date: next });
    });
    this.controls.due_date = input;
    this.dueHint = h("p", { class: "muted hint" });
    mount(this.dueHint, this.dueHintText());
    return h("div", { class: "field field-due-date" }, h("label", { for: "f-due_date" }, "Due date"), input, this.dueHint, this.errorSlot("due_date"));
  }

  // --- status chips and the inline blocked prompt ---

  chipRow() {
    this.chipButtons = STATUSES.map((s) =>
      h(
        "button",
        {
          type: "button",
          class: `chip status-${s.id}`,
          "data-status": s.id,
          "aria-pressed": String(s.id === this.task.status),
          on: { click: () => this.pickStatus(s.id) },
        },
        s.label,
      ),
    );
    return h("div", { class: "chip-row", role: "group", "aria-label": "Status" }, this.chipButtons);
  }

  pickStatus(status) {
    if (status === this.task.status) {
      if (status === "blocked") this.controls.blocked_reason.focus();
      return;
    }
    if (status === "blocked") {
      this.showPrompt();
      return;
    }
    this.hidePrompt();
    this.save({ status }, "status");
  }

  blockedPrompt() {
    this.promptInput = h("input", { type: "text", id: "blocked-prompt-reason", autocomplete: "off" });
    this.promptError = h("p", { class: "field-error", role: "alert" });
    const submit = async () => {
      const reason = this.promptInput.value.trim();
      mount(this.promptError, "");
      const saved = await this.save({ status: "blocked", blocked_reason: reason }, "status");
      if (saved) this.hidePrompt();
    };
    this.prompt = h(
      "div",
      { class: "blocked-prompt", hidden: true },
      h("label", { for: "blocked-prompt-reason" }, "Why is it blocked?"),
      this.promptInput,
      h(
        "div",
        { class: "prompt-actions" },
        h("button", { type: "button", class: "btn btn-primary", on: { click: submit } }, "Save"),
        h("button", { type: "button", class: "btn", on: { click: () => this.hidePrompt() } }, "Cancel"),
      ),
      this.promptError,
    );
    this.promptInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
    return this.prompt;
  }

  showPrompt() {
    this.prompt.hidden = false;
    this.promptInput.value = "";
    mount(this.promptError, "");
    this.promptInput.focus();
  }

  hidePrompt() {
    if (this.prompt) this.prompt.hidden = true;
  }

  // --- links ---

  linkItems() {
    return this.task.links.map((link, index) =>
      h(
        "li",
        { class: "link-item" },
        h("a", { class: "wrap", href: link.url, target: "_blank", rel: "noopener noreferrer" }, link.label || link.url),
        h(
          "button",
          {
            type: "button",
            class: "btn btn-small",
            "aria-label": `Remove link ${link.label || link.url}`,
            on: { click: () => this.save({ links: this.task.links.filter((_, i) => i !== index) }, "links") },
          },
          "Remove",
        ),
      ),
    );
  }

  linksField() {
    this.linkList = h("ul", { class: "link-list" }, this.linkItems());
    const label = h("input", { type: "text", id: "link-label", autocomplete: "off", "aria-label": "Link label", placeholder: "Label (optional)" });
    const url = h("input", { type: "url", id: "link-url", autocomplete: "off", "aria-label": "Link URL", placeholder: "https://…" });
    const add = async () => {
      const link = { label: label.value.trim(), url: url.value.trim() };
      const saved = await this.save({ links: [...this.task.links, link] }, "links");
      if (saved) {
        label.value = "";
        url.value = "";
      }
    };
    return h(
      "div",
      { class: "field field-links" },
      h("span", { class: "label" }, "Links"),
      this.linkList,
      h(
        "div",
        { class: "add-row link-add" },
        label,
        url,
        h("button", { type: "button", class: "btn", on: { click: add } }, "Add link"),
      ),
      this.errorSlot("links"),
    );
  }

  // --- linked emails ---
  // Emails filed to this task: who sent it, the organizer's note, and the text on demand. The
  // section stays hidden while there are none. Email text is untrusted and only ever a text node.
  linkedEmailsSection() {
    this.emailsSection = h(
      "section",
      { class: "task-section", "data-section": "linked-emails", hidden: this.emails.length === 0 },
      h("h2", null, "Linked emails"),
      h(
        "ul",
        { class: "email-list", "aria-label": "Linked emails" },
        this.emails.map((email) =>
          h(
            "li",
            { class: "email-item", "data-email-id": email.id },
            h("h3", { class: "email-subject wrap" }, email.subject || "(no subject)"),
            emailMeta(email),
            forwardNote(email),
            bodyViewer(email, "Show email text"),
          ),
        ),
      ),
    );
    return this.emailsSection;
  }

  // --- activity ---

  activitySection() {
    this.activityList = h("ul", { class: "activity", "aria-label": "Activity" });
    return h("section", { class: "task-section" }, h("h2", null, "Activity"), this.activityList);
  }

  async loadActivity() {
    const token = ++this.activityToken;
    let items;
    try {
      items = await api.get(`/api/activity?entity_type=task&entity_id=${this.task.id}&limit=50`);
    } catch {
      return; // the history is secondary: the screen still works without it
    }
    if (token !== this.activityToken) return; // a newer load is on its way
    mount(
      this.activityList,
      items.length
        ? items.map((item) =>
            h(
              "li",
              { class: "activity-item" },
              h("time", { datetime: item.at }, stamp(item.at, this.timezone)),
              h("span", { class: "activity-source" }, item.source),
              h("span", { class: "activity-actor wrap" }, item.actor),
              h("span", { class: "activity-what wrap" }, summarize(item)),
            ),
          )
        : h("li", { class: "muted" }, "No activity yet."),
    );
  }

  // --- the whole screen ---

  draw() {
    const { task, ws } = this;
    this.heading = h("h1", { class: "wrap" }, task.title);
    this.generalError = h("p", { class: "field-error", role: "alert" });
    const reason = this.textField("blocked_reason", "Blocked reason");
    this.reasonRow = reason;
    reason.hidden = task.status !== "blocked";

    mount(
      this.root,
      h("a", { class: "back wrap", href: `#/ws/${ws.id}` }, `← ${ws.name}`),
      this.heading,
      this.chipRow(),
      this.blockedPrompt(),
      this.generalError,
      h(
        "div",
        { class: "task-fields" },
        this.textField("title", "Title"),
        this.ownerField(),
        this.dueField(),
        this.textField("waiting_on", "Waiting on", { nullable: true }),
        reason,
        this.textField("notes", "Notes", { textarea: true }),
        this.linksField(),
      ),
      this.linkedEmailsSection(),
      this.activitySection(),
    );
    this.loadActivity();
  }
}
