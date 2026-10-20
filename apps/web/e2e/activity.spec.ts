import { expect, test, type Page } from "@playwright/test";

type Job = {
  job_id: string;
  type: string;
  status: string;
  state: string;
  attempts: number;
  last_error: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
};

const sessionRoute = "**/api/session";
const jobsRoute = "**/api/operations/jobs";
const healthRoute = "**/api/operations/sources/health";
const auditRoute = "**/api/operations/audit";
const diagnosticsRoute = "**/api/operations/diagnostics";

let jobs: Job[];

function job(overrides: Partial<Job>): Job {
  return {
    job_id: "j1",
    type: "source_extract",
    status: "pending",
    state: "queued",
    attempts: 0,
    last_error: null,
    retryable: false,
    created_at: "2026-10-20T09:30:00+00:00",
    updated_at: "2026-10-20T09:30:00+00:00",
    ...overrides,
  };
}

function seed(initial: Job[]) {
  jobs = initial;
}

const emptyPage = { items: [], total: 0, limit: 50, offset: 0 };

const diagnostics = {
  host: "Darwin/arm64",
  ok: false,
  checks: [
    { name: "platform", status: "ok", detail: "macOS 15.6 arm64", remediation: "" },
    {
      name: "storage_paths",
      status: "warn",
      detail: "data volume at 91% capacity",
      remediation: "Free space under data/ or move the volume.",
    },
    {
      name: "ollama_reachable",
      status: "fail",
      detail: "Ollama not reachable at http://127.0.0.1:11434",
      remediation: "Start Ollama, then retry.",
    },
  ],
};

/** Mock the operations API for the given role; records retry POSTs. */
async function mockOperationsApi(page: Page, role: "admin" | "member") {
  let retryCount = 0;
  await page.route(sessionRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ role }),
    }),
  );
  await page.route(jobsRoute, (route) => {
    void route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: jobs, total: jobs.length, limit: 50, offset: 0 }),
    });
  });
  // Registered after the list route so it wins for the retry POST path.
  await page.route("**/api/operations/jobs/*/retry", (route) => {
    retryCount += 1;
    jobs = jobs.map((item) =>
      item.job_id === "jf" ? { ...item, state: "retrying", retryable: false } : item,
    );
    void route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ job_id: "jf", retry: "requested" }),
    });
  });
  await page.route(healthRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            source_id: "s1",
            name: "Employee Handbook",
            state: "active",
            latest_version_state: "failed",
            latest_version_at: "2026-10-20T09:00:00+00:00",
            created_at: "2026-10-19T09:00:00+00:00",
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    }),
  );
  await page.route(auditRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            event_id: "e1",
            type: "source.uploaded",
            actor: "owner@example.com",
            subject: "s1",
            metadata: {},
            created_at: "2026-10-20T08:00:00+00:00",
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    }),
  );
  await page.route(diagnosticsRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(diagnostics),
    }),
  );
  return () => retryCount;
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test("admin sees job states, retries only the failed job, and storage/provider warnings", async ({ page }) => {
  seed([
    job({ job_id: "jq", state: "queued" }),
    job({ job_id: "jr", state: "retrying", attempts: 2, last_error: "cutover_pending" }),
    job({ job_id: "ju", state: "running", attempts: 1 }),
    job({ job_id: "js", state: "stalled", attempts: 4, last_error: "lease lost" }),
    job({ job_id: "jf", state: "failed", attempts: 3, last_error: "parse_failed", retryable: true }),
    job({ job_id: "jc", state: "completed", attempts: 1 }),
  ]);
  const retryCount = await mockOperationsApi(page, "admin");
  await page.goto("/activity");

  for (const [jobId, state] of [
    ["jq", "queued"],
    ["jr", "retrying"],
    ["ju", "running"],
    ["js", "stalled"],
    ["jf", "failed"],
    ["jc", "completed"],
  ] as const) {
    await expect(page.getByTestId(`job-state-${jobId}`)).toHaveText(state);
  }
  await expect(page.getByText("parse_failed")).toBeVisible();

  // Exactly one Retry button: on the failed job only.
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(1);
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("status")).toContainText("Retry requested");
  expect(retryCount()).toBe(1);
  // The list refreshed: the failed job is now retrying.
  await expect(page.getByTestId("job-state-jf")).toHaveText("retrying");

  // Source health and audit render.
  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await expect(page.getByText("source.uploaded")).toBeVisible();

  // Diagnostics surface the storage warning and the provider outage.
  await expect(page.getByTestId("check-storage_paths")).toHaveText("warning");
  await expect(page.getByTestId("check-ollama_reachable")).toHaveText("fail");
  await expect(page.getByText("Free space under data/")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("member sees the restricted view", async ({ page }) => {
  seed([]);
  await mockOperationsApi(page, "member");
  await page.goto("/activity");

  await expect(
    page.getByText("Operator access is limited to administrators and owners."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("operator sees empty states with no data", async ({ page }) => {
  seed([]);
  const retryCount = await mockOperationsApi(page, "admin");
  await page.route(healthRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(emptyPage),
    }),
  );
  await page.route(auditRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(emptyPage),
    }),
  );
  await page.route(diagnosticsRoute, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ host: "Darwin/arm64", ok: true, checks: [] }),
    }),
  );
  await page.goto("/activity");

  await expect(page.getByText("No background jobs yet.")).toBeVisible();
  await expect(page.getByText("No sources yet.")).toBeVisible();
  await expect(page.getByText("No audit events yet.")).toBeVisible();
  await expect(page.getByText("No diagnostics available.")).toBeVisible();
  expect(retryCount()).toBe(0);
  await expectNoHorizontalOverflow(page);
});
