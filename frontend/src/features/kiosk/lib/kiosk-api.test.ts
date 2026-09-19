import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";
import { beforeEach, describe, expect, it, vi } from "vitest";

const clearKioskOfflineData = vi.hoisted(() => vi.fn());

vi.mock("./kiosk-db", () => ({
  clearKioskOfflineData,
}));

import {
  activateKiosk,
  autoDetectTraining,
  fetchBranding,
  fetchKioskOptions,
  fetchKioskRoster,
  fetchTodaySchedules,
  kioskApi,
  kioskCheckin,
  kioskGuestBookAndCheckin,
  lookupStudents,
} from "./kiosk-api";
import { useKioskStore } from "./kiosk-store";

describe("kiosk API client", () => {
  let requests: InternalAxiosRequestConfig[];

  beforeEach(() => {
    requests = [];
    localStorage.clear();
    clearKioskOfflineData.mockReset();
    clearKioskOfflineData.mockResolvedValue(undefined);
    useKioskStore.setState({
      deviceToken: null,
      clubId: null,
      clubName: null,
      isActivated: false,
    });
    kioskApi.defaults.adapter = ((config) => {
      requests.push(config);
      return Promise.resolve({
        data: {},
        status: 200,
        statusText: "OK",
        headers: {},
        config,
      });
    }) as AxiosAdapter;
  });

  it("activates through the checkins kiosk route", async () => {
    await activateKiosk("123456");

    expect(requests[0]).toMatchObject({
      method: "post",
      url: "/checkins/kiosk/activate/",
    });
  });

  it("keeps phone lookup constrained to exactly 4 digits", async () => {
    await expect(lookupStudents("123")).rejects.toThrow(/4/);
    await expect(lookupStudents("12345")).rejects.toThrow(/4/);
    await expect(lookupStudents("12a4")).rejects.toThrow(/4/);

    expect(requests).toHaveLength(0);

    await lookupStudents("1234");

    expect(requests[0]).toMatchObject({
      method: "post",
      url: "/checkins/kiosk/lookup/",
      data: JSON.stringify({ phone_suffix: "1234" }),
    });
  });

  it("fetches the explicit kiosk roster endpoint", async () => {
    await fetchKioskRoster();

    expect(requests[0]).toMatchObject({
      method: "get",
      url: "/checkins/kiosk/roster/",
    });
  });

  it("fetches kiosk check-in options for a selected student and date", async () => {
    await fetchKioskOptions(10, "2026-06-03");

    expect(requests[0]).toMatchObject({
      method: "post",
      url: "/checkins/kiosk/options/",
      data: JSON.stringify({
        student_id: 10,
        date: "2026-06-03",
      }),
    });
  });

  it.each([401, 403])(
    "deactivates the kiosk but preserves pending check-ins on authenticated %s responses",
    async (status) => {
      await useKioskStore.getState().activate("device-token", 7, "Jaguar");
      clearKioskOfflineData.mockClear();
      kioskApi.defaults.adapter = ((config) => {
        requests.push(config);
        return Promise.reject({
          response: { status },
          config,
        });
      }) as AxiosAdapter;

      await expect(fetchKioskRoster()).rejects.toMatchObject({
        response: { status },
      });

      expect(useKioskStore.getState().isActivated).toBe(false);
      expect(localStorage.getItem("kiosk_device_token")).toBeNull();
      expect(clearKioskOfflineData).toHaveBeenCalledOnce();
      expect(clearKioskOfflineData).toHaveBeenCalledWith({ preservePending: true });
    },
  );

  it("does not deactivate the kiosk on activation 401 responses", async () => {
    await useKioskStore.getState().activate("device-token", 7, "Jaguar");
    clearKioskOfflineData.mockClear();
    kioskApi.defaults.adapter = ((config) => {
      requests.push(config);
      return Promise.reject({
        response: { status: 401 },
        config,
      });
    }) as AxiosAdapter;

    await expect(activateKiosk("123456")).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(useKioskStore.getState().isActivated).toBe(true);
    expect(clearKioskOfflineData).not.toHaveBeenCalled();
  });

  it("fetches the kiosk-safe branding endpoint", async () => {
    await fetchBranding();

    expect(requests[0]).toMatchObject({
      method: "get",
      url: "/checkins/kiosk/branding/",
    });
  });

  it("normalizes kiosk schedule occurrences at the API boundary", async () => {
    kioskApi.defaults.adapter = ((config) => {
      requests.push(config);
      return Promise.resolve({
        data: [
          {
            schedule_id: 42,
            effective_date: "2026-06-03",
            effective_start_time: "17:00:00",
            effective_end_time: "18:00:00",
            group_name: "Kids",
            trainer_name: "Coach",
            location_name: "Tatami 1",
            training_type_id: 7,
            training_type_name: "Group",
          },
        ],
        status: 200,
        statusText: "OK",
        headers: {},
        config,
      });
    }) as AxiosAdapter;

    await expect(fetchTodaySchedules()).resolves.toEqual([
      {
        schedule_id: 42,
        effective_date: "2026-06-03",
        start_time: "17:00:00",
        end_time: "18:00:00",
        group_name: "Kids",
        trainer_name: "Coach",
        location_name: "Tatami 1",
        training_type_id: 7,
        training_type_name: "Group",
      },
    ]);
    expect(requests[0]).toMatchObject({
      method: "get",
      url: "/checkins/kiosk/schedules/today/",
    });
  });

  it("sends the selected occurrence date with kiosk check-in when provided", async () => {
    await kioskCheckin(10, 42, 7, "2026-06-03");

    expect(requests[0]).toMatchObject({
      method: "post",
      url: "/checkins/kiosk/",
      data: JSON.stringify({
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
      }),
    });
  });

  it("posts kiosk guest booking with immediate check-in fields", async () => {
    await kioskGuestBookAndCheckin(10, 42, 7, "2026-06-03");

    expect(requests[0]).toMatchObject({
      method: "post",
      url: "/checkins/kiosk/guest-book-and-checkin/",
      data: JSON.stringify({
        student_id: 10,
        schedule_id: 42,
        training_type_id: 7,
        checkin_date: "2026-06-03",
      }),
    });
  });

  it("auto-detects normalized schedule occurrences by effective start/end", () => {
    vi.setSystemTime(new Date("2026-06-03T17:10:00"));

    expect(
      autoDetectTraining([
        {
          schedule_id: 42,
          effective_date: "2026-06-03",
          start_time: "17:00:00",
          end_time: "18:00:00",
          group_name: "Kids",
          trainer_name: "Coach",
          location_name: "Tatami 1",
          training_type_id: 7,
          training_type_name: "Group",
        },
      ]),
    ).toMatchObject({ schedule_id: 42, effective_date: "2026-06-03" });

    vi.useRealTimers();
  });

  it("does not auto-detect stale schedule occurrences from another date", () => {
    vi.setSystemTime(new Date("2026-06-03T17:10:00"));

    expect(
      autoDetectTraining([
        {
          schedule_id: 42,
          effective_date: "2026-06-02",
          start_time: "17:00:00",
          end_time: "18:00:00",
          group_name: "Kids",
          trainer_name: "Coach",
          location_name: "Tatami 1",
          training_type_id: 7,
          training_type_name: "Group",
        },
      ]),
    ).toBeNull();

    vi.useRealTimers();
  });

  it("does not auto-detect schedules without a training type", () => {
    vi.setSystemTime(new Date("2026-06-03T17:10:00"));

    expect(
      autoDetectTraining([
        {
          schedule_id: 42,
          effective_date: "2026-06-03",
          start_time: "17:00:00",
          end_time: "18:00:00",
          group_name: "Kids",
          trainer_name: "Coach",
          location_name: "Tatami 1",
          training_type_id: null,
          training_type_name: "",
        },
      ]),
    ).toBeNull();

    vi.useRealTimers();
  });
});
