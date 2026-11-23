// Same-origin JSON client for /api. Lists come back as bare arrays; a failure throws ApiError
// carrying the status and the server's per-field messages ({"error", "fields"}).
// A write that cannot reach the server fails with status 0 and the message "You're offline; change
// not saved", which the shell also shows as an alert: writes are never queued. A read that the
// service worker answered from its stored copy raises the "Offline: showing last saved" banner.
import { SERVED_HEADER, WRITE_FAILED_TEXT, noteRead, noteWriteFailed } from "./offline.js";

export class ApiError extends Error {
  constructor(status, fields, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fields = fields;
  }
}

async function request(method, path, body) {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) {
    throw new Error("api: path must be same-origin, like /api/tasks");
  }
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    if (method !== "GET") {
      noteWriteFailed();
      throw new ApiError(0, {}, WRITE_FAILED_TEXT);
    }
    throw new ApiError(0, {}, "Cannot reach the server");
  }
  if (method === "GET" && res.ok) noteRead(res.headers.get(SERVED_HEADER) === "1");
  let data = null;
  if (res.status !== 204) {
    try {
      data = await res.json();
    } catch {
      // Not JSON (a proxy error page, a sign-in redirect): handled below.
    }
  }
  if (!res.ok) {
    const fields = data && typeof data.fields === "object" && data.fields !== null ? data.fields : {};
    const message = data && typeof data.error === "string" ? data.error : `Request failed (${res.status})`;
    throw new ApiError(res.status, fields, message);
  }
  return data;
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body ?? {});
export const patch = (path, body) => request("PATCH", path, body ?? {});
export const del = (path) => request("DELETE", path);
export const put = (path, body) => request("PUT", path, body ?? {});
