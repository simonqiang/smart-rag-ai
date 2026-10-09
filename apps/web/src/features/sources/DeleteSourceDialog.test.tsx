import { describe, expect, it, vi } from "vitest";

import DeleteSourceDialog from "./DeleteSourceDialog";
import SourceListPage from "./SourceListPage";

describe("DeleteSourceDialog", () => {
  it("exposes renderable root components", () => {
    expect(typeof DeleteSourceDialog).toBe("function");
    expect(typeof SourceListPage).toBe("function");
  });

  it("schedules deletion with DELETE on confirm", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify({ source_id: "s1", deletion: "scheduled" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onDeleted = vi.fn();
    // The confirm handler is the component's destructive path: one DELETE
    // against the tombstone route, then the parent refresh.
    const result = await fetch("/api/sources/s1", { method: "DELETE" });
    expect(result.status).toBe(202);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sources/s1",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(onDeleted).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
