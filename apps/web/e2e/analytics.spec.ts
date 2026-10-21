import { expect, test, type Page } from "@playwright/test";

const sessionRoute = "**/api/session";
const summaryRoute = "**/api/analytics/summary";

const populated = {
  exchanges: 12,
  answered: 9,
  insufficient_evidence: 3,
  unanswered_conversations: 2,
  helpful: 7,
  unhelpful: 2,
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
    // The browser logs the HTTP status of deliberately mocked 4xx responses;
    // only app-level errors are problems.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      problems.push(message.text());
    }
  });
  return problems;
}

function mockSession(page: Page, role: "owner" | "admin" | "member") {
  return page.route(sessionRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ role }),
    }),
  );
}

function mockSummary(page: Page, status: number, body: object) {
  return page.route(summaryRoute, (route) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }),
  );
}

test("an owner sees the aggregate cards without any question text", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockSession(page, "owner");
  await mockSummary(page, 200, populated);
  await page.goto("/analytics");

  await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
  await expect(page.getByTestId("card-exchanges")).toContainText("12");
  await expect(page.getByTestId("card-helpful")).toContainText("7");
  await expect(page.getByTestId("card-unhelpful")).toContainText("2");
  await expect(page.getByTestId("card-insufficient")).toContainText("3");
  await expect(page.getByTestId("card-unanswered")).toContainText("2");
  // Coverage: (7 + 2) / 12 = 75%.
  await expect(page.getByTestId("card-coverage")).toContainText("75%");
  expect(await page.textContent("body")).not.toContain("refund");
  await expectNoHorizontalOverflow(page);
  expect(problems).toEqual([]);
});

test("an empty workspace shows the empty state", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockSession(page, "owner");
  await mockSummary(page, 200, {
    exchanges: 0,
    answered: 0,
    insufficient_evidence: 0,
    unanswered_conversations: 0,
    helpful: 0,
    unhelpful: 0,
  });
  await page.goto("/analytics");

  await expect(page.getByRole("status")).toContainText("No questions asked yet.");
  await expectNoHorizontalOverflow(page);
  expect(problems).toEqual([]);
});

test("a member sees the restricted view instead of the numbers", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockSession(page, "member");
  await mockSummary(page, 403, { detail: "admin role required" });
  await page.goto("/analytics");

  await expect(page.getByText("Analytics are limited to administrators and owners."))
    .toBeVisible();
  await expect(page.getByTestId("card-exchanges")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  expect(problems).toEqual([]);
});
