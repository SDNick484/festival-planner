// Same-origin JSON client for /api. Lists come back as bare arrays; a failure throws ApiError
// carrying the status and the server's per-field messages ({"error", "fields"}).

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
    throw new ApiError(0, {}, "Cannot reach the server");
  }
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
