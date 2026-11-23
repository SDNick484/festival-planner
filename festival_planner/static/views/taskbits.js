// Small pieces shared by the Workstream and Task screens.
import { h } from "../dom.js";
import * as api from "../api.js";

export const STATUSES = [
  { id: "todo", label: "To do" },
  { id: "doing", label: "Doing" },
  { id: "waiting", label: "Waiting" },
  { id: "blocked", label: "Blocked" },
  { id: "done", label: "Done" },
];

export const statusLabel = (id) => STATUSES.find((s) => s.id === id)?.label ?? id;

// Every row of a list endpoint, following the server's page size (limit is capped at 500).
export async function fetchAll(path) {
  const PAGE = 500;
  const joiner = path.includes("?") ? "&" : "?";
  const rows = [];
  for (;;) {
    const batch = await api.get(`${path}${joiner}limit=${PAGE}&offset=${rows.length}`);
    rows.push(...batch);
    if (batch.length < PAGE) return rows;
  }
}

// The organizers as {email: display name}, for owner labels and the owner select.
export async function loadOrganizers() {
  return api.get("/api/organizers");
}

export function ownerLabel(organizers, email) {
  if (!email) return "Unassigned";
  return organizers.find((o) => o.email === email)?.display_name || email;
}

// "2099-09-10" -> "Sep 10" (with the year when it is not this year). Dates are calendar days, so
// they are built from their parts and never shifted by a time zone.
export function formatDay(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  const opts = { month: "short", day: "numeric" };
  if (y !== new Date().getFullYear()) opts.year = "numeric";
  return date.toLocaleDateString(undefined, opts);
}

// Open tasks first by effective due date (undated last), then by id.
export function byEffectiveDue(a, b) {
  const da = a.effective_due;
  const db = b.effective_due;
  if (da && db && da !== db) return da < db ? -1 : 1;
  if (da && !db) return -1;
  if (!da && db) return 1;
  return a.id - b.id;
}

// A message for a failed request that is not tied to one field.
export function errorText(error) {
  return error instanceof api.ApiError ? error.message : "Something went wrong. Try again.";
}

export function statusDot(status) {
  return h("span", { class: `dot status-${status}`, role: "img", "aria-label": statusLabel(status) });
}
