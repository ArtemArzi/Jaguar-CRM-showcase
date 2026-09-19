import { describe, expect, it } from "vitest";
import { buildGradeHeroModel, formatTrainingCount } from "./student-home-presenters";

describe("student-home-presenters", () => {
  it("formats russian training count correctly", () => {
    expect(formatTrainingCount(1)).toBe("1 тренировка");
    expect(formatTrainingCount(3)).toBe("3 тренировки");
    expect(formatTrainingCount(8)).toBe("8 тренировок");
  });

  it("builds an empty-state hero model when there is no grade yet", () => {
    const result = buildGradeHeroModel();

    expect(result.title).toBe("Пока без грейда");
    expect(result.isEmpty).toBe(true);
    expect(result.secondaryMetricValue).toBe("Первый грейд");
  });

  it("builds a progress model when a next grade exists", () => {
    const result = buildGradeHeroModel({
      systemName: "Муай-тай",
      currentGradeName: "Жёлтый пояс",
      nextGradeName: "Оранжевый пояс",
      currentCheckins: 7,
      requiredCheckins: 10,
      progressPercent: 70,
    });

    expect(result.eyebrow).toBe("Муай-тай");
    expect(result.title).toBe("Жёлтый пояс");
    expect(result.secondaryMetricValue).toBe("3 тренировки");
    expect(result.isReady).toBe(false);
  });

  it("marks the model as ready when the threshold is reached", () => {
    const result = buildGradeHeroModel({
      currentGradeName: "Синий пояс",
      nextGradeName: "Коричневый пояс",
      currentCheckins: 12,
      requiredCheckins: 10,
      progressPercent: 100,
    });

    expect(result.isReady).toBe(true);
    expect(result.secondaryMetricValue).toBe("Можно повышать");
    expect(result.ringValue).toBe("100%");
  });

  it("treats requiredCheckins = 0 as an already-ready promotion threshold", () => {
    const result = buildGradeHeroModel({
      currentGradeName: "Белый пояс",
      nextGradeName: "Жёлтый пояс",
      currentCheckins: 0,
      requiredCheckins: 0,
      progressPercent: 0,
    });

    expect(result.isReady).toBe(true);
    expect(result.progressPercent).toBe(100);
    expect(result.secondaryMetricValue).toBe("Можно повышать");
  });
});
