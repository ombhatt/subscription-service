import { describe, expect, it } from "vitest";

import { safeNext } from "./redirect";

describe("safeNext", () => {
  it("follows a path on this origin, query and hash included", () => {
    expect(safeNext("/chat")).toBe("/chat");
    expect(safeNext("/billing?x=1#plans")).toBe("/billing?x=1#plans");
    expect(safeNext("/")).toBe("/");
  });

  it("falls back when there is no next", () => {
    expect(safeNext(null)).toBe("/billing");
    expect(safeNext("")).toBe("/billing");
    expect(safeNext(undefined, "/")).toBe("/");
  });

  it.each([
    "javascript:alert(document.cookie)",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "https://evil.example/login",
    "//evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "/\t/evil.example",
    "/\n/evil.example",
    "//[",
    "chat",
  ])("refuses %j", (next) => {
    expect(safeNext(next)).toBe("/billing");
  });
});
