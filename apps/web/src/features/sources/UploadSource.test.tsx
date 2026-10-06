import { describe, expect, it } from "vitest";

import UploadSource, { uploadBody } from "./UploadSource";

describe("UploadSource", () => {
  it("exposes a renderable root component", () => {
    expect(typeof UploadSource).toBe("function");
  });
});

describe("uploadBody", () => {
  it("packs file, collection, and name for the multipart API", () => {
    const file = new File([new Uint8Array([0x25])], "handbook.pdf", { type: "application/pdf" });
    const body = uploadBody(file, "c1", "Handbook");
    expect(body.get("collection_id")).toBe("c1");
    expect(body.get("name")).toBe("Handbook");
    expect(body.get("file")).toBeInstanceOf(File);
  });
});
