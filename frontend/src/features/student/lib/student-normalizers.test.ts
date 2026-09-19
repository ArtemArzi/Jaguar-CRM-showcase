import { describe, expect, it } from "vitest";
import {
  normalizeChecklistItems,
  readAttendanceSummary,
} from "./student-normalizers";

describe("readAttendanceSummary", () => {
  it("uses attended_count instead of current page length", () => {
    const result = readAttendanceSummary({
      attended_count: 42,
      items: [
        {
          id: 1,
          date: "2026-04-10",
          group_name: "Class 5",
          trainer_name: "Trainer",
          location_name: "Location",
          training_type_name: "Muay Thai",
          start_time: "14:00",
        },
      ],
    });

    expect(result.totalCount).toBe(42);
    expect(result.items).toHaveLength(1);
  });
});

describe("normalizeChecklistItems", () => {
  it("maps nested checklist contract into frontend-friendly rows", () => {
    const result = normalizeChecklistItems([
      {
        document_type: {
          id: 7,
          name: "Медицинская справка",
          description: "Актуальная справка",
          is_required: true,
          is_active: true,
        },
        is_provided: true,
        has_file: true,
      },
    ]);

    expect(result).toEqual([
      {
        documentTypeId: 7,
        documentTypeName: "Медицинская справка",
        description: "Актуальная справка",
        isRequired: true,
        isActive: true,
        isProvided: true,
        hasFile: true,
      },
    ]);
  });

  it("preserves document_type.is_active for historical checklist rows", () => {
    const result = normalizeChecklistItems([
      {
        document_type: {
          id: 9,
          name: "Архивная справка",
          description: "Тип уже отключён",
          is_required: false,
          is_active: false,
        },
        is_provided: false,
        has_file: false,
      },
    ]);

    expect(result[0]).toMatchObject({
      documentTypeId: 9,
      isActive: false,
      hasFile: false,
    });
  });
});
