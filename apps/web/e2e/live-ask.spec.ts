import { expect, test, type Page } from "@playwright/test";

/** Live checkpoint: real stack at SMART_RAG_LIVE_URL, no mocks. Skipped in CI. */
const LIVE = process.env.SMART_RAG_LIVE_URL;
const CREDS = {
  email: process.env.SMART_RAG_LIVE_USER ?? "owner@example.com",
  password: process.env.SMART_RAG_LIVE_PASSWORD ?? "",
};

test.skip(!LIVE || !CREDS.password, "set SMART_RAG_LIVE_URL/USER/PASSWORD to run");

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test("sign-in, ask, and open a live citation on this device", async ({ page }) => {
  test.setTimeout(300_000); // qwen3 on CPU answers in tens of seconds
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(String(error)));

  await page.goto(`${LIVE}/signin`);
  await page.getByLabel("Email").fill(CREDS.email);
  await page.getByLabel("Password (at least 10 characters)").fill(CREDS.password);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/ask$/);
  await expect(page.getByRole("heading", { name: "Ask" })).toBeVisible();

  await page.getByLabel("Your question").fill("What is the home-office allowance?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  const answer = page.getByRole("region", { name: "Answer" });
  await expect(answer).toContainText("500", { timeout: 240_000 });
  await expect(page.getByText("Local model (ollama)")).toBeVisible({ timeout: 240_000 });

  await page.getByRole("button", { name: "Open citation 1" }).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText("allowance");

  await expectNoHorizontalOverflow(page);
  expect(problems).toEqual([]);
});
