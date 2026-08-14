export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fieldPaths: string[];
  readonly witness: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    fieldPaths: string[],
    witness: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fieldPaths = fieldPaths;
    this.witness = witness;
  }
}

export const SESSION_EXPIRED_EVENT = "ratereplay:session-expired";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  const body = (await response.json().catch(() => null)) as
    | (T & {
        code?: string;
        message?: string;
        field_paths?: string[];
        witness?: Record<string, unknown>;
      })
    | null;
  if (!response.ok) {
    if (
      response.status === 401 &&
      path !== "/v1/auth/session" &&
      path !== "/v1/auth/login" &&
      path !== "/v1/auth/register"
    ) {
      window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
    }
    throw new ApiError(
      response.status,
      body?.code ?? "REQUEST_FAILED",
      body?.message ?? "RateReplay could not complete that request.",
      body?.field_paths ?? [],
      body?.witness ?? {},
    );
  }
  return body as T;
}
