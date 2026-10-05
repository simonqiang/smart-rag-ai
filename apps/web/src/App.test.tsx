import { describe, expect, it } from "vitest";
import App from "./App";

describe("App", () => {
  it("exposes a renderable root component", () => {
    expect(typeof App).toBe("function");
  });
});
