import { describe, expect, it } from "vitest";

import { getContrastingAccent, isDarkBackground } from "./use-contrast-color";

describe("isDarkBackground", () => {
  it("treats missing or invalid colors as dark fallback", () => {
    expect(isDarkBackground(undefined)).toBe(true);
    expect(isDarkBackground("not-a-color")).toBe(true);
  });

  it("classifies dark and light hex backgrounds by luminance", () => {
    expect(isDarkBackground("#1A1A1A")).toBe(true);
    expect(isDarkBackground("#F4F4F5")).toBe(false);
  });

  it("keeps threshold-adjacent mid colors on the dark side", () => {
    expect(isDarkBackground("#888888")).toBe(true);
  });
});

describe("getContrastingAccent", () => {
  it("keeps an accent when contrast is high enough", () => {
    expect(getContrastingAccent("#111827", "#F59E0B")).toBe("#F59E0B");
  });

  it("falls back to white on low-contrast dark backgrounds", () => {
    expect(getContrastingAccent("#1A1A1A", "#222222")).toBe("#FFFFFF");
  });

  it("falls back to dark text on low-contrast light backgrounds", () => {
    expect(getContrastingAccent("#F4F4F5", "#FFFFFF")).toBe("#1A1A1A");
  });

  it("uses white fallback when inputs are missing or invalid", () => {
    expect(getContrastingAccent(undefined, "#FFFFFF")).toBe("#FFFFFF");
    expect(getContrastingAccent("#000000", "bad")).toBe("#FFFFFF");
  });
});
