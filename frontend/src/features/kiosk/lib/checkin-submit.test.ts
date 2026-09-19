import { beforeEach, describe, expect, it, vi } from "vitest";

const kioskCheckin = vi.hoisted(() => vi.fn());
const kioskGuestBookAndCheckin = vi.hoisted(() => vi.fn());
const fetchKioskOptions = vi.hoisted(() => vi.fn());
const addPendingCheckin = vi.hoisted(() => vi.fn());

vi.mock("./kiosk-api", () => ({
  fetchKioskOptions,
  kioskCheckin,
  kioskGuestBookAndCheckin,
}));

vi.mock("./kiosk-db", () => ({
  addPendingCheckin,
}));

import { submitKioskCheckinWithOfflineQueue } from "./checkin-submit";

const allowedKioskOptions = {
  student_id: 10,
  date: "2026-06-03",
  options: [
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
      self_checkin_status: "can_checkin",
      reason_code: "",
      financial_status: "subscription",
      subscription_id: 12,
      drop_in_price: null,
      existing_checkin_id: null,
      checkin_window_status: "open",
      checkin_opens_at: "2026-06-03T16:30:00+05:00",
      checkin_closes_at: "2026-06-03T18:00:00+05:00",
    },
  ],
};

describe("submitKioskCheckinWithOfflineQueue", () => {
  beforeEach(() => {
    fetchKioskOptions.mockReset();
    kioskCheckin.mockReset();
    kioskGuestBookAndCheckin.mockReset();
    addPendingCheckin.mockReset();
    fetchKioskOptions.mockResolvedValue(allowedKioskOptions);
    addPendingCheckin.mockResolvedValue(undefined);
  });

  it("submits directly while online", async () => {
    kioskCheckin.mockResolvedValue({
      checkin_id: 99,
      is_debt: false,
      subscription_id: 12,
      alerts: [],
    });

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).resolves.toMatchObject({ checkin_id: 99 });

    expect(fetchKioskOptions).toHaveBeenCalledWith(10, "2026-06-03");
    expect(kioskCheckin).toHaveBeenCalledWith(10, 42, 7, "2026-06-03");
    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("queues a pending check-in on online network failure", async () => {
    kioskCheckin.mockRejectedValue({
      code: "ERR_NETWORK",
      request: {},
    });

    const result = await submitKioskCheckinWithOfflineQueue({
      studentId: 10,
      scheduleId: 42,
      trainingTypeId: 7,
      checkinDate: "2026-06-03",
      online: true,
    });

    expect(addPendingCheckin).toHaveBeenCalledWith({
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
    });
    expect(result).toMatchObject({
      checkin_id: -1,
      is_debt: false,
      subscription_id: null,
      alerts: [
        {
          type: "offline",
          icon: "wifi-off",
        },
      ],
    });
  });

  it("queues a pending check-in when the preflight request fails on network", async () => {
    fetchKioskOptions.mockRejectedValue({
      code: "ERR_NETWORK",
      request: {},
    });

    const result = await submitKioskCheckinWithOfflineQueue({
      studentId: 10,
      scheduleId: 42,
      trainingTypeId: 7,
      checkinDate: "2026-06-03",
      online: true,
    });

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(addPendingCheckin).toHaveBeenCalledWith({
      student_id: 10,
      schedule_id: 42,
      training_type_id: 7,
      checkin_date: "2026-06-03",
    });
    expect(result.alerts[0]).toMatchObject({ type: "offline" });
  });

  it("books and checks in guest-bookable subscribed sessions online", async () => {
    fetchKioskOptions.mockResolvedValue({
      ...allowedKioskOptions,
      options: [
        {
          ...allowedKioskOptions.options[0],
          self_checkin_status: "can_book_guest_visit",
          subscription_id: null,
        },
      ],
    });
    kioskGuestBookAndCheckin.mockResolvedValue({
      checkin_id: 101,
      is_debt: false,
      subscription_id: 12,
      alerts: [],
      created: true,
      duplicate: false,
      subscription_effect: "deducted",
      debt_effect: "none",
    });

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).resolves.toMatchObject({
      checkin_id: 101,
      subscription_id: 12,
      created: true,
      alerts: [],
    });

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(kioskGuestBookAndCheckin).toHaveBeenCalledWith(
      10,
      42,
      7,
      "2026-06-03",
    );
    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("delegates drop-in guest-bookable sessions to the server", async () => {
    fetchKioskOptions.mockResolvedValue({
      ...allowedKioskOptions,
      options: [
        {
          ...allowedKioskOptions.options[0],
          self_checkin_status: "can_book_guest_visit",
          financial_status: "drop_in_debt",
          subscription_id: null,
          drop_in_price: "1500.00",
        },
      ],
    });
    kioskGuestBookAndCheckin.mockResolvedValue({
      checkin_id: 102,
      is_debt: true,
      subscription_id: null,
      alerts: [],
      created: true,
      duplicate: false,
      debt_effect: "created",
    });

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).resolves.toMatchObject({
      checkin_id: 102,
      is_debt: true,
      debt_effect: "created",
    });

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(kioskGuestBookAndCheckin).toHaveBeenCalledWith(
      10,
      42,
      7,
      "2026-06-03",
    );
    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("does not queue guest booking network failures as offline retries", async () => {
    const networkError = {
      code: "ERR_NETWORK",
      request: {},
    };
    fetchKioskOptions.mockResolvedValue({
      ...allowedKioskOptions,
      options: [
        {
          ...allowedKioskOptions.options[0],
          self_checkin_status: "can_book_guest_visit",
          subscription_id: null,
        },
      ],
    });
    kioskGuestBookAndCheckin.mockRejectedValue(networkError);

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).rejects.toBe(networkError);

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(kioskGuestBookAndCheckin).toHaveBeenCalledWith(
      10,
      42,
      7,
      "2026-06-03",
    );
    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("does not queue blocked preflight results as offline retries", async () => {
    fetchKioskOptions.mockResolvedValue({
      ...allowedKioskOptions,
      options: [
        {
          ...allowedKioskOptions.options[0],
          self_checkin_status: "blocked",
          reason_code: "already_checked_in",
          existing_checkin_id: 99,
        },
      ],
    });

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).rejects.toMatchObject({
      data: { code: "already_checked_in" },
    });

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("does not post when preflight omits the selected schedule", async () => {
    fetchKioskOptions.mockResolvedValue({
      ...allowedKioskOptions,
      options: [],
    });

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).rejects.toMatchObject({
      data: { code: "schedule_occurrence_not_found" },
    });

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("does not queue server-side check-in errors", async () => {
    const serverError = {
      response: { status: 422 },
      message: "Invalid check-in",
    };
    kioskCheckin.mockRejectedValue(serverError);

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).rejects.toBe(serverError);

    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("does not queue frozen business rejections as offline retries", async () => {
    const frozenError = {
      response: {
        status: 409,
        data: {
          checkin_blocked_reason: "enrollment_frozen",
          detail: "Абонемент заморожен",
        },
      },
    };
    kioskCheckin.mockRejectedValue(frozenError);

    await expect(
      submitKioskCheckinWithOfflineQueue({
        studentId: 10,
        scheduleId: 42,
        trainingTypeId: 7,
        checkinDate: "2026-06-03",
        online: true,
      }),
    ).rejects.toBe(frozenError);

    expect(addPendingCheckin).not.toHaveBeenCalled();
  });

  it("queues immediately while offline", async () => {
    const result = await submitKioskCheckinWithOfflineQueue({
      studentId: 10,
      scheduleId: 42,
      trainingTypeId: 7,
      checkinDate: "2026-06-03",
      online: false,
    });

    expect(kioskCheckin).not.toHaveBeenCalled();
    expect(fetchKioskOptions).not.toHaveBeenCalled();
    expect(addPendingCheckin).toHaveBeenCalledOnce();
    expect(result.alerts[0]).toMatchObject({ type: "offline" });
  });
});
