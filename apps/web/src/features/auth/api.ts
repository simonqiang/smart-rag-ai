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

export async function getJson(url: string): Promise<ApiResult> {
  const response = await fetch(url);
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  return { ok: response.ok, status: response.status, payload };
}

export async function postJson(url: string, body: unknown): Promise<ApiResult> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  return { ok: response.ok, status: response.status, payload };
}

export async function putJson(url: string, body: unknown): Promise<ApiResult> {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  return { ok: response.ok, status: response.status, payload };
}

export async function deleteJson(url: string): Promise<ApiResult> {
  const response = await fetch(url, { method: "DELETE" });
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  return { ok: response.ok, status: response.status, payload };
}

export async function postForm(url: string, body: FormData): Promise<ApiResult> {
  const response = await fetch(url, { method: "POST", body });
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  return { ok: response.ok, status: response.status, payload };
}

export function detailText(result: ApiResult): string {
  const detail = result.payload.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as Record<string, unknown>).message);
  }
  return "request failed";
}
