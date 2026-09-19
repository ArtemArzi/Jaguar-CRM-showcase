import { describe, expect, it } from "vitest";
import { normalizeStudentSubscriptionsPayload } from "./subscriptions";

describe("normalizeStudentSubscriptionsPayload", () => {
  it("normalizes paginated billing subscription payloads with nested tariff data", () => {
    expect(
      normalizeStudentSubscriptionsPayload({
        items: [
          {
            id: 7,
            tariff: {
              name: "Backend BJJ",
              trainings_limit: 12,
              training_type: {
                id: 15,
                name: "Персональная",
                kind: "personal",
              },
            },
            trainings_used: 3,
            trainings_left: 9,
            expires_at: "2026-07-01T00:00:00Z",
            status: "active",
            training_type_kind: "personal",
            package_owner_trainer_id: 4,
            package_owner_trainer_name: "Owner Coach",
          },
        ],
      }),
    ).toEqual([
      {
        id: 7,
        tariff_name: "Backend BJJ",
        training_type_id: 15,
        training_type_name: "Персональная",
        trainings_used: 3,
        trainings_total: 12,
        trainings_left: 9,
        expires_at: "2026-07-01T00:00:00Z",
        status: "active",
        paid_amount: undefined,
        freeze_status: null,
        training_type_kind: "personal",
        package_owner_trainer_id: 4,
        package_owner_trainer_name: "Owner Coach",
      },
    ]);
  });

  it("keeps already-flat student subscription payloads compatible", () => {
    expect(
      normalizeStudentSubscriptionsPayload([
        {
          id: 8,
          tariff_name: "Flat Boxing",
          trainings_used: 1,
          trainings_total: 8,
          trainings_left: 7,
          expires_at: null,
          status: "pending",
        },
      ]),
    ).toEqual([
      {
        id: 8,
        tariff_name: "Flat Boxing",
        training_type_id: null,
        training_type_name: "",
        trainings_used: 1,
        trainings_total: 8,
        trainings_left: 7,
        expires_at: null,
        status: "pending",
        paid_amount: undefined,
        freeze_status: null,
        training_type_kind: "",
        package_owner_trainer_id: null,
        package_owner_trainer_name: "",
      },
    ]);
  });

  it("preserves the server-selected renewal target alongside the original subscription", () => {
    expect(
      normalizeStudentSubscriptionsPayload([
        {
          id: 9,
          tariff_name: "Bought Base",
          renewal_target_tariff_id: 12,
          renewal_target_tariff_name: "Current Base",
          renewal_target_price: "6500.00",
          trainings_used: 2,
          trainings_total: 8,
          trainings_left: 6,
          expires_at: "2026-09-01",
          status: "active",
        },
      ]),
    ).toEqual([
      expect.objectContaining({
        id: 9,
        tariff_name: "Bought Base",
        renewal_target_tariff_id: 12,
        renewal_target_tariff_name: "Current Base",
        renewal_target_price: "6500.00",
      }),
    ]);
  });
});
