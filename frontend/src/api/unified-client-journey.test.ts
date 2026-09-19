import { describe, expect, it } from "vitest";
import {
  getPersonalStaffCommandProtocol,
  getPersonalAvailabilityCapabilityMode,
  getPersonalAvailabilityCapabilityQueryKey,
  getUnifiedClientJourneyCapabilityMode,
  getUnifiedClientJourneyCapabilityQueryKey,
} from "./unified-client-journey";

describe("personal availability capability", () => {
  it("fails closed when a cached explicit value receives a refetch error", () => {
    expect(
      getPersonalAvailabilityCapabilityMode({
        data: { enabled: true },
        isSuccess: true,
        isRefetchError: true,
      }),
    ).toBe("unavailable");
    expect(
      getPersonalAvailabilityCapabilityMode({
        data: { enabled: false },
        isSuccess: true,
        isRefetchError: true,
      }),
    ).toBe("unavailable");
  });

  it("requires a successful explicit boolean and keeps capability cache actor-and-club-scoped", () => {
    expect(getPersonalAvailabilityCapabilityMode({ data: { enabled: true }, isSuccess: true })).toBe(
      "unified",
    );
    expect(getPersonalAvailabilityCapabilityMode({ data: { enabled: false }, isSuccess: true })).toBe(
      "legacy",
    );
    expect(getPersonalAvailabilityCapabilityMode({ data: {}, isSuccess: true })).toBe("unavailable");
    expect(getPersonalAvailabilityCapabilityQueryKey(1)).not.toEqual(
      getPersonalAvailabilityCapabilityQueryKey(2),
    );
    expect(
      getPersonalAvailabilityCapabilityQueryKey(1, "header.eyJzdWIiOiJ0cmFpbmVyLTEifQ.signature", "trainer"),
    ).not.toEqual(
      getPersonalAvailabilityCapabilityQueryKey(1, "header.eyJzdWIiOiJ0cmFpbmVyLTIifQ.signature", "trainer"),
    );
    expect(
      getPersonalStaffCommandProtocol({
        data: { enabled: true, staff_command_protocol_version: "v2" },
        isSuccess: true,
      }),
    ).toBe("v2");
    expect(getPersonalStaffCommandProtocol({ data: { enabled: true }, isSuccess: true })).toBeNull();
  });
});

describe("unified client journey capability", () => {
  it("fails closed after a refetch error and scopes cache entries to the club", () => {
    expect(
      getUnifiedClientJourneyCapabilityMode({
        data: { enabled: true },
        isSuccess: true,
        isRefetchError: true,
      }),
    ).toBe("unavailable");
    expect(
      getUnifiedClientJourneyCapabilityMode({ data: { enabled: true }, isSuccess: true }),
    ).toBe("unified");
    expect(
      getUnifiedClientJourneyCapabilityMode({ data: { enabled: false }, isSuccess: true }),
    ).toBe("legacy");
    expect(getUnifiedClientJourneyCapabilityQueryKey(1)).not.toEqual(
      getUnifiedClientJourneyCapabilityQueryKey(2),
    );
    expect(getUnifiedClientJourneyCapabilityQueryKey(null)).toContain("no-club");
  });
});
