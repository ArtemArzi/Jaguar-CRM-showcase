import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  blockTrainerAvailabilitySlot,
  cancelTrainerAvailabilitySlot,
  generateTrainerAvailability,
  listTrainerAvailability,
  normalizePersonalOffer,
  personalOfferQueryKey,
  previewTrainerPersonalOffer,
  unblockTrainerAvailabilitySlot,
} from "./personal-availability";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

describe("trainer personal availability api helpers", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue({ data: [] });
    post.mockResolvedValue({ data: {} });
  });

  it("lists trainer availability without sending trainer_id", async () => {
    await listTrainerAvailability({
      dateFrom: "2026-06-22",
      dateTo: "2026-06-28",
    });

    expect(get).toHaveBeenCalledWith("/personal-availability/slots/", {
      params: { date_from: "2026-06-22", date_to: "2026-06-28" },
    });
  });

  it("keeps an explicit trainer scope for owner/admin availability reads", async () => {
    await listTrainerAvailability({
      dateFrom: "2026-06-22",
      dateTo: "2026-06-28",
      trainerId: 9,
    });

    expect(get).toHaveBeenCalledWith("/personal-availability/slots/", {
      params: { date_from: "2026-06-22", date_to: "2026-06-28", trainer_id: 9 },
    });
  });

  it("posts weekly generation payload to the trainer slots endpoint", async () => {
    await generateTrainerAvailability({
      date_from: "2026-06-22",
      date_to: "2026-06-28",
      weekdays: [0, 2],
      start_time: "10:00",
      end_time: "14:00",
      slot_duration_minutes: 60,
      buffer_minutes: 15,
      location_id: 2,
      training_type_id: 7,
    });

    expect(post).toHaveBeenCalledWith("/personal-availability/slots/generate/", {
      date_from: "2026-06-22",
      date_to: "2026-06-28",
      weekdays: [0, 2],
      start_time: "10:00",
      end_time: "14:00",
      slot_duration_minutes: 60,
      buffer_minutes: 15,
      location_id: 2,
      training_type_id: 7,
    });
  });

  it("posts slot action endpoints", async () => {
    await blockTrainerAvailabilitySlot({ slotId: 5, reason: "занят" });
    await unblockTrainerAvailabilitySlot(6);
    await cancelTrainerAvailabilitySlot(7);

    expect(post).toHaveBeenCalledWith(
      "/personal-availability/slots/5/block/",
      { reason: "занят" },
    );
    expect(post).toHaveBeenCalledWith(
      "/personal-availability/slots/6/unblock/",
      {},
    );
    expect(post).toHaveBeenCalledWith(
      "/personal-availability/slots/7/cancel/",
      {},
    );
  });

  it("scopes direct offer previews by both training type and location without inventing a digest", async () => {
    get.mockResolvedValueOnce({
      data: {
        offer_tariff_id: 44,
        offer_tariff_name: "Персоналка по назначенной цене",
        offer_price: "2500.00",
      },
    });

    await expect(
      previewTrainerPersonalOffer({ trainingTypeId: 7, locationId: 2 }),
    ).resolves.toEqual({
      tariffId: 44,
      tariffName: "Персоналка по назначенной цене",
      price: "2500.00",
      baseAmount: "2500.00",
      discountId: undefined,
      discountName: undefined,
      discountType: undefined,
      discountValue: undefined,
      discountAmount: 0,
      payableAmount: "2500.00",
      trainerId: undefined,
      digest: undefined,
      errorCode: undefined,
    });
    expect(personalOfferQueryKey({ trainingTypeId: 7, locationId: 2 })).toEqual([
      "trainer",
      "personal-offer",
      7,
      2,
    ]);
    expect(get).toHaveBeenCalledWith("/personal-availability/offers/", {
      params: { training_type_id: 7, location_id: 2 },
    });
  });

  it("keeps a typed configuration error when the server has no usable offer", () => {
    expect(
      normalizePersonalOffer({ offer_error_code: "personal_booking_tariff_not_configured" }),
    ).toEqual({ errorCode: "personal_booking_tariff_not_configured" });
  });

  it("keeps the server-owned trainer, discount and payable evidence while previewing one exact slot", async () => {
    get.mockResolvedValueOnce({
      data: {
        offer_tariff_id: 44,
        offer_tariff_name: "Персоналка тренера",
        offer_price: "1800.00",
        offer_base_amount: "1800.00",
        offer_trainer_id: 9,
        offer_discount_id: 22,
        offer_discount_name: "Семейная",
        offer_discount_type: "fixed",
        offer_discount_value: "300.00",
        offer_discount_amount: "300.00",
        offer_payable_amount: "1500.00",
        offer_digest: "slot-discount-v1",
      },
    });

    await expect(
      previewTrainerPersonalOffer({
        trainingTypeId: 7,
        locationId: 2,
        trainerId: 9,
        slotId: 81,
        discountId: 22,
      }),
    ).resolves.toMatchObject({
      trainerId: 9,
      baseAmount: "1800.00",
      discountId: 22,
      discountName: "Семейная",
      discountType: "fixed",
      discountValue: "300.00",
      discountAmount: "300.00",
      payableAmount: "1500.00",
      digest: "slot-discount-v1",
    });
    expect(get).toHaveBeenCalledWith("/personal-availability/offers/", {
      params: {
        training_type_id: 7,
        location_id: 2,
        trainer_id: 9,
        slot_id: 81,
        discount_id: 22,
      },
    });
  });
});
