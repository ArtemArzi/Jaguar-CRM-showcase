import { useState, useCallback } from "react";
import { useKioskStore } from "@/features/kiosk/lib/kiosk-store";
import type {
  StudentMatch,
  CheckinResult,
  KioskScheduleOption,
  Schedule,
} from "@/features/kiosk/lib/kiosk-api";
import { getKioskCheckinErrorMessage } from "@/features/kiosk/lib/checkin-error";

export type KioskScreen = "pin" | "numpad" | "matches" | "result" | "feedback";

export interface KioskNavState {
  screen: KioskScreen;
  matches: StudentMatch[];
  selectedStudent: StudentMatch | null;
  checkinResult: CheckinResult | null;
  checkinError: string | null;
  phoneSuffix: string;
  schedules: Schedule[];
  kioskOptions: KioskScheduleOption[] | null;
}

const initialNav: KioskNavState = {
  screen: "numpad",
  matches: [],
  selectedStudent: null,
  checkinResult: null,
  checkinError: null,
  phoneSuffix: "",
  schedules: [],
  kioskOptions: null,
};

export function useKioskNav() {
  const isActivated = useKioskStore((s) => s.isActivated);
  const [nav, setNav] = useState<KioskNavState>({
    ...initialNav,
    screen: isActivated ? "numpad" : "pin",
  });

  const goToNumpad = useCallback(() => {
    setNav((prev) => ({
      ...initialNav,
      screen: "numpad",
      schedules: prev.schedules,
    }));
  }, []);

  const goToMatches = useCallback(
    (matches: StudentMatch[], phoneSuffix: string) => {
      setNav((prev) => ({
        ...prev,
        screen: "matches",
        matches,
        phoneSuffix,
        selectedStudent: null,
        checkinResult: null,
        checkinError: null,
      }));
    },
    [],
  );

  const goToResult = useCallback(
    (student: StudentMatch, kioskOptions: KioskScheduleOption[] | null = null) => {
      setNav((prev) => ({
        ...prev,
        screen: "result",
        selectedStudent: student,
        checkinResult: null,
        checkinError: null,
        kioskOptions,
      }));
    },
    [],
  );

  const goToFeedback = useCallback(
    (result: CheckinResult, student: StudentMatch) => {
      setNav((prev) => ({
        ...prev,
        screen: "feedback",
        checkinResult: result,
        checkinError: null,
        selectedStudent: student,
      }));
    },
    [],
  );

  const goToErrorFeedback = useCallback(
    (error: unknown, student: StudentMatch) => {
      setNav((prev) => ({
        ...prev,
        screen: "feedback",
        checkinResult: null,
        checkinError: getKioskCheckinErrorMessage(error),
        selectedStudent: student,
      }));
    },
    [],
  );

  const onActivated = useCallback(() => {
    setNav((prev) => ({ ...prev, screen: "numpad" }));
  }, []);

  const setSchedules = useCallback((schedules: Schedule[]) => {
    setNav((prev) => ({ ...prev, schedules }));
  }, []);

  return {
    nav,
    goToNumpad,
    goToMatches,
    goToResult,
    goToFeedback,
    goToErrorFeedback,
    onActivated,
    setSchedules,
  };
}
