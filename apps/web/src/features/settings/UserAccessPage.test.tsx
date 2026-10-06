import { describe, expect, it } from "vitest";

import UserAccessPage, { parseCollections } from "./UserAccessPage";

describe("UserAccessPage", () => {
  it("exposes a renderable root component", () => {
    expect(typeof UserAccessPage).toBe("function");
  });
});

describe("parseCollections", () => {
  it("parses, trims, and dedupes comma-separated ids", () => {
    expect(parseCollections("col-a, col-b ,col-a")).toEqual(["col-a", "col-b"]);
  });

  it("drops empty entries", () => {
    expect(parseCollections("  , col-a ,, ")).toEqual(["col-a"]);
  });

  it("returns empty for blank input", () => {
    expect(parseCollections("")).toEqual([]);
  });
});
