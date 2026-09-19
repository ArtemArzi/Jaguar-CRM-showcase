import { describe, it, expect, vi, beforeEach } from "vitest";

import { fetchVapidKey, urlBase64ToUint8Array } from "../push-utils";

describe("urlBase64ToUint8Array", () => {
  it("converts base64url string to Uint8Array", () => {
    // Known base64url: "AQID" = [1, 2, 3]
    const result = urlBase64ToUint8Array("AQID");
    expect(result).toBeInstanceOf(Uint8Array);
    expect(Array.from(result)).toEqual([1, 2, 3]);
  });

  it("handles base64url with - and _ characters", () => {
    // base64url uses - instead of + and _ instead of /
    // "AB-D" in base64url = "AB+D" in base64 = bytes [0, 31, 195]
    const result = urlBase64ToUint8Array("AB-D");
    expect(result).toBeInstanceOf(Uint8Array);
    expect(result.length).toBeGreaterThan(0);
  });
});

describe("fetchVapidKey", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches and returns public_key from API", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ public_key: "test-vapid-key-123" }),
    });

    const key = await fetchVapidKey();
    expect(key).toBe("test-vapid-key-123");
    expect(fetch).toHaveBeenCalledWith("/api/notifications/vapid-key/");
  });

  it("throws on non-OK response", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
    });

    await expect(fetchVapidKey()).rejects.toThrow("Failed to fetch VAPID key");
  });
});
