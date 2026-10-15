import { expect, test, type Page } from "@playwright/test";

const retentionRoute = "**/api/settings/retention";

const defaults = { superseded_days: 30, conversation_days: 90 };

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function mockRetention(page: Page, status = 200, body: object = defaults) {
  return page.route(retentionRoute, (route) => {
    if (route.request().method() === "PUT") {
      void route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(status < 400 ? JSON.parse(route.request().postData() ?? "{}") : body),
      });
      return;
    }
    void route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("owner sees the defaults and saves new windows", async ({ page }) => {
  await mockRetention(page);
  await page.goto("/settings/retention");

  await expect(page.getByRole("heading", { name: "Retention" })).toBeVisible();
  const superseded = page.getByLabel(/superseded versions/i);
  const conversations = page.getByLabel(/idle conversations/i);
  await expect(superseded).toHaveValue("30");
  await expect(conversations).toHaveValue("90");

  await superseded.fill("14");
  await conversations.fill("60");
  await page.getByRole("button", { name: "Save retention windows" }).click();
  await expect(page.getByRole("status")).toContainText("Retention windows saved");
  await expectNoHorizontalOverflow(page);
});

test("a non-owner sees the denial instead of the form values", async ({ page }) => {
  await mockRetention(page, 403, { detail: "owner role required" });
  await page.goto("/settings/retention");

  await expect(page.getByRole("alert")).toContainText("owner role required");
  await expectNoHorizontalOverflow(page);
});

test("invalid windows disable saving without a request", async ({ page }) => {
  let putCount = 0;
  await page.route(retentionRoute, (route) => {
    const method = route.request().method();
    if (method === "PUT") putCount += 1;
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        method === "PUT" ? JSON.parse(route.request().postData() ?? "{}") : defaults,
      ),
    });
  });
  await page.goto("/settings/retention");

  await page.getByLabel(/superseded versions/i).fill("0");
  await expect(page.getByRole("button", { name: "Save retention windows" })).toBeDisabled();

  await page.getByLabel(/superseded versions/i).fill("7");
  await page.getByRole("button", { name: "Save retention windows" }).click();
  await expect(page.getByRole("status")).toContainText("Retention windows saved");
  expect(putCount).toBe(1);
});
