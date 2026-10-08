import { expect, test, type Page, type Route } from "@playwright/test";

type Frame = Record<string, unknown>;

function sse(frames: Frame[]): string {
  return frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("");
}

const HANDBOOK_QUOTE = "Public onboarding steps for every employee joining the support team.";

function citation(label: number, sourceId = "s1"): Frame {
  return {
    label,
    chunk_id: `chunk-${label}`,
    source_id: sourceId,
    source_name: "Employee Handbook",
    page: 2,
    block_start: 0,
    block_end: 1,
    quote: HANDBOOK_QUOTE,
  };
}

function groundedStream(overrides: Partial<{ language: string; text: string; marker: string }> = {}): Frame[] {
  const { language = "en", text = "Follow the onboarding steps ", marker = "[1]" } = overrides;
  return [
    { kind: "started", request_id: "req-1", seq: 1, language, conversation_id: "conv-1" },
    { kind: "delta", request_id: "req-1", seq: 2, text },
    { kind: "citation", request_id: "req-1", seq: 3, citation: citation(1) },
    {
      kind: "completed",
      request_id: "req-1",
      seq: 4,
      text: `${text}${marker}`,
      insufficient_evidence: false,
      model: "fake-chat",
      provider: { name: "ollama", local: true },
    },
  ];
}

const conversations = [
  { id: "conv-1", title: "Onboarding question", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-02T00:00:00Z" },
  { id: "conv-2", title: "Old thread", created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-02T00:00:00Z" },
];

async function mockBase(page: Page, ask: (route: Route) => Promise<unknown>) {
  await page.route("**/api/sources", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ source_id: "s1", name: "Employee Handbook", state: "active" }]),
    }),
  );
  await page.route("**/api/sources/s1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ source_id: "s1", name: "Employee Handbook", state: "active" }),
    }),
  );
  await page.route("**/api/sources/s-restricted", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"no such source"}' }),
  );
  await page.route("**/api/conversations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(conversations),
    }),
  );
  await page.route("**/api/conversations/conv-1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...conversations[0],
        messages: [
          { role: "user", content: "How do I onboard?", citations: [], language: "en", request_id: null, insufficient: false, created_at: "2026-10-01T00:00:00Z" },
          { role: "assistant", content: `Follow the onboarding steps [1]`, citations: [citation(1)], language: "en", request_id: "req-1", insufficient: false, created_at: "2026-10-01T00:01:00Z" },
        ],
      }),
    }),
  );
  await page.route("**/api/conversations/conv-2", (route) => route.fulfill({ status: 404, body: "{}", contentType: "application/json" }));
  await page.route("**/api/feedback", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/ask", ask);
}

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
    if (message.type() === "error") problems.push(message.text());
  });
  return problems;
}

test("member asks and streams a grounded answer with citations", async ({ page }) => {
  const problems = await watchConsole(page);
  await mockBase(page, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }),
  );
  await page.goto("/ask");

  await page.getByLabel("Your question").fill("How do I onboard?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  const answer = page.getByRole("region", { name: "Answer" });
  await expect(answer).toContainText("Follow the onboarding steps");
  await expect(answer).toHaveAttribute("lang", "en");
  await expect(page.getByText("Local model (ollama)")).toBeVisible();

  await page.getByRole("button", { name: "Open citation 1" }).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText(
    "Employee Handbook",
  );
  await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText(HANDBOOK_QUOTE);
  await page.getByRole("button", { name: "Close evidence" }).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toHaveCount(0);

  await page.getByRole("button", { name: "Helpful", exact: true }).click();
  await expect(page.getByText("Thanks for the feedback.")).toBeVisible();

  await expectNoHorizontalOverflow(page);
  expect(problems).toEqual([]);
});

test("the answer renders in the question language for Chinese and Malay", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }),
  );

  await page.route("**/api/ask", (route) => {
    const body = route.request().postDataJSON() as { question: string };
    const language = body.question.includes("津贴") ? "zh-Hans" : "ms";
    const text = language === "zh-Hans" ? "请按照入职步骤操作 " : "Ikut langkah orientasi ";
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse(groundedStream({ language, text })),
    });
  });
  await page.goto("/ask");

  await page.getByLabel("Your question").fill("津贴是多少？");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("region", { name: "Answer" })).toHaveAttribute("lang", "zh-Hans");

  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByLabel("Your question").fill("Bagaimana orientasi?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("region", { name: "Answer" })).toHaveAttribute("lang", "ms");
});

test("insufficient evidence shows the notice without citations", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse([
        { kind: "started", request_id: "req-2", seq: 1, language: "en", conversation_id: "conv-1" },
        { kind: "delta", request_id: "req-2", seq: 2, text: "not enough indexed evidence" },
        { kind: "completed", request_id: "req-2", seq: 3, text: "not enough indexed evidence", insufficient_evidence: true },
      ]),
    }),
  );
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("zzqq unrelated xyzz");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(
    page.getByText("Not enough indexed evidence. Try a different question or add sources."),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "Answer" })).toContainText("not enough indexed evidence");
  await expect(page.getByRole("button", { name: /Open citation/ })).toHaveCount(0);
});

test("a provider timeout arrives as a safe error frame with retry", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse([
        { kind: "started", request_id: "req-3", seq: 1, language: "en", conversation_id: "conv-1" },
        { kind: "error", request_id: "req-3", seq: 2, reason: "timeout", message: "the model host timed out; try again" },
      ]),
    }),
  );
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("How do I onboard?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(page.getByRole("alert")).toContainText("the model host timed out; try again");
  await expect(page.getByRole("button", { name: "Retry" })).toBeEnabled();
});

test("a dropped stream reconnects through resume and completes", async ({ page }) => {
  // POST answers partially, then ends without completed: the hook resumes.
  await mockBase(page, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse([
        { kind: "started", request_id: "req-drop", seq: 1, language: "en", conversation_id: "conv-1" },
        { kind: "delta", request_id: "req-drop", seq: 2, text: "Follow the " },
      ]),
    }),
  );
  await page.route("**/api/ask/req-drop/events", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }),
  );
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("How do I onboard?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(page.getByRole("region", { name: "Answer" })).toContainText(
    "Follow the onboarding steps",
  );
  await expect(page.getByText("Local model (ollama)")).toBeVisible();
});

test("the member can cancel a running answer", async ({ page }) => {
  await mockBase(page, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 5000)); // generation keeps going
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) });
  });
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("How do I onboard?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  const stop = page.getByRole("button", { name: "Stop" });
  await expect(stop).toBeEnabled();
  await stop.click();
  await expect(page.getByText("Answer cancelled.")).toBeVisible();
});

test("the source filter narrows the question to one source", async ({ page }) => {
  const captured: { payload?: { source_id: string | null } } = {};
  await mockBase(page, (route) => {
    captured.payload = route.request().postDataJSON() as { source_id: string | null };
    return route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) });
  });
  await page.goto("/ask");
  await page.getByLabel("Filter sources").selectOption({ label: "Employee Handbook" });
  await page.getByLabel("Your question").fill("How do I onboard?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(page.getByRole("region", { name: "Answer" })).toBeVisible();
  expect(captured.payload?.source_id).toBe("s1");
});

test("citations from history re-authorize before opening", async ({ page }) => {
  await mockBase(page, (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }));
  await page.goto("/ask");

  await page.getByRole("button", { name: "Onboarding question" }).click();
  await expect(page.getByRole("region", { name: "Conversation history" })).toBeVisible();
  await page.getByRole("button", { name: "Open citation 1" }).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText(HANDBOOK_QUOTE);
});

test("a citation whose source vanished says so instead of leaking", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse(groundedStream()),
    }),
  );
  // Restricted evidence: the citation re-authorization refuses.
  await page.route("**/api/ask", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse([
        { kind: "started", request_id: "req-r", seq: 1, language: "en", conversation_id: "conv-1" },
        { kind: "delta", request_id: "req-r", seq: 2, text: "Hidden answer " },
        { kind: "citation", request_id: "req-r", seq: 3, citation: citation(1, "s-restricted") },
        { kind: "completed", request_id: "req-r", seq: 4, text: "Hidden answer [1]", insufficient_evidence: false },
      ]),
    }),
  );
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("What are the bonus targets?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await page.getByRole("button", { name: "Open citation 1" }).click();

  await expect(page.getByText("This source is no longer available.")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Evidence" })).not.toContainText("bonus");
});

test("conversations list, rename, and delete work without hover", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }),
  );
  const captured: { renamed?: { title: string }; deleted?: boolean } = {};
  await page.route("**/api/conversations/conv-2", async (route) => {
    if (route.request().method() === "PUT") {
      captured.renamed = route.request().postDataJSON() as { title: string };
      return route.fulfill({ status: 204, body: "" });
    }
    if (route.request().method() === "DELETE") {
      captured.deleted = true;
      return route.fulfill({ status: 204, body: "" });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
  await page.goto("/ask");

  await expect(page.getByRole("button", { name: "Onboarding question" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Old thread" })).toBeVisible();

  // Rename conv-2 inline.
  await page.getByRole("button", { name: "Rename" }).nth(1).click();
  await page.getByLabel("Conversation title").fill("Renamed thread");
  await page.getByRole("button", { name: "Save title" }).click();
  expect(captured.renamed?.title).toBe("Renamed thread");

  // Delete conv-2 through the two-step confirmation.
  await page.getByRole("button", { name: "Delete" }).nth(1).click();
  await page.getByRole("button", { name: "Yes, delete" }).click();
  expect(captured.deleted).toBe(true);

  await expectNoHorizontalOverflow(page);
});

test("the empty workspace shows guidance instead of an empty page", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }),
  );
  await page.route("**/api/conversations", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.goto("/ask");

  await expect(page.getByText("Ask a question and a cited answer will appear here.")).toBeVisible();
  await expect(page.getByText("No conversations yet")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("submitting with the keyboard works on every device class", async ({ page }) => {
  await mockBase(page, (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(groundedStream()) }),
  );
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("How do I onboard?");
  await page.getByLabel("Your question").press("Enter");

  await expect(page.getByRole("region", { name: "Answer" })).toContainText("Follow the onboarding steps");
  await expectNoHorizontalOverflow(page);
});
