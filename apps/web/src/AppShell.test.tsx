import { describe, expect, it } from "vitest";

import AppShell from "./AppShell";
import SourceListPage from "./features/sources/SourceListPage";

describe("AppShell", () => {
  it("exposes a renderable root component", () => {
    expect(typeof AppShell).toBe("function");
  });
});

describe("SourceListPage", () => {
  it("exposes a renderable root component", () => {
    expect(typeof SourceListPage).toBe("function");
  });
});
