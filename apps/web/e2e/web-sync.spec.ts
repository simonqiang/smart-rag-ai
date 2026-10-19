import { expect, test, type Page } from "@playwright/test";

type Schedule = {
  schedule_id: string;
  collection_id: string;
  source_id: string | null;
  name: string;
  start_url: string;
  sitemap_url: string | null;
  allowed_prefixes: string[];
  interval_seconds: number;
  enabled: boolean;
  next_sync_at: string | null;
  last_synced_at: string | null;
  last_state: string | null;
  last_detail: string | null;
};

const syncRoute = "**/api/web-sync";
const sourcesRoute = "**/api/sources";
const sessionRoute = "**/api/session";
const collectionsRoute = "**/api/collections";

let schedules: Schedule[];

function seed(initial: Schedule[]) {
  schedules = initial;
}

function schedule(overrides: Partial<Schedule>): Schedule {
  return {
    schedule_id: "ws1",
    collection_id: "c1",
    source_id: "s1",
    name: "Product Docs",
    start_url: "https://docs.example.com/",
    sitemap_url: null,
    allowed_prefixes: ["/guide/"],
    interval_seconds: 86_400,
    enabled: true,
    next_sync_at: "2026-10-21T09:30:00+00:00",
    last_synced_at: "2026-10-20T09:30:00+00:00",
    last_state: "synced",
    last_detail: "snapshot live",
    ...overrides,
  };
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** Mock the sources page as an admin (or member) with the given schedules. */
async function mockSyncApi(page: Page, role: "admin" | "member") {
  await page.route(sessionRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ role }),
    }),
  );
  await page.route(sourcesRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route(collectionsRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ collection_id: "c1", name: "Handbook" }]),
    }),
  );
  await page.route(syncRoute, (route) => {
    const method = route.request().method();
    if (method === "GET") {
      void route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(schedules),
      });
      return;
    }
    if (method === "POST" && new URL(route.request().url()).pathname === "/api/web-sync") {
      const body = JSON.parse(route.request().postData() ?? "{}") as Partial<Schedule>;
      const created = schedule({
        schedule_id: "ws2",
        name: body.name ?? "Unnamed",
        start_url: body.start_url ?? "",
        source_id: null,
        next_sync_at: "2026-10-20T10:00:00+00:00",
        last_synced_at: null,
        last_state: null,
        last_detail: null,
      });
      schedules = [...schedules, created];
      void route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(created) });
      return;
    }
    void route.fulfill({ status: 405, contentType: "application/json", body: "{}" });
  });
}

test("admin sees freshness chips and adds a website", async ({ page }) => {
  seed([schedule({})]);
  await mockSyncApi(page, "admin");
  await page.goto("/sources");

  const row = page.locator("li", { hasText: "Product Docs" });
  await expect(row).toBeVisible();
  await expect(row.getByText("synced", { exact: true })).toBeVisible();
  await expect(row.getByText("2026-10-20 09:30")).toBeVisible();
  await expect(row.getByText("2026-10-21 09:30")).toBeVisible();

  const form = page.getByRole("form", { name: "Add website" });
  await form.getByLabel("Name").fill("Docs Portal");
  await form.getByLabel("Start URL").fill("https://portal.example.com/");
  await form.getByRole("button", { name: "Add website" }).click();
  await expect(page.getByRole("status")).toContainText("Website added");
  const newRow = page.locator("li", { hasText: "Docs Portal" });
  await expect(newRow).toBeVisible();
  await expect(newRow.getByText("never synced", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("sync now and disable update the list", async ({ page }) => {
  seed([schedule({})]);
  await mockSyncApi(page, "admin");
  let runCount = 0;
  await page.route("**/api/web-sync/ws1/run", (route) => {
    runCount += 1;
    void route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ schedule_id: "ws1", sync: "requested" }),
    });
  });
  await page.route("**/api/web-sync/ws1/enabled", (route) => {
    const enabled = (JSON.parse(route.request().postData() ?? "{}") as { enabled: boolean }).enabled;
    schedules = schedules.map((item) =>
      item.schedule_id === "ws1" ? { ...item, enabled } : item,
    );
    void route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  await page.goto("/sources");

  await page.getByRole("button", { name: "Sync now" }).click();
  await expect(page.getByRole("status")).toContainText("Sync requested");
  expect(runCount).toBe(1);

  await page.getByRole("button", { name: "Disable" }).click();
  const row = page.locator("li", { hasText: "Product Docs" });
  await expect(row.getByText("paused", { exact: true })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("a member sees the freshness list but no admin controls", async ({ page }) => {
  seed([schedule({ last_state: "failed", last_detail: "crawl timeout" })]);
  await mockSyncApi(page, "member");
  await page.goto("/sources");

  const row = page.locator("li", { hasText: "Product Docs" });
  await expect(row.getByText("failed", { exact: true })).toBeVisible();
  await expect(row.getByText("crawl timeout")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sync now" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add website" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});
