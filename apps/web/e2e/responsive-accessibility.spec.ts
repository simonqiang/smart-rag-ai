import { expect, test, type Page } from "@playwright/test";

type Role = "owner" | "admin" | "member";

type AuditReport = {
  overflowPx: number;
  clippedControls: string[];
  undersizedTargets: string[];
  unnamedControls: string[];
  landmarkViolations: string[];
  headingViolations: string[];
  duplicateIds: string[];
  contrastViolations: string[];
  hoverOnlyRules: string[];
};

function sse(frames: Record<string, unknown>[]): string {
  return frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("");
}

async function watchConsole(page: Page): Promise<string[]> {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      problems.push(message.text());
    }
  });
  return problems;
}

async function mockAllApis(page: Page, role: Role = "owner") {
  await page.route("**/api/setup/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        initialized: false,
        dependencies: [
          { name: "database", ok: true, detail: "reachable" },
          { name: "redis", ok: true, detail: "reachable" },
          { name: "storage", ok: true, detail: "writable" },
          { name: "ollama", ok: true, detail: "qwen3:8b + bge-m3 available" },
        ],
      }),
    }),
  );
  await page.route("**/api/session", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user_id: "u1",
        workspace_id: "w1",
        email: `${role}@example.com`,
        role,
        must_change_password: false,
      }),
    }),
  );
  await page.route("**/api/collections", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { collection_id: "c1", name: "Operations Handbook", created_at: "2026-10-01T00:00:00Z" },
      ]),
    }),
  );
  await page.route("**/api/sources", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          source_id: "s1",
          collection_id: "c1",
          name: "Employee Handbook",
          state: "active",
          created_at: "2026-10-01T00:00:00Z",
        },
      ]),
    }),
  );
  await page.route("**/api/sources/s1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        source_id: "s1",
        collection_id: "c1",
        name: "Employee Handbook",
        state: "active",
      }),
    }),
  );
  await page.route("**/api/sources/s1/versions", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          source_id: "s1",
          version_id: "v1",
          state: "active",
          filename: "handbook-v1.pdf",
          size_bytes: 2048,
          created_at: "2026-10-01T00:00:00Z",
        },
        {
          source_id: "s1",
          version_id: "v2",
          state: "indexed",
          filename: "handbook-v2-replacement-with-long-name.pdf",
          size_bytes: 4096,
          created_at: "2026-10-02T00:00:00Z",
        },
      ]),
    }),
  );
  await page.route("**/api/web-sync", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          schedule_id: "ws1",
          collection_id: "c1",
          source_id: "s1",
          name: "Help Center",
          start_url: "https://support.example.com/documentation/getting-started",
          sitemap_url: null,
          allowed_prefixes: ["/documentation/"],
          interval_seconds: 86400,
          enabled: true,
          next_sync_at: "2026-10-23T09:30:00+00:00",
          last_synced_at: "2026-10-22T09:30:00+00:00",
          last_state: "synced",
          last_detail: "snapshot live",
        },
      ]),
    }),
  );
  await page.route("**/api/conversations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "conv-1",
          title: "Leave policy question",
          created_at: "2026-10-20T00:00:00Z",
          updated_at: "2026-10-21T00:00:00Z",
        },
      ]),
    }),
  );
  await page.route("**/api/conversations/conv-1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "conv-1",
        title: "Leave policy question",
        created_at: "2026-10-20T00:00:00Z",
        updated_at: "2026-10-21T00:00:00Z",
        messages: [
          {
            role: "user",
            content: "What is the annual leave entitlement?",
            citations: [],
            language: "en",
            request_id: null,
            insufficient: false,
            created_at: "2026-10-20T00:00:00Z",
          },
          {
            role: "assistant",
            content: "Full-time staff receive 18 days of paid annual leave [1].",
            citations: [
              {
                label: 1,
                chunk_id: "chunk-1",
                source_id: "s1",
                source_name: "Employee Handbook",
                page: 4,
                block_start: 1,
                block_end: 2,
                quote: "Full-time staff receive 18 days of paid annual leave per calendar year.",
              },
            ],
            language: "en",
            request_id: "req-1",
            insufficient: false,
            created_at: "2026-10-20T00:00:05Z",
          },
        ],
      }),
    }),
  );
  await page.route("**/api/ask", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sse([
        { kind: "started", request_id: "req-1", seq: 1, language: "en", conversation_id: "conv-1" },
        {
          kind: "delta",
          request_id: "req-1",
          seq: 2,
          text: "Full-time staff receive 18 days of paid annual leave ",
        },
        {
          kind: "citation",
          request_id: "req-1",
          seq: 3,
          citation: {
            label: 1,
            chunk_id: "chunk-1",
            source_id: "s1",
            source_name: "Employee Handbook",
            page: 4,
            block_start: 1,
            block_end: 2,
            quote: "Full-time staff receive 18 days of paid annual leave per calendar year.",
          },
        },
        {
          kind: "completed",
          request_id: "req-1",
          seq: 4,
          text: "Full-time staff receive 18 days of paid annual leave [1]",
          insufficient_evidence: false,
          model: "qwen3:8b",
          provider: { name: "ollama", local: true },
        },
      ]),
    }),
  );
  await page.route("**/api/operations/jobs", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : {
              items: [
                {
                  job_id: "jf",
                  type: "source_extract",
                  status: "failed",
                  state: "failed",
                  attempts: 3,
                  last_error: "parse_failed",
                  retryable: true,
                  created_at: "2026-10-20T09:30:00+00:00",
                  updated_at: "2026-10-20T09:35:00+00:00",
                },
              ],
              total: 1,
              limit: 50,
              offset: 0,
            },
      ),
    }),
  );
  await page.route("**/api/operations/sources/health", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : {
              items: [
                {
                  source_id: "s1",
                  name: "Employee Handbook",
                  state: "active",
                  latest_version_state: "active",
                  created_at: "2026-10-19T09:00:00+00:00",
                },
              ],
              total: 1,
              limit: 50,
              offset: 0,
            },
      ),
    }),
  );
  await page.route("**/api/operations/audit", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : {
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
            },
      ),
    }),
  );
  await page.route("**/api/operations/diagnostics", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : {
              host: "Darwin/arm64",
              ok: true,
              checks: [
                { name: "platform", status: "ok", detail: "macOS 15.6 arm64", remediation: "" },
                {
                  name: "storage_paths",
                  status: "warn",
                  detail: "data volume at 88% capacity",
                  remediation: "Free space under data/ or move the volume.",
                },
              ],
            },
      ),
    }),
  );
  await page.route("**/api/analytics/summary", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : {
              exchanges: 8,
              answered: 6,
              insufficient_evidence: 2,
              unanswered_conversations: 1,
              helpful: 5,
              unhelpful: 1,
            },
      ),
    }),
  );
  await page.route("**/api/users", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : [
              {
                user_id: "u1",
                email: "owner@example.com",
                role: "owner",
                status: "active",
                must_change_password: false,
                collections: ["c1"],
              },
            ],
      ),
    }),
  );
  await page.route("**/api/users/invitations", (route) =>
    route.fulfill({
      status: role === "member" ? 403 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        role === "member"
          ? { detail: "admin role required" }
          : [
              {
                invitation_id: "inv1",
                email: "new.teammate@example.com",
                role: "member",
                collections: ["c1"],
                expires_at: "2026-10-30T00:00:00Z",
              },
            ],
      ),
    }),
  );
  await page.route("**/api/settings/retention", (route) =>
    route.fulfill({
      status: role === "owner" ? 200 : 403,
      contentType: "application/json",
      body: JSON.stringify(
        role === "owner"
          ? { superseded_days: 30, conversation_days: 90, backup_days: 30 }
          : { detail: "owner role required" },
      ),
    }),
  );
  await page.route("**/api/settings/ai-provider", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        provider: "glm",
        endpoint: "https://glm.example/v1/chat/completions",
        model: "glm-4",
        rewrite_enabled: true,
        consent_version: "2026-10-20",
        consented_at: "2026-10-21T08:05:00+00:00",
        consent_version_current: "2026-10-20",
      }),
    }),
  );
  await page.route("**/api/settings/backup", (route) =>
    route.fulfill({
      status: role === "owner" ? 200 : 403,
      contentType: "application/json",
      body: JSON.stringify(
        role === "owner"
          ? {
              backups: [
                {
                  id: "b1",
                  filename: "f47ac10b-58cc-4372-a567-0e02b2c3d479.srb",
                  size_bytes: 5 * 1024 * 1024,
                  created_at: "2026-10-22T09:07:41+00:00",
                  source_count: 3,
                },
              ],
            }
          : { detail: "owner role required" },
      ),
    }),
  );
}

/**
 * Runs a comprehensive WCAG 2.1 AA + responsive-device audit on the current page:
 * - Zero page-level horizontal overflow
 * - Zero horizontally clipped controls
 * - Touch targets >= 44x44 CSS pixels for all interactive controls
 * - Accessible names on all interactive controls
 * - Landmarks (document lang, title, single main landmark, labeled nav/sections)
 * - Heading hierarchy (single h1, no skipped levels)
 * - No duplicate DOM IDs
 * - WCAG 2.1 AA text contrast (>= 4.5:1 normal text, >= 3.0:1 large text)
 * - No hover-only CSS visibility rules
 */
async function expectAccessibleAndResponsive(page: Page, contextLabel: string) {
  const report: AuditReport = await page.evaluate(() => {
    const isVisible = (el: Element): boolean => {
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
        return false;
      }
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };

    const describeEl = (el: Element): string => {
      const tag = el.tagName.toLowerCase();
      const text = (el.textContent ?? "").trim().slice(0, 30);
      const aria = el.getAttribute("aria-label") ?? "";
      return `<${tag}${aria ? ` aria-label="${aria}"` : ""}> "${text}"`;
    };

    const accessibleName = (el: Element): string => {
      const ariaLabel = el.getAttribute("aria-label")?.trim();
      if (ariaLabel) return ariaLabel;
      const labelledBy = el.getAttribute("aria-labelledby")?.trim();
      if (labelledBy) {
        const parts = labelledBy
          .split(/\s+/)
          .map((id) => document.getElementById(id)?.textContent?.trim() ?? "")
          .filter(Boolean);
        if (parts.length > 0) return parts.join(" ");
      }
      if (
        el instanceof HTMLInputElement ||
        el instanceof HTMLSelectElement ||
        el instanceof HTMLTextAreaElement
      ) {
        if (el.labels && el.labels.length > 0) {
          const text = Array.from(el.labels)
            .map((label) => label.textContent?.trim() ?? "")
            .filter(Boolean)
            .join(" ");
          if (text) return text;
        }
        const wrappingLabel = el.closest("label");
        if (wrappingLabel?.textContent?.trim()) return wrappingLabel.textContent.trim();
      }
      return (el.textContent ?? "").trim();
    };

    const overflowPx = Math.max(
      0,
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );

    const clippedControls: string[] = [];
    const undersizedTargets: string[] = [];
    const unnamedControls: string[] = [];

    const interactive = Array.from(
      document.querySelectorAll("button, a[href], input, select, textarea"),
    ).filter(isVisible);

    for (const el of interactive) {
      const rect = el.getBoundingClientRect();
      if (rect.left < -1 || rect.right > window.innerWidth + 1) {
        clippedControls.push(
          `${describeEl(el)} [${Math.round(rect.left)}..${Math.round(rect.right)} outside 0..${window.innerWidth}]`,
        );
      }
      if (rect.width < 43.5 || rect.height < 43.5) {
        undersizedTargets.push(
          `${describeEl(el)} (${Math.round(rect.width)}x${Math.round(rect.height)}px < 44x44px)`,
        );
      }
      if (!accessibleName(el)) {
        unnamedControls.push(describeEl(el));
      }
    }

    const landmarkViolations: string[] = [];
    const htmlLang = document.documentElement.getAttribute("lang")?.trim() ?? "";
    if (!htmlLang) landmarkViolations.push("missing <html lang>");
    if (!document.title.trim()) landmarkViolations.push("missing <title>");

    const mains = Array.from(document.querySelectorAll('main, [role="main"]')).filter(isVisible);
    if (mains.length !== 1) {
      landmarkViolations.push(`expected 1 visible main landmark, found ${mains.length}`);
    }

    for (const nav of Array.from(document.querySelectorAll("nav")).filter(isVisible)) {
      if (!accessibleName(nav)) {
        landmarkViolations.push(`unlabeled <nav>: ${describeEl(nav)}`);
      }
    }

    const headingViolations: string[] = [];
    const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6")).filter(
      isVisible,
    );
    const h1Count = headings.filter((h) => h.tagName === "H1").length;
    if (h1Count !== 1) {
      headingViolations.push(`expected 1 visible <h1>, found ${h1Count}`);
    }
    let prevLevel = 0;
    for (const h of headings) {
      const level = Number(h.tagName.slice(1));
      if (!(h.textContent ?? "").trim()) {
        headingViolations.push(`empty heading <${h.tagName.toLowerCase()}>`);
      }
      if (prevLevel > 0 && level > prevLevel + 1) {
        headingViolations.push(`heading level skipped from h${prevLevel} to h${level}`);
      }
      prevLevel = level;
    }

    const duplicateIds: string[] = [];
    const seenIds = new Set<string>();
    for (const el of Array.from(document.querySelectorAll("[id]"))) {
      const id = el.getAttribute("id") ?? "";
      if (id) {
        if (seenIds.has(id)) duplicateIds.push(id);
        seenIds.add(id);
      }
    }

    const parseRgba = (raw: string): [number, number, number, number] => {
      const m = raw.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
      if (!m) return [255, 255, 255, 1];
      return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])];
    };

    const effectiveBg = (el: Element): [number, number, number] => {
      let cur: Element | null = el;
      while (cur) {
        const [r, g, b, a] = parseRgba(window.getComputedStyle(cur).backgroundColor);
        if (a >= 0.95) return [r, g, b];
        cur = cur.parentElement;
      }
      return [255, 255, 255];
    };

    const luminance = ([r, g, b]: [number, number, number]): number => {
      const chan = (c: number) => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b);
    };

    const contrastViolations: string[] = [];
    const allElements = Array.from(document.body.querySelectorAll("*")).filter(isVisible);
    for (const el of allElements) {
      if ((el as HTMLButtonElement | HTMLInputElement).disabled) continue;
      const directText = Array.from(el.childNodes)
        .filter((node) => node.nodeType === Node.TEXT_NODE)
        .map((node) => node.textContent?.trim() ?? "")
        .join("");
      if (!directText) continue;
      const style = window.getComputedStyle(el);
      const [fr, fg, fb] = parseRgba(style.color);
      const bg = effectiveBg(el);
      const l1 = luminance([fr, fg, fb]);
      const l2 = luminance(bg);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      const fontSize = parseFloat(style.fontSize);
      const weight = Number(style.fontWeight) || (style.fontWeight === "bold" ? 700 : 400);
      const isLarge = fontSize >= 24 || (fontSize >= 18.66 && weight >= 700);
      const minRatio = isLarge ? 3.0 : 4.5;
      if (ratio < minRatio) {
        contrastViolations.push(
          `${describeEl(el)} contrast ${ratio.toFixed(2)}:1 < ${minRatio}:1`,
        );
      }
    }

    const hoverOnlyRules: string[] = [];
    for (const sheet of Array.from(document.styleSheets)) {
      let rules: CSSRuleList;
      try {
        rules = sheet.cssRules;
      } catch {
        continue;
      }
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSStyleRule && rule.selectorText.includes(":hover")) {
          const css = rule.style.cssText.toLowerCase();
          if (css.includes("display") || css.includes("visibility") || css.includes("opacity")) {
            hoverOnlyRules.push(rule.cssText);
          }
        }
      }
    }

    return {
      overflowPx,
      clippedControls,
      undersizedTargets,
      unnamedControls,
      landmarkViolations,
      headingViolations,
      duplicateIds,
      contrastViolations,
      hoverOnlyRules,
    };
  });

  expect(report, `accessibility/responsive audit failed on ${contextLabel}`).toEqual({
    overflowPx: 0,
    clippedControls: [],
    undersizedTargets: [],
    unnamedControls: [],
    landmarkViolations: [],
    headingViolations: [],
    duplicateIds: [],
    contrastViolations: [],
    hoverOnlyRules: [],
  });
}

test("onboarding and sign-in routes pass WCAG AA, touch target, and overflow checks", async ({
  page,
}) => {
  const problems = await watchConsole(page);
  await mockAllApis(page, "owner");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Smart RAG AI" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "landing (/)");

  await page.goto("/setup");
  await expect(page.getByLabel("Owner email")).toBeVisible();
  await expectAccessibleAndResponsive(page, "setup form (/setup)");

  await page.goto("/signin");
  await expect(page.getByLabel("Email")).toBeVisible();
  await expectAccessibleAndResponsive(page, "sign-in form (/signin)");

  await page.getByRole("link", { name: "I have an invitation token" }).click();
  await expect(page.getByLabel("Invitation token")).toBeVisible();
  await expectAccessibleAndResponsive(page, "invitation form (/signin)");

  expect(problems).toEqual([]);
});

test("owner workflows across every route pass WCAG AA, touch target, and overflow checks", async ({
  page,
}) => {
  const problems = await watchConsole(page);
  await mockAllApis(page, "owner");

  // 1. Ask page with streamed answer, citation marker, open evidence, and conversation actions
  await page.goto("/ask");
  await page.getByLabel("Your question").fill("What is the annual leave entitlement?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByRole("region", { name: "Answer" })).toContainText("18 days");
  await page.getByRole("button", { name: "Open citation 1" }).click();
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /ask with evidence open");

  // 2. Sources page with upload, expanded version history, two-step delete dialog, and web-sync
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Sources" }).click();
  await expect(page).toHaveURL(/\/sources$/);
  await page.getByRole("button", { name: "Version history" }).click();
  await expect(page.getByText("handbook-v2-replacement-with-long-name.pdf")).toBeVisible();
  await page.getByRole("button", { name: "Delete…" }).click();
  await expect(page.getByRole("button", { name: "Delete forever" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /sources with versions and delete confirm");
  await page.getByRole("button", { name: "Keep" }).click();

  // 3. Activity page (jobs, retry button, source health, audit, diagnostics)
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Activity" }).click();
  await expect(page).toHaveURL(/\/activity$/);
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /activity");

  // 4. Analytics page
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Analytics" }).click();
  await expect(page).toHaveURL(/\/analytics$/);
  await expect(page.getByTestId("card-exchanges")).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /analytics");

  // 5. User access page
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "User access" }).click();
  await expect(page).toHaveURL(/\/users$/);
  await expect(page.getByRole("button", { name: "Create invitation" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /users");

  // 6. Retention settings
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Retention" }).click();
  await expect(page).toHaveURL(/\/settings\/retention$/);
  await expect(page.getByRole("button", { name: "Save retention windows" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /settings/retention");

  // 7. AI provider settings
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "AI provider" }).click();
  await expect(page).toHaveURL(/\/settings\/ai-provider$/);
  await expect(page.getByRole("button", { name: "Revert to local Ollama" })).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /settings/ai-provider");

  // 8. Backup and restore settings
  await page.getByRole("navigation", { name: "primary" }).getByRole("link", { name: "Backup" }).click();
  await expect(page).toHaveURL(/\/settings\/backup$/);
  await expect(page.getByText(/f47ac10b-58cc-4372-a567-0e02b2c3d479\.srb/)).toBeVisible();
  await expectAccessibleAndResponsive(page, "owner /settings/backup");

  expect(problems).toEqual([]);
});

test("member restricted and read-only views pass WCAG AA, touch target, and overflow checks", async ({
  page,
}) => {
  const problems = await watchConsole(page);
  await mockAllApis(page, "member");

  await page.goto("/sources");
  await expect(page.getByText("Employee Handbook")).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete…" })).toHaveCount(0);
  await expectAccessibleAndResponsive(page, "member /sources");

  await page.goto("/activity");
  await expect(
    page.getByText("Operator access is limited to administrators and owners."),
  ).toBeVisible();
  await expectAccessibleAndResponsive(page, "member /activity");

  await page.goto("/analytics");
  await expect(
    page.getByText("Analytics are limited to administrators and owners."),
  ).toBeVisible();
  await expectAccessibleAndResponsive(page, "member /analytics");

  await page.goto("/settings/ai-provider");
  await expect(page.getByTestId("provider-summary")).toBeVisible();
  await expect(page.getByRole("button", { name: "Activate GLM" })).toHaveCount(0);
  await expectAccessibleAndResponsive(page, "member /settings/ai-provider");

  expect(problems).toEqual([]);
});

test("keyboard navigation, visible focus ring, and orientation changes preserve workflow state", async ({
  page,
}) => {
  const problems = await watchConsole(page);
  await mockAllApis(page, "owner");

  await page.goto("/ask");
  await page.keyboard.press("Tab");
  const focusedInfo = await page.evaluate(() => {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    const style = window.getComputedStyle(active);
    return {
      tag: active.tagName.toLowerCase(),
      outlineStyle: style.outlineStyle,
      outlineWidth: parseFloat(style.outlineWidth),
    };
  });
  expect(focusedInfo).not.toBeNull();
  expect(focusedInfo?.outlineStyle).not.toBe("none");
  expect(focusedInfo?.outlineWidth ?? 0).toBeGreaterThanOrEqual(2);

  // Start an Ask workflow, open citation evidence, then rotate portrait <-> landscape across device classes
  await page.getByLabel("Your question").fill("What is the annual leave entitlement?");
  await page.getByLabel("Your question").press("Enter");
  await expect(page.getByRole("region", { name: "Answer" })).toContainText("18 days");

  const marker = page.getByRole("button", { name: "Open citation 1" });
  await marker.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", { name: "Evidence" })).toBeVisible();

  const initialViewport = page.viewportSize() ?? { width: 320, height: 844 };
  for (const size of [
    { width: 320, height: 844 },
    { width: 844, height: 320 },
    { width: 768, height: 1024 },
    { width: 1024, height: 768 },
    { width: 1440, height: 900 },
    initialViewport,
  ]) {
    await page.setViewportSize(size);
    await expect(page.getByLabel("Your question")).toHaveValue(
      "What is the annual leave entitlement?",
    );
    await expect(page.getByRole("region", { name: "Answer" })).toContainText("18 days");
    await expect(page.getByRole("complementary", { name: "Evidence" })).toContainText(
      "Employee Handbook",
    );
    await expectAccessibleAndResponsive(page, `orientation ${size.width}x${size.height}`);
  }

  expect(problems).toEqual([]);
});
