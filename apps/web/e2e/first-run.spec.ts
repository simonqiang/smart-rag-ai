import { expect, test, type Page } from "@playwright/test";

type Check = { name: string; ok: boolean; detail: string; remediation?: string };

const allOk: Check[] = [
  { name: "database", ok: true, detail: "reachable" },
  { name: "redis", ok: true, detail: "reachable" },
  { name: "storage", ok: true, detail: "writable" },
  { name: "ollama", ok: true, detail: "qwen3:8b + bge-m3 available" },
];

const statusRoute = "**/api/setup/status";

function statusBody(checks: Check[], initialized = false) {
  return { initialized, dependencies: checks };
}

async function mockJson(page: Page, url: string, body: unknown, status = 200) {
  await page.route(url, (route) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }),
  );
}

test("first-run setup succeeds and signs in the owner", async ({ page }) => {
  await mockJson(page, statusRoute, statusBody(allOk));
  await page.route("**/api/setup/owner", (route) =>
    route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ workspace_id: "w1", user_id: "u1" }),
    }),
  );

  await page.goto("/setup");
  await page.getByLabel("Owner email").fill("owner@example.com");
  await page.getByLabel("Password (at least 10 characters)").fill("owner-password-1");
  await page.getByLabel("Repeat password").fill("owner-password-1");
  await page.getByRole("button", { name: "Create owner account" }).click();

  await expect(page.getByRole("heading", { name: "Workspace ready" })).toBeVisible();
});

test("unavailable database shows remediation and setup resumes after retry", async ({
  page,
}) => {
  const failing: Check[] = [
    {
      name: "database",
      ok: false,
      detail: "ConnectionRefusedError",
      remediation: "run: make up (starts PostgreSQL)",
    },
    ...allOk.slice(1),
  ];
  await mockJson(page, statusRoute, statusBody(failing));
  await page.goto("/setup");

  await expect(page.getByText("fix: run: make up (starts PostgreSQL)")).toBeVisible();
  expect(await page.getByLabel("Owner email").count()).toBe(0);

  await mockJson(page, statusRoute, statusBody(allOk));
  await page.getByRole("button", { name: "Retry checks" }).click();
  await expect(page.getByLabel("Owner email")).toBeVisible();
});

test("unwritable storage shows actionable remediation", async ({ page }) => {
  const failing: Check[] = [
    ...allOk.slice(0, 2),
    {
      name: "storage",
      ok: false,
      detail: "unwritable: Permission denied",
      remediation: "grant write access to SMART_RAG_DATA_DIR (/data)",
    },
    allOk[3],
  ];
  await mockJson(page, statusRoute, statusBody(failing));
  await page.goto("/setup");

  await expect(
    page.getByText("fix: grant write access to SMART_RAG_DATA_DIR (/data)"),
  ).toBeVisible();
});

test("missing Ollama model shows the pull remediation", async ({ page }) => {
  const failing: Check[] = [
    ...allOk.slice(0, 3),
    {
      name: "ollama",
      ok: false,
      detail: "model(s) missing: qwen3:8b",
      remediation: "pull the models: ollama pull qwen3:8b && ollama pull bge-m3",
    },
  ];
  await mockJson(page, statusRoute, statusBody(failing));
  await page.goto("/setup");

  await expect(
    page.getByText("fix: pull the models: ollama pull qwen3:8b && ollama pull bge-m3"),
  ).toBeVisible();
});

test("initialized workspace refuses a second owner with recovery hint", async ({
  page,
}) => {
  await mockJson(page, statusRoute, statusBody(allOk, true));
  await page.goto("/setup");

  await expect(page.getByText("already initialized")).toBeVisible();
  expect(await page.getByLabel("Owner email").count()).toBe(0);
});

test("setup owner form surfaces the dependency failure from a 503", async ({ page }) => {
  await mockJson(page, statusRoute, statusBody(allOk));
  await page.route("**/api/setup/owner", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          message: "setup dependencies unavailable (redis); fix and retry",
          checks: [
            { name: "redis", ok: false, detail: "ConnectionRefusedError", remediation: "run: make up (starts Redis)" },
          ],
        },
      }),
    }),
  );
  await page.goto("/setup");
  await page.getByLabel("Owner email").fill("owner@example.com");
  await page.getByLabel("Password (at least 10 characters)").fill("owner-password-1");
  await page.getByLabel("Repeat password").fill("owner-password-1");
  await page.getByRole("button", { name: "Create owner account" }).click();

  await expect(
    page.getByText("setup dependencies unavailable (redis); fix and retry"),
  ).toBeVisible();
});

test("sign-in with a wrong password shows a generic error", async ({ page }) => {
  await page.route("**/api/session", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "invalid email or password (or account temporarily locked)",
      }),
    }),
  );
  await page.goto("/signin");
  await page.getByLabel("Email").fill("owner@example.com");
  await page.getByLabel("Password (at least 10 characters)").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByText(/invalid email or password/)).toBeVisible();
});

test("successful sign-in opens the Ask workspace and app navigation", async ({ page }) => {
  await page.route("**/api/session", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ user_id: "u1", expires_at: "2026-10-07T00:00:00Z" }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u1",
        workspace_id: "w1",
        email: "owner@example.com",
        role: "owner",
        must_change_password: false,
      }),
    });
  });
  await page.route("**/api/sources", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/conversations", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );

  await page.goto("/signin");
  await page.getByLabel("Email").fill("owner@example.com");
  await page.getByLabel("Password (at least 10 characters)").fill("owner-password-123");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/ask$/);
  await expect(page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Sources" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Ask/i })).toBeVisible();
});

test("invitation acceptance works and rejects invalid tokens generically", async ({
  page,
}) => {
  await page.route("**/api/session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u2",
        workspace_id: "w1",
        email: "member@example.com",
        role: "member",
        must_change_password: false,
      }),
    }),
  );
  await page.route("**/api/sources", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/conversations", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.goto("/signin");
  await page.getByText("I have an invitation token").click();

  let attempts = 0;
  await page.route("**/api/invitations/accept", (route) => {
    attempts += 1;
    const token = JSON.parse(route.request().postData() ?? "{}").token;
    const status = token === "good-token" ? 201 : 400;
    const body =
      status === 201
        ? JSON.stringify({ user_id: "u2", workspace_id: "w1" })
        : JSON.stringify({ detail: "invitation is invalid, expired, or already used" });
    void route.fulfill({ status, contentType: "application/json", body });
    return;
  });

  await page.getByLabel("Invitation token").fill("bad-token");
  await page.getByLabel("Password (at least 10 characters)").fill("member-pass-11");
  await page.getByRole("button", { name: "Accept invitation" }).click();
  await expect(page.getByText(/invalid, expired, or already used/)).toBeVisible();

  await page.getByLabel("Invitation token").fill("good-token");
  await page.getByLabel("Password (at least 10 characters)").fill("member-pass-11");
  await page.getByRole("button", { name: "Accept invitation" }).click();
  await expect(page).toHaveURL(/\/ask$/);
  expect(attempts).toBe(2);
});

test("temporary password forces a change before use", async ({ page }) => {
  await page.route("**/api/session", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ user_id: "u1", expires_at: "2026-10-07T00:00:00Z" }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u1",
        workspace_id: "w1",
        email: "member@example.com",
        role: "member",
        must_change_password: true,
      }),
    });
  });

  await page.goto("/signin");
  await page.getByLabel("Email").fill("member@example.com");
  await page.getByLabel("Password (at least 10 characters)").fill("temp-pass-99");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(
    page.getByRole("heading", { name: "Choose a new password" }),
  ).toBeVisible();
});
