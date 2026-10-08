export type DependencyCheck = {
  name: string;
  ok: boolean;
  detail: string;
  remediation?: string;
};

export type SetupStatus = {
  initialized: boolean;
  dependencies: DependencyCheck[];
};

export type ApiResult = {
  ok: boolean;
  status: number;
  payload: Record<string, unknown>;
};

async function parse(response: Response): Promise<ApiResult> {
  // A 200 response with a non-JSON body (e.g. a proxy fallback page) must not
  // masquerade as an empty success: list callers render payload with .map.
  const payload = (await response.json().catch(() => null)) as Record<string, unknown> | null;
  return { ok: response.ok && payload !== null, status: response.status, payload: payload ?? {} };
}

export async function getJson(url: string): Promise<ApiResult> {
  return parse(await fetch(url));
}

export async function postJson(url: string, body: unknown): Promise<ApiResult> {
  return parse(await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function putJson(url: string, body: unknown): Promise<ApiResult> {
  return parse(await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function deleteJson(url: string): Promise<ApiResult> {
  return parse(await fetch(url, { method: "DELETE" }));
}

export async function postForm(url: string, body: FormData): Promise<ApiResult> {
  return parse(await fetch(url, { method: "POST", body }));
}

export function detailText(result: ApiResult): string {
  const detail = result.payload.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as Record<string, unknown>).message);
  }
  return "request failed";
}
