import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createDirectPersonalStaffIntent,
  createPersonalStaffIntent,
} from "./personal-commercial-context-api";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { post } }));

const receipt = {
  kind: "personal_staff_intent",
  booking_id: 71,
  payment_id: 91,
  subscription_id: 101,
  reservation_id: null,
  bank_payment_order_id: null,
  debt_id: null,
  slot_id: 81,
  schedule_id: 101,
  enrollment_id: 102,
  starts_at: "2099-07-07T10:00:00+05:00",
  ends_at: "2099-07-07T11:00:00+05:00",
  trainer_id: 9,
  trainer_name: "Тренер",
  location_id: 11,
  location_name: "Основной зал",
  training_type_id: 22,
  training_type_name: "Персоналка",
  tariff_id: 44,
  tariff_name: "Разовая персоналка",
  amount: "2500.00",
  payment_method: "cash",
  status: "pending",
  allowed_actions: [],
  resource_route: "/api/personal-drop-in-bookings/71/",
  workspace_state: "student",
  finance_state: "pending_manual",
  command_replayed: false,
};

describe("versioned personal staff intents", () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({ data: receipt });
  });

  it("keeps a v1 tenant on the compatible slot route", async () => {
    await createPersonalStaffIntent({
      slotId: 81,
      protocolVersion: "v1",
      payload: {
        student_id: 7,
        payment_method: "cash",
        offer_digest: "slot-offer",
        idempotency_key: "v1-slot",
      },
    } as never);

    expect(post).toHaveBeenCalledWith(
      "/personal-availability/slots/81/staff-intents/",
      expect.not.objectContaining({ protocol_version: "v2" }),
    );
  });

  it("uses frozen v2 routes for both slot and direct cash commands", async () => {
    await createPersonalStaffIntent({
      slotId: 81,
      protocolVersion: "v2",
      payload: {
        student_id: 7,
        payment_method: "cash",
        offer_digest: "slot-offer",
        idempotency_key: "v2-slot",
      },
    } as never);
    await createDirectPersonalStaffIntent({
      protocolVersion: "v2",
      student_id: 7,
      payment_method: "transfer",
      offer_digest: "direct-offer",
      idempotency_key: "v2-direct",
      trainer_id: 9,
      starts_at: "2099-07-07T10:00:00+05:00",
      ends_at: "2099-07-07T11:00:00+05:00",
      location_id: 11,
      training_type_id: 22,
    } as never);

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/personal-availability/v2/slots/81/staff-intents/",
      expect.objectContaining({ protocol_version: "v2" }),
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/personal-availability/v2/staff-intents/direct/",
      expect.objectContaining({ protocol_version: "v2" }),
    );
  });
});
