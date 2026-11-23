// Inbox screen: one card per captured email that has not been triaged (design §6.4). Each card
// shows the subject, sender, the organizer's forward note (highlighted), a 200-character preview,
// attachment names and the workstream the server suggests, with three actions: file it to an
// existing task (a searchable picker), start a new task from it, or dismiss it.
//
// Everything in an email is untrusted text: it reaches the page only through h(), as text nodes.
import { h, mount } from "../dom.js";
import * as api from "../api.js";
import { errorText, fetchAll } from "./taskbits.js";

const PICKER_LIMIT = 20;
const TITLE_MAX = 200; // the service's title limit; a longer subject is cut the same way

// --- pieces shared with the Task screen -----------------------------------------------------

function sentOn(iso) {
  const when = iso ? new Date(iso) : null;
  if (!when || Number.isNaN(when.getTime())) return "";
  return when.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// "From a@example.org · Jan 6, 2098 · via organizer-a@example.com".
export function emailMeta(email) {
  const parts = [`From ${email.from_addr || "unknown sender"}`, sentOn(email.sent_at)];
  if (email.captured_by) parts.push(`via ${email.captured_by}`);
  return h("p", { class: "email-meta muted wrap" }, parts.filter(Boolean).join(" · "));
}

// The organizer's one-line note, set apart from the email's own text. Null when there is none.
export function forwardNote(email) {
  if (!email.forward_note) return null;
  return h(
    "blockquote",
    { class: "forward-note" },
    h("span", { class: "forward-note-label" }, email.captured_by ? `Note from ${email.captured_by}` : "Note"),
    h("p", { class: "forward-note-text wrap" }, email.forward_note),
  );
}

// A button that shows or hides the whole email as plain text. The body is fetched on first open
// (lists carry only a preview) and rendered as a text node inside <pre>.
export function bodyViewer(email, label = "Show full text") {
  const panel = h("div", { class: "email-body-panel", hidden: true });
  let full = null;
  const button = h(
    "button",
    {
      type: "button",
      class: "btn btn-small email-toggle",
      "aria-expanded": "false",
      on: {
        click: async () => {
          if (button.getAttribute("aria-expanded") === "true") {
            button.setAttribute("aria-expanded", "false");
            button.textContent = label;
            panel.hidden = true;
            return;
          }
          if (full === null) {
            button.disabled = true;
            try {
              full = await api.get(`/api/emails/${email.id}`);
            } catch (error) {
              mount(panel, h("p", { class: "field-error", role: "alert" }, errorText(error)));
              panel.hidden = false;
              return;
            } finally {
              button.disabled = false;
            }
          }
          mount(
            panel,
            full.body_text
              ? h("pre", { class: "email-body" }, full.body_text)
              : h("p", { class: "muted" }, "The text of this email was removed after the retention period."),
          );
          panel.hidden = false;
          button.setAttribute("aria-expanded", "true");
          button.textContent = "Hide text";
        },
      },
    },
    label,
  );
  return h("div", { class: "email-viewer" }, button, panel);
}

// --- the screen -----------------------------------------------------------------------------

export async function render(root, params) {
  const [items, workstreams] = await Promise.all([api.get("/api/inbox"), fetchAll("/api/workstreams")]);
  new InboxScreen(root, items, workstreams).draw();
}

class InboxScreen {
  constructor(root, items, workstreams) {
    Object.assign(this, { root, items, workstreams });
    this.remaining = items.length;
  }

  draw() {
    this.status = h("p", { class: "inbox-status", role: "status", tabindex: "-1" });
    this.empty = h("p", { class: "inbox-empty muted" }, "Nothing to triage. New email will show up here.");
    this.list = h(
      "ul",
      { class: "inbox-list", "aria-label": "Emails to triage" },
      this.items.map((item) => new EmailCard(this, item).el),
    );
    this.empty.hidden = this.remaining > 0;
    mount(this.root, h("h1", null, "Inbox"), this.status, this.list, this.empty);
  }

  // A card is done (filed or dismissed): take it off the list and say what happened.
  resolved(card, ...message) {
    card.el.remove();
    this.remaining -= 1;
    this.empty.hidden = this.remaining > 0;
    mount(this.status, ...message);
    this.status.focus({ preventScroll: true });
  }
}

class EmailCard {
  constructor(screen, email) {
    Object.assign(this, { screen, email });
    this.panelToken = 0;
    this.build();
  }

  workstreamName(id) {
    return this.screen.workstreams.find((w) => w.id === id)?.name;
  }

  build() {
    const { email } = this;
    const suggestion = this.workstreamName(email.suggested_workstream_id);
    this.error = h("p", { class: "field-error", role: "alert" });
    this.panel = h("div", { class: "inbox-panel", hidden: true });
    this.actions = [
      h("button", { type: "button", class: "btn btn-primary", on: { click: () => this.openPicker() } }, "File to task…"),
      h("button", { type: "button", class: "btn", on: { click: () => this.openNewTask() } }, "New task"),
      h("button", { type: "button", class: "btn", on: { click: () => this.dismiss() } }, "Dismiss"),
    ];
    this.el = h(
      "li",
      { class: "inbox-card", "data-email-id": email.id },
      h("h2", { class: "inbox-subject wrap" }, email.subject || "(no subject)"),
      emailMeta(email),
      forwardNote(email),
      email.preview
        ? h("p", { class: "inbox-preview wrap" }, email.preview_truncated ? `${email.preview}…` : email.preview)
        : null,
      email.preview_truncated ? bodyViewer(email) : null,
      email.attachment_names.length
        ? h(
            "ul",
            { class: "attachments", "aria-label": "Attachments" },
            email.attachment_names.map((name) => h("li", { class: "attachment wrap" }, name)),
          )
        : null,
      suggestion ? h("p", { class: "suggest-chip wrap" }, `Suggested: ${suggestion}`) : null,
      h("div", { class: "inbox-actions" }, this.actions),
      this.panel,
      this.error,
    );
  }

  // Run one request with every button off, so a double tap cannot file or create twice.
  async busy(work) {
    mount(this.error, "");
    for (const button of this.el.querySelectorAll("button")) button.disabled = true;
    try {
      return await work();
    } catch (error) {
      mount(this.error, errorText(error));
      return undefined;
    } finally {
      if (this.el.isConnected) for (const button of this.el.querySelectorAll("button")) button.disabled = false;
    }
  }

  closePanel() {
    this.panelToken += 1; // a search still in flight must not write into the next panel
    this.panel.hidden = true;
    mount(this.panel, "");
  }

  async dismiss() {
    this.closePanel();
    const done = await this.busy(() => api.post(`/api/inbox/${this.email.id}/dismiss`));
    if (done) this.screen.resolved(this, `Dismissed “${this.email.subject || "(no subject)"}”.`);
  }

  // --- file to an existing task ---

  openPicker() {
    this.closePanel();
    const input = h("input", {
      type: "text",
      id: `picker-${this.email.id}`,
      autocomplete: "off",
      placeholder: "Search by title or notes",
    });
    const results = h("ul", { class: "picker-results", "aria-label": "Matching tasks" });
    this.pickerResults = results;
    input.addEventListener("input", () => this.search(input.value.trim(), results));
    mount(
      this.panel,
      h("label", { for: input.id }, "Search tasks"),
      input,
      results,
      h("div", { class: "prompt-actions" }, h("button", { type: "button", class: "btn", on: { click: () => this.closePanel() } }, "Cancel")),
    );
    this.panel.hidden = false;
    input.focus();
    this.search("", results);
  }

  async search(text, results) {
    const token = ++this.panelToken;
    let tasks;
    try {
      tasks = await api.get(`/api/tasks?limit=${PICKER_LIMIT}${text ? `&q=${encodeURIComponent(text)}` : ""}`);
    } catch (error) {
      if (token === this.panelToken) mount(results, h("li", { class: "field-error", role: "alert" }, errorText(error)));
      return;
    }
    if (token !== this.panelToken) return; // typed on, or closed, since this was asked
    mount(
      results,
      tasks.length
        ? tasks.map((task) =>
            h(
              "li",
              null,
              h(
                "button",
                { type: "button", class: "picker-result", "data-task-id": task.id, on: { click: () => this.fileTo(task) } },
                h("span", { class: "picker-title wrap" }, task.title),
                h("span", { class: "picker-meta muted wrap" }, this.workstreamName(task.workstream_id) ?? ""),
              ),
            ),
          )
        : h("li", { class: "muted" }, "No matching tasks."),
    );
  }

  async fileTo(task) {
    const done = await this.busy(() => api.post(`/api/inbox/${this.email.id}/file`, { task_id: task.id }));
    if (done) {
      this.screen.resolved(this, "Filed to ", h("a", { href: `#/task/${task.id}`, class: "wrap" }, task.title), ".");
    }
  }

  // --- start a new task from the email ---

  openNewTask() {
    this.closePanel();
    const prefill = this.email.subject.trim().slice(0, TITLE_MAX).trim();
    const id = `new-${this.email.id}`;
    const select = h("select", { id: `${id}-ws` }, [
      h("option", { value: "" }, "Choose a workstream…"),
      ...this.screen.workstreams.map((ws) => h("option", { value: ws.id }, ws.archived ? `${ws.name} (archived)` : ws.name)),
    ]);
    const suggested = this.email.suggested_workstream_id;
    if (suggested && this.workstreamName(suggested)) select.value = String(suggested);
    const title = h("input", { type: "text", id: `${id}-title`, autocomplete: "off", maxlength: TITLE_MAX });
    title.value = prefill;
    const create = async () => {
      if (!select.value) {
        mount(this.error, "Choose a workstream for the task.");
        select.focus();
        return;
      }
      const body = { workstream_id: Number(select.value) };
      if (title.value.trim() !== prefill) body.title = title.value.trim();
      const row = await this.busy(() => api.post(`/api/inbox/${this.email.id}/new-task`, body));
      if (row) {
        this.screen.resolved(this, "Created task ", h("a", { href: `#/task/${row.task_id}`, class: "wrap" }, body.title ?? (prefill || "(no subject)")), ".");
      }
    };
    title.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        create();
      }
    });
    mount(
      this.panel,
      h("div", { class: "field" }, h("label", { for: select.id }, "Workstream"), select),
      h("div", { class: "field" }, h("label", { for: title.id }, "Task title"), title),
      h(
        "div",
        { class: "prompt-actions" },
        h("button", { type: "button", class: "btn btn-primary", on: { click: create } }, "Create task"),
        h("button", { type: "button", class: "btn", on: { click: () => this.closePanel() } }, "Cancel"),
      ),
    );
    this.panel.hidden = false;
    title.focus();
  }
}
