import { expect, test } from "@playwright/test";

test("application shell renders", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Smart RAG AI" })).toBeVisible();
  await expect(page.getByText("Local-first knowledge assistant")).toBeVisible();
});
