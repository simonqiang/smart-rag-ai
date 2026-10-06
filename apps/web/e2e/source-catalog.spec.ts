import { expect, test, type Page } from "@playwright/test";

type Source = { source_id: string; collection_id: string; name: string; state: string; created_at: string };

const sourcesRoute = "**/api/sources";
const collectionsRoute = "**/api/collections";

const shared: Source[] = [
  {
    source_id: "s1",
    collection_id: "c1",
    name: "Employee Handbook",
    state: "active",
    created_at: "2026-10-01T00:00:00Z",
  },
  {
    source_id: "s2",
    collection_id: "c2",
    name: "Board Notes",
    state: "active",
    created_at: "2026-10-02T00:00:00Z",
  },
];

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

async function mockUserAccessApi(page: Page) {
  await page.route("**/api/users", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          user_id: "u1",
          email: "owner@example.com",
          role: "owner",
          status: "active",
          must_change_password: false,
          collections: [],
        },
      ]),
    }),
  );
  await page.route("**/api/users/invitations", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
}

test("empty catalog shows the empty state without errors", async ({ page }) => {
  await page.route(sourcesRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.goto("/sources");

  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  await expect(page.getByText("No sources yet")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("permitted sources are listed on every device class", async ({ page }) => {
  await page.route(sourcesRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([shared[0]]),
    }),
  );
  await page.goto("/sources");

  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await expect(page.getByText("Board Notes")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("a restricted source stays outside the member's list", async ({ page }) => {
  // Member is granted only the first source's collection.
  await page.route(sourcesRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([shared[0]]),
    }),
  );
  await page.route(collectionsRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ collection_id: "c1", name: "Shared", created_at: "2026-10-01T00:00:00Z" }]),
    }),
  );
  await page.goto("/sources");

  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await expect(page.getByText("Board Notes")).toHaveCount(0);
});

test("loading state is announced while the catalog loads", async ({ page }) => {
  await page.route(sourcesRoute, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });
  await page.goto("/sources");

  await expect(page.getByRole("status")).toHaveText("Loading sources…");
  await expect(page.getByText("No sources yet")).toBeVisible();
});

test("catalog failure shows an actionable error and recovers on retry", async ({
  page,
}) => {
  let failing = true;
  await page.route(sourcesRoute, (route) => {
    if (failing) {
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "catalog is unavailable; try again shortly" }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([shared[0]]),
    });
  });
  await page.goto("/sources");

  await expect(page.getByText("catalog is unavailable; try again shortly")).toBeVisible();
  const retry = page.getByRole("button", { name: "Retry" });
  await expect(retry).toBeVisible();
  await expect(retry).toBeEnabled();

  failing = false;
  await retry.click();
  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("shell navigation reaches user access without hover-only actions", async ({
  page,
}) => {
  await page.route(sourcesRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([shared[0]]),
    }),
  );
  await mockUserAccessApi(page);
  await page.goto("/sources");

  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await page.getByRole("link", { name: "User access" }).click();
  await expect(page).toHaveURL(/\/users$/);
  await expect(page.getByRole("heading", { name: "User access" })).toBeVisible();

  await page.getByRole("link", { name: "Sources" }).click();
  await expect(page).toHaveURL(/\/sources$/);
  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
