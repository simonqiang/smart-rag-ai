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

// --- Task 16: archive, version history, cutover, rollback ---

const versionsRoute = "**/api/sources/s1/versions";

async function mockOwnerSession(page: Page) {
  await page.route("**/api/session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u1",
        email: "owner@example.com",
        role: "owner",
      }),
    }),
  );
  // The owner page also mounts the upload form, which lists collections.
  await page.route(collectionsRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { collection_id: "c1", name: "Shared", created_at: "2026-10-01T00:00:00Z" },
      ]),
    }),
  );
}

test("an admin archives and unarchives a source in place", async ({ page }) => {
  await mockOwnerSession(page);
  let archived = false;
  await page.route(sourcesRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ ...shared[0], state: archived ? "archived" : "active" }]),
    }),
  );
  let posts = 0;
  await page.route("**/api/sources/s1/*", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts += 1;
    archived = posts % 2 === 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ source_id: "s1", state: archived ? "archived" : "active" }),
    });
  });
  await page.goto("/sources");

  const archive = page.getByRole("button", { name: "Archive" });
  await expect(archive).toBeVisible();
  await archive.click();
  await expect(page.getByRole("button", { name: "Unarchive" })).toBeVisible();
  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByRole("button", { name: "Archive" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("version history cuts over to a ready replacement", async ({ page }) => {
  await mockOwnerSession(page);
  await page.route(sourcesRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([shared[0]]),
    }),
  );
  let cutover = false;
  await page.route(versionsRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        cutover
          ? [
              { source_id: "s1", version_id: "v2", state: "active", filename: "travel-v2.txt", size_bytes: 20, created_at: "2026-10-03T00:00:00Z" },
              { source_id: "s1", version_id: "v1", state: "superseded", filename: "travel.txt", size_bytes: 18, created_at: "2026-10-01T00:00:00Z" },
            ]
          : [
              { source_id: "s1", version_id: "v1", state: "active", filename: "travel.txt", size_bytes: 18, created_at: "2026-10-01T00:00:00Z" },
              { source_id: "s1", version_id: "v2", state: "indexed", filename: "travel-v2.txt", size_bytes: 20, created_at: "2026-10-03T00:00:00Z" },
            ],
      ),
    }),
  );
  await page.route("**/api/sources/s1/versions/v2/activate", (route) => {
    cutover = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ source_id: "s1", active_version_id: "v2" }),
    });
  });
  await page.goto("/sources");

  await page.getByRole("button", { name: "Version history" }).click();
  await expect(page.getByText("travel.txt")).toBeVisible();
  await expect(page.getByText("travel-v2.txt")).toBeVisible();

  await page.getByRole("button", { name: "Make active" }).click();
  await expect(page.getByText("superseded").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Make active" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("owner deletion asks for confirmation and removes the source", async ({ page }) => {
  await mockOwnerSession(page);
  let deleted = false;
  await page.route(sourcesRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(deleted ? [] : [shared[0]]),
    }),
  );
  await page.route("**/api/sources/s1", (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deleted = true;
    return route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ source_id: "s1", state: "deleted", deletion: "scheduled" }),
    });
  });
  await page.goto("/sources");

  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await page.getByRole("button", { name: "Delete…" }).click();
  const confirm = page.getByRole("button", { name: "Delete forever" });
  await expect(confirm).toBeVisible();
  // The destructive step is explicit: "Keep" backs out without deleting.
  await page.getByRole("button", { name: "Keep" }).click();
  await expect(page.getByText("Employee Handbook")).toBeVisible();

  await page.getByRole("button", { name: "Delete…" }).click();
  await confirm.click();
  await expect(page.getByText("No sources yet")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
