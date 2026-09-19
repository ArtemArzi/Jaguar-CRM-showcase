import { describe, expect, it, vi } from "vitest";

import { isDocumentRoute, navigateToDocumentRoute } from "./document-routes";

describe("isDocumentRoute", () => {
  it("accepts dashboard document routes", () => {
    expect(isDocumentRoute("/dashboard/")).toBe(true);
    expect(isDocumentRoute("/dashboard/students/42/")).toBe(true);
    expect(isDocumentRoute("/dashboard/students/?status=active#top")).toBe(true);
  });

  it("rejects PWA and public routes", () => {
    expect(isDocumentRoute("/dashboard")).toBe(false);
    expect(isDocumentRoute("/trainer")).toBe(false);
    expect(isDocumentRoute("/student/profile")).toBe(false);
    expect(isDocumentRoute("/login")).toBe(false);
    expect(isDocumentRoute("/parent-invite/token")).toBe(false);
  });
});

describe("navigateToDocumentRoute", () => {
  it("delegates navigation to window.location.assign", () => {
    const originalLocation = window.location;
    const assign = vi.fn();

    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, assign },
    });

    try {
      navigateToDocumentRoute("/dashboard/login/");
      expect(assign).toHaveBeenCalledWith("/dashboard/login/");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: originalLocation,
      });
    }
  });
});
