import { describe, expect, it } from "vitest";

import BackupRestore, { formatSize, formatWhen } from "./BackupRestore";

describe("BackupRestore", () => {
  it("exposes a renderable root component", () => {
    expect(typeof BackupRestore).toBe("function");
  });
});

describe("formatSize", () => {
  it("keeps small backups in bytes", () => {
    expect(formatSize(512)).toBe("512 B");
  });

  it("scales to KiB and MiB", () => {
    expect(formatSize(2048)).toBe("2.0 KiB");
    expect(formatSize(5 * 1024 * 1024)).toBe("5.0 MiB");
  });
});

describe("formatWhen", () => {
  it("trims the ISO timestamp to minutes without locale drift", () => {
    expect(formatWhen("2026-10-22T09:07:41+00:00")).toBe("2026-10-22 09:07");
  });
});
