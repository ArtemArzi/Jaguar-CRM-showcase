interface GradeHeroInput {
  systemName?: string;
  currentGradeName?: string;
  nextGradeName?: string | null;
  currentCheckins?: number;
  requiredCheckins?: number | null;
  progressPercent?: number;
}

export interface GradeHeroModel {
  eyebrow: string;
  title: string;
  status: string;
  meta: string;
  ringValue: string;
  ringLabel: string;
  primaryMetricLabel: string;
  primaryMetricValue: string;
  secondaryMetricLabel: string;
  secondaryMetricValue: string;
  progressPercent: number;
  remainingTrainings: number | null;
  isReady: boolean;
  isEmpty: boolean;
}

export function formatTrainingCount(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;

  if (mod10 === 1 && mod100 !== 11) {
    return `${count} тренировка`;
  }

  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return `${count} тренировки`;
  }

  return `${count} тренировок`;
}

export function buildGradeHeroModel(input?: GradeHeroInput): GradeHeroModel {
  const currentGradeName = input?.currentGradeName?.trim() || "Без грейда";
  const currentCheckins = input?.currentCheckins ?? 0;
  const nextGradeName = input?.nextGradeName ?? null;
  const requiredCheckins = input?.requiredCheckins ?? null;
  const progressPercent = Math.max(0, Math.min(input?.progressPercent ?? 0, 100));
  const eyebrow = input?.systemName?.trim() || "Грейд";

  if (!nextGradeName || requiredCheckins === null) {
    if (currentGradeName === "Без грейда") {
      return {
        eyebrow,
        title: "Пока без грейда",
        status: "Посети первые тренировки, чтобы открыть первый уровень.",
        meta: "Когда тренер назначит стартовый грейд, прогресс появится здесь.",
        ringValue: String(currentCheckins),
        ringLabel: "посещений",
        primaryMetricLabel: "Сейчас",
        primaryMetricValue: "Старт",
        secondaryMetricLabel: "Следующая цель",
        secondaryMetricValue: "Первый грейд",
        progressPercent: 8,
        remainingTrainings: null,
        isReady: false,
        isEmpty: true,
      };
    }

    return {
      eyebrow,
      title: currentGradeName,
      status: "Текущий уровень уже открыт. Следующий грейд ещё не назначен.",
      meta:
        currentCheckins > 0
          ? `После последнего повышения уже пройдено ${formatTrainingCount(currentCheckins)}.`
          : "Тренер может назначить следующий шаг, когда появится новая цель.",
      ringValue: String(currentCheckins),
      ringLabel: "с последнего грейда",
      primaryMetricLabel: "Текущий статус",
      primaryMetricValue: "Активен",
      secondaryMetricLabel: "Пройдено",
      secondaryMetricValue: formatTrainingCount(currentCheckins),
      progressPercent: currentCheckins > 0 ? 100 : 24,
      remainingTrainings: null,
      isReady: false,
      isEmpty: false,
    };
  }

  const remainingTrainings = Math.max(requiredCheckins - currentCheckins, 0);
  const isReady = remainingTrainings <= 0;

  return {
    eyebrow,
    title: currentGradeName,
    status: isReady
      ? `Можно готовиться к повышению до ${nextGradeName}.`
      : `До ${nextGradeName} осталось ${formatTrainingCount(remainingTrainings)}.`,
    meta: `${currentCheckins} из ${requiredCheckins} тренировок уже закрыты по пути к следующему уровню.`,
    ringValue: isReady ? "100%" : `${Math.round(progressPercent)}%`,
    ringLabel: isReady ? "готов" : "до цели",
    primaryMetricLabel: "Следующий грейд",
    primaryMetricValue: nextGradeName,
    secondaryMetricLabel: isReady ? "Статус" : "Осталось",
    secondaryMetricValue: isReady ? "Можно повышать" : formatTrainingCount(remainingTrainings),
    progressPercent: isReady ? 100 : progressPercent,
    remainingTrainings,
    isReady,
    isEmpty: false,
  };
}
