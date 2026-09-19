import { beforeEach, describe, expect, it, vi } from "vitest";

const clearKioskOfflineData = vi.hoisted(() => vi.fn());

vi.mock("./kiosk-db", () => ({
  clearKioskOfflineData,
}));

import { useKioskStore } from "./kiosk-store";

describe("kiosk store", () => {
  beforeEach(() => {
    localStorage.clear();
    clearKioskOfflineData.mockReset();
    clearKioskOfflineData.mockResolvedValue(undefined);
    useKioskStore.setState({
      deviceToken: null,
      clubId: null,
      clubName: null,
      isActivated: false,
    });
  });

  it("deactivate removes device state and clears offline kiosk data", async () => {
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");
    clearKioskOfflineData.mockClear();

    await useKioskStore.getState().deactivate();

    expect(useKioskStore.getState()).toMatchObject({
      deviceToken: null,
      clubId: null,
      clubName: null,
      isActivated: false,
    });
    expect(localStorage.getItem("kiosk_device_token")).toBeNull();
    expect(localStorage.getItem("kiosk_club_id")).toBeNull();
    expect(localStorage.getItem("kiosk_club_name")).toBeNull();
    expect(clearKioskOfflineData).toHaveBeenCalledOnce();
  });

  it("deactivate can preserve pending check-ins while removing device state", async () => {
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");
    clearKioskOfflineData.mockClear();

    await useKioskStore.getState().deactivate({ preservePending: true });

    expect(useKioskStore.getState()).toMatchObject({
      deviceToken: null,
      clubId: null,
      clubName: null,
      isActivated: false,
    });
    expect(localStorage.getItem("kiosk_device_token")).toBeNull();
    expect(clearKioskOfflineData).toHaveBeenCalledOnce();
    expect(clearKioskOfflineData).toHaveBeenCalledWith({ preservePending: true });
  });

  it("clears offline data before accepting a new activation", async () => {
    useKioskStore.setState({
      deviceToken: "old-token",
      clubId: 7,
      clubName: "Old Club",
      isActivated: true,
    });
    localStorage.setItem("kiosk_device_token", "old-token");
    localStorage.setItem("kiosk_club_id", "7");
    localStorage.setItem("kiosk_club_name", "Old Club");

    clearKioskOfflineData.mockImplementation(async () => {
      expect(useKioskStore.getState().deviceToken).toBe("old-token");
      expect(localStorage.getItem("kiosk_device_token")).toBe("old-token");
    });

    await useKioskStore.getState().activate("new-token", 9, "New Club");

    expect(clearKioskOfflineData).toHaveBeenCalledOnce();
    expect(clearKioskOfflineData).toHaveBeenCalledWith({ preservePending: false });
    expect(useKioskStore.getState()).toMatchObject({
      deviceToken: "new-token",
      clubId: 9,
      clubName: "New Club",
      isActivated: true,
    });
  });

  it("preserves pending check-ins when reactivating the same club", async () => {
    useKioskStore.setState({
      deviceToken: "old-token",
      clubId: 7,
      clubName: "Old Club",
      isActivated: true,
    });
    localStorage.setItem("kiosk_device_token", "old-token");
    localStorage.setItem("kiosk_club_id", "7");
    localStorage.setItem("kiosk_club_name", "Old Club");

    await useKioskStore.getState().activate("new-token", 7, "Jaguar");

    expect(clearKioskOfflineData).toHaveBeenCalledOnce();
    expect(clearKioskOfflineData).toHaveBeenCalledWith({ preservePending: true });
    expect(useKioskStore.getState()).toMatchObject({
      deviceToken: "new-token",
      clubId: 7,
      clubName: "Jaguar",
      isActivated: true,
    });
  });
});
