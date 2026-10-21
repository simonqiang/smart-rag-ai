import { expect, test, type Page } from "@playwright/test";

const backupRoute = "**/api/settings/backup";

const empty = { backups: [] };
const populated = {
  backups: [
    {
      id: "b1",
      filename: "f47ac10b-58cc-4372-a567-0e02b2c3d479.srb",
      size_bytes: 5 * 1024 * 1024,
      created_at: "2026-10-22T09:07:41+00:00",
      source_count: 3,
    },
  ],
};

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

function mockBackup(page: Page, getStatus = 200, getBody: object = empty) {
  return page.route(backupRoute, (route) => {
    if (route.request().method() === "POST") {
      void route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: "new",
          filename: "new-backup.srb",
          sha256: "abc",
          size_bytes: 1024,
        }),
      });
      return;
    }
    void route.fulfill({
      status: getStatus,
      contentType: "application/json",
      body: JSON.stringify(getBody),
    });
  });
}

test("owner sees the list and creates a backup", async ({ page }) => {
  let created = false;
  await page.route(backupRoute, (route) => {
    if (route.request().method() === "POST") {
      created = true;
      void route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: "new", filename: "new-backup.srb", sha256: "abc", size_bytes: 1024,
        }),
      });
      return;
    }
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(created ? populated : empty),
    });
  });
  await page.goto("/settings/backup");

  await expect(page.getByRole("heading", { name: "Backup and restore" })).toBeVisible();
  await expect(page.getByText("No backups yet.")).toBeVisible();

  await page.getByRole("button", { name: "Create encrypted backup" }).click();
  await expect(page.getByRole("status")).toContainText("Backup created: new-backup.srb");
  await expect(page.getByText(/new-backup\.srb/).first()).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("an existing backup shows its facts without the key", async ({ page }) => {
  await mockBackup(page, 200, populated);
  await page.goto("/settings/backup");

  const row = page.getByRole("listitem");
  await expect(row).toContainText("5.0 MiB");
  await expect(row).toContainText("2026-10-22 09:07");
  await expect(row).toContainText("3 sources");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("a non-owner sees the denial instead of the list", async ({ page }) => {
  await mockBackup(page, 403, { detail: "owner role required" });
  await page.goto("/settings/backup");

  await expect(page.getByRole("alert")).toContainText("owner role required");
  await expectNoHorizontalOverflow(page);
});

test("a missing recovery key surfaces an actionable message", async ({ page }) => {
  let posts = 0;
  await page.route(backupRoute, (route) => {
    if (route.request().method() === "POST") {
      posts += 1;
      void route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "set the SMART_RAG_BACKUP_KEY environment variable to the recovery key",
        }),
      });
      return;
    }
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(empty),
    });
  });
  await page.goto("/settings/backup");

  await page.getByRole("button", { name: "Create encrypted backup" }).click();
  await expect(page.getByRole("alert")).toContainText("SMART_RAG_BACKUP_KEY");
  expect(posts).toBe(1);
  await expect(page.getByText("No backups yet.")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
