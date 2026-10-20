import { expect, test, type Page } from "@playwright/test";

const stateRoute = "**/api/settings/ai-provider";
const testRoute = "**/api/settings/ai-provider/test";
const activateRoute = "**/api/settings/ai-provider/activate";
const sessionRoute = "**/api/session";

const ENDPOINT = "https://glm.example/v1";
const MODEL = "glm-4";
const CONSENT_VERSION = "2026-10-20";

const defaultState = {
  provider: "ollama",
  endpoint: null,
  model: null,
  rewrite_enabled: false,
  consent_version: null,
  consented_at: null,
  consent_version_current: CONSENT_VERSION,
};

const glmState = {
  provider: "glm",
  endpoint: ENDPOINT,
  model: MODEL,
  rewrite_enabled: true,
  consent_version: CONSENT_VERSION,
  consented_at: "2026-10-21T08:05:00+00:00",
  consent_version_current: CONSENT_VERSION,
};

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function watchConsole(page: Page): Promise<string[]> {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(String(error)));
  page.on("console", (message) => {
    // The browser logs the HTTP status of deliberately mocked 4xx/5xx probe
    // responses; only app-level errors are problems.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      problems.push(message.text());
    }
  });
  return problems;
}

function mockState(page: Page, body: object = defaultState) {
  return page.route(stateRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }),
  );
}

function mockSession(page: Page, role: "owner" | "member") {
  return page.route(sessionRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ role }) }),
  );
}

test("the owner tests the connection, consents, and activates GLM", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockState(page);
  await mockSession(page, "owner");
  await page.route(testRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }),
  );
  await page.route(activateRoute, (route) => {
    const payload = JSON.parse(route.request().postData() ?? "{}");
    expect(payload.endpoint).toBe(ENDPOINT);
    expect(payload.model).toBe(MODEL);
    expect(payload.consent_version).toBe(CONSENT_VERSION);
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(glmState),
    });
  });
  await page.goto("/settings/ai-provider");

  await expect(page.getByRole("heading", { name: "AI provider" })).toBeVisible();
  await expect(page.getByTestId("provider-summary")).toContainText("locally by Ollama");
  // The consent checkbox is bound to the version the server requires.
  await expect(page.getByText(`(version ${CONSENT_VERSION})`)).toBeVisible();

  const activate = page.getByRole("button", { name: "Activate GLM" });
  await page.getByLabel("Endpoint URL").fill(ENDPOINT);
  await page.getByLabel("Model").fill(MODEL);
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByRole("status")).toContainText("Connection test passed");
  await expect(activate).toBeDisabled(); // tested, but consent not yet given

  await page
    .getByText(/I accept the GLM data-processing terms/)
    .getByRole("checkbox")
    .check();
  await expect(activate).toBeEnabled();
  await activate.click();

  await expect(page.getByTestId("provider-summary")).toContainText(`GLM at ${ENDPOINT}`);
  await expect(page.getByRole("button", { name: "Revert to local Ollama" })).toBeVisible();
  expect(problems).toEqual([]);
  await expectNoHorizontalOverflow(page);
});

test("a rejected API key shows an actionable error and blocks activation", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockState(page);
  await mockSession(page, "owner");
  await page.route(testRoute, (route) =>
    route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { reason: "invalid_key", message: "GLM probe failed: the GLM endpoint rejected the API key" },
      }),
    }),
  );
  await page.goto("/settings/ai-provider");

  await page.getByLabel("Endpoint URL").fill(ENDPOINT);
  await page.getByLabel("Model").fill(MODEL);
  await page.getByRole("button", { name: "Test connection" }).click();

  await expect(page.getByRole("alert")).toContainText("rejected the API key");
  await expect(page.getByRole("button", { name: "Activate GLM" })).toBeDisabled();
  expect(problems).toEqual([]);
  await expectNoHorizontalOverflow(page);
});

test("an endpoint outage shows an actionable error", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockState(page);
  await mockSession(page, "owner");
  await page.route(testRoute, (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { reason: "unreachable", message: "GLM probe failed: the GLM endpoint is unreachable" },
      }),
    }),
  );
  await page.goto("/settings/ai-provider");

  await page.getByLabel("Endpoint URL").fill(ENDPOINT);
  await page.getByLabel("Model").fill(MODEL);
  await page.getByRole("button", { name: "Test connection" }).click();

  await expect(page.getByRole("alert")).toContainText("endpoint is unreachable");
  expect(problems).toEqual([]);
  await expectNoHorizontalOverflow(page);
});

test("a member sees the provider state but not the owner form", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockState(page, glmState);
  await mockSession(page, "member");
  await page.goto("/settings/ai-provider");

  await expect(page.getByTestId("provider-summary")).toContainText("GLM at");
  await expect(page.getByRole("heading", { name: "Switch generation to GLM" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Activate GLM" })).toHaveCount(0);
  expect(problems).toEqual([]);
  await expectNoHorizontalOverflow(page);
});
