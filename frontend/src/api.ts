export class ApiError extends Error {
  status: number;
  code: string;
  fields: Record<string, string | string[]>;
  constructor(status: number, body: any) {
    super(
      body?.error?.message ||
        (status === 503
          ? "Сервис временно недоступен. Повторите запрос."
          : "Не удалось выполнить запрос. Попробуйте ещё раз."),
    );
    this.status = status;
    this.code = body?.error?.code || "REQUEST_FAILED";
    this.fields = body?.error?.field_errors || {};
  }
}
let csrfToken = "";
export async function ensureCsrf() {
  const r = await fetch("/api/v1/auth/csrf", { credentials: "same-origin" });
  if (!r.ok) throw new ApiError(r.status, await r.json().catch(() => null));
  csrfToken = (await r.json()).csrf_token;
}
function cookieCsrf() {
  return decodeURIComponent(
    document.cookie
      .split("; ")
      .find((s) => s.startsWith("csrftoken="))
      ?.split("=")
      .slice(1)
      .join("=") || csrfToken,
  );
}
export async function api<T>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    etag?: string;
    key?: string;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const method = options.method || "GET";
  if (method !== "GET" && !cookieCsrf()) await ensureCsrf();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRFToken"] = cookieCsrf();
  if (options.etag) headers["If-Match"] = options.etag;
  if (options.key) headers["Idempotency-Key"] = options.key;
  let response: Response;
  try {
    response = await fetch("/api/v1" + path, {
      method,
      headers,
      credentials: "same-origin",
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new Error(
      "Нет связи с сервером. Проверьте подключение и повторите действие.",
    );
  }
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    if (
      response.status === 401 &&
      !["/auth/login", "/auth/me", "/auth/logout"].includes(path)
    )
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, body);
  }
  return body as T;
}
export const orgApi = (id: string) => `/orgs/${id}`;
export const instructionApi = (org: string, id: string) =>
  `${orgApi(org)}/instructions/${id}`;
export const libraryPath = (org: string) => `/o/${org}/instructions`;
export const instructionPath = (org: string, id: string) =>
  `${libraryPath(org)}/${id}`;
export const requestKey = () => crypto.randomUUID();

/** A binary upload reports sent bytes only; media preparation is a separate job. */
export async function uploadVideo<T>(
  path: string,
  file: File,
  onProgress: (loaded: number, total: number) => void,
  signal: AbortSignal,
): Promise<T> {
  if (!cookieCsrf()) await ensureCsrf();
  if (signal.aborted) throw new DOMException("Отменено", "AbortError");
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const cleanup = () => signal.removeEventListener("abort", abort);
    xhr.open("POST", "/api/v1" + path);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-CSRFToken", cookieCsrf());
    xhr.upload.onprogress = (event) =>
      onProgress(
        event.loaded,
        event.lengthComputable ? event.total : file.size,
      );
    xhr.onload = () => {
      cleanup();
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* Normalized below. */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else {
        if (xhr.status === 401)
          window.dispatchEvent(new Event("session-expired"));
        reject(new ApiError(xhr.status, body));
      }
    };
    xhr.onerror = () => {
      cleanup();
      reject(
        new Error(
          "Передача прервалась. Файл не загружен полностью. Повторите загрузку с начала.",
        ),
      );
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Отменено", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
    xhr.send(file);
  });
}

export async function uploadFrame<T>(
  path: string,
  image: Blob,
  sequence: number,
  timestampMs: number,
  key: string,
  signal: AbortSignal,
): Promise<T> {
  if (!cookieCsrf()) await ensureCsrf();
  const response = await fetch("/api/v1" + path, {
    method: "POST",
    credentials: "same-origin",
    signal,
    body: image,
    headers: {
      Accept: "application/json",
      "Content-Type": "image/jpeg",
      "X-CSRFToken": cookieCsrf(),
      "Idempotency-Key": key,
      "X-Frame-Sequence": String(sequence),
      "X-Timestamp-Ms": String(timestampMs),
    },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, body);
  }
  return body as T;
}
