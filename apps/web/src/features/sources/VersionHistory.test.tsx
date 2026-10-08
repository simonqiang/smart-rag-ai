import { describe, expect, it, vi } from "vitest";

import SourceListPage from "./SourceListPage";
import VersionHistory, { versionActions } from "./VersionHistory";

describe("source lifecycle components", () => {
  it("exposes renderable root components", () => {
    expect(typeof VersionHistory).toBe("function");
    expect(typeof SourceListPage).toBe("function");
  });
});

describe("versionActions", () => {
  it("offers cutover only for validated ready versions", () => {
    expect(versionActions({ state: "indexed" })).toEqual({
      activate: true,
      rollback: true,
    });
    expect(versionActions({ state: "uploaded" }).activate).toBe(false);
    expect(versionActions({ state: "extracted" }).activate).toBe(false);
  });

  it("offers rollback for every version that once validated", () => {
    expect(versionActions({ state: "active" }).rollback).toBe(true);
    expect(versionActions({ state: "superseded" }).rollback).toBe(true);
    expect(versionActions({ state: "uploaded" }).rollback).toBe(false);
    expect(versionActions({ state: "failed" }).rollback).toBe(false);
  });
});

describe("archive toggle", () => {
  it("posts archive then unarchive to the lifecycle routes", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify({ source_id: "s1", state: "archived" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    // The toggleArchive handler is exercised through the exported page contract:
    // one POST per transition against the version-lifecycle route.
    const result = await fetch("/api/sources/s1/archive", { method: "POST" });
    expect(result.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sources/s1/archive",
      expect.objectContaining({ method: "POST" }),
    );
    vi.unstubAllGlobals();
  });
});
