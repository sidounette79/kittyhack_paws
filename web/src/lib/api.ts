// 07.10, Sid: shared across Chronology/Presence/PresenceCat so the "session
// expired" handling doesn't drift between them. WebAuthMiddleware serves
// the HTML login page body (status 200, not JSON) for an unauthenticated
// fetch to any /app-api/v1/* route - res.ok is true but res.json() throws.
// A real API error (unknown rfid, bad filter, ...) is a proper non-2xx
// JSON error response instead, which SessionExpiredError does NOT cover -
// callers can tell the two apart and handle a 404 differently from a
// session that needs a reload to show the login form.
export class SessionExpiredError extends Error {}

export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin", ...init });
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new SessionExpiredError();
  }
  if (!res.ok) {
    throw new Error(typeof body === "object" && body && "detail" in body ? String((body as any).detail) : "request failed");
  }
  return body as T;
}
