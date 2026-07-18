/**
 * Typed fetch wrapper for the TradeMax REST API (prefix `/api`, proxied to the
 * FastAPI backend in dev — see vite.config.ts). Every non-2xx response is
 * turned into an ApiError carrying the Italian `detail` message the backend
 * always sends: `{"detail": "<messaggio in italiano>"}`.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const BASE_URL = "/api";

export type QueryParams = Record<string, string | number | boolean | undefined | null>;

function buildQueryString(params?: QueryParams): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) {
      usp.set(key, String(value));
    }
  }
  const serialized = usp.toString();
  return serialized ? `?${serialized}` : "";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, "Impossibile contattare il server. Verifica che il backend sia in esecuzione.");
  }

  if (res.status === 204) {
    return undefined as T;
  }

  const rawText = await res.text();
  let payload: unknown = undefined;
  if (rawText.length > 0) {
    try {
      payload = JSON.parse(rawText);
    } catch {
      payload = undefined;
    }
  }

  if (!res.ok) {
    let detail = `Errore del server (codice ${res.status}).`;
    if (payload && typeof payload === "object" && "detail" in payload) {
      const d = (payload as { detail?: unknown }).detail;
      if (typeof d === "string" && d.trim().length > 0) {
        detail = d;
      } else if (Array.isArray(d)) {
        // FastAPI 422 validation errors: [{loc, msg, type}, ...]
        const msgs = d
          .map((e) => (e && typeof e === "object" && "msg" in e ? String((e as { msg: unknown }).msg) : ""))
          .filter((m) => m.length > 0);
        if (msgs.length > 0) detail = `Dati non validi: ${msgs.join("; ")}`;
      }
    }
    throw new ApiError(res.status, detail);
  }

  return payload as T;
}

export function apiGet<T>(path: string, params?: QueryParams): Promise<T> {
  return request<T>(`${path}${buildQueryString(params)}`, { method: "GET" });
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiDelete<T = void>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

/** True when the error represents an HTTP 404 (resource not found). */
export function isNotFound(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 404;
}

/** True when the error represents an HTTP 409 (conflict, e.g. run already active). */
export function isConflict(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 409;
}

/** Extracts a user-facing Italian error message from any thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return "Si è verificato un errore imprevisto.";
}
