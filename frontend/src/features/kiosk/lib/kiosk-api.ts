import axios from "axios";
import { toISODate } from "@/lib/utils";
import { useKioskStore } from "./kiosk-store";

// Separate axios instance for kiosk -- uses X-Kiosk-Token, NOT JWT Bearer
export const kioskApi = axios.create({ baseURL: "/api" });

// Reactive interceptor: reads token from Zustand store on each request
kioskApi.interceptors.request.use((config) => {
  const token = useKioskStore.getState().deviceToken;
  if (token) {
    config.headers["X-Kiosk-Token"] = token;
  }
  return config;
});

function shouldDeactivateOnUnauthorized(url?: string): boolean {
  return !!url && !url.includes("/checkins/kiosk/activate/");
}

kioskApi.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (
      [401, 403].includes(error.response?.status) &&
      shouldDeactivateOnUnauthorized(error.config?.url)
    ) {
      await useKioskStore.getState().deactivate({ preservePending: true });
    }
    throw error;
  },
);

/** @deprecated Token is now managed reactively via kiosk-store. Kept for backward compat. */
export function setKioskToken(token: string) {
  void token;
  // no-op: kiosk-store.activate() handles token storage
}

/** @deprecated Token is now managed reactively via kiosk-store. Kept for backward compat. */
export function clearKioskToken() {
  // no-op: kiosk-store.deactivate() handles token removal
}

// ── Types ──────────────────────────────────────

export interface StudentMatch {
  id: number;
  first_name: string;
  last_name: string;
  lookup_suffix?: string;
  lookup_suffixes?: string[];
  masked_phone?: string;
  group_name: string;
  grade_name?: string;
  subscription_name?: string;
  subscription_status?: string;
  trainings_left?: number | null;
}

export interface Schedule {
  schedule_id: number;
  effective_date: string;
  start_time: string;
  end_time: string;
  group_name: string;
  trainer_name: string;
  location_name: string;
  training_type_id: number | null;
  training_type_name: string;
}

export type KioskSelfCheckinStatus =
  | "can_checkin"
  | "can_book_guest_visit"
  | "blocked";

export type KioskFinancialStatus =
  | "subscription"
  | "trial_free"
  | "drop_in_debt"
  | "blocked";

export type KioskCheckinWindowStatus = "too_early" | "open" | "closed";

export interface KioskScheduleOption {
  schedule_id: number;
  effective_date: string;
  start_time: string;
  end_time: string;
  group_name: string;
  trainer_name: string;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  self_checkin_status: KioskSelfCheckinStatus;
  reason_code: string;
  financial_status: KioskFinancialStatus;
  subscription_id: number | null;
  drop_in_price: string | null;
  existing_checkin_id: number | null;
  checkin_window_status: KioskCheckinWindowStatus;
  checkin_opens_at: string;
  checkin_closes_at: string;
}

export interface KioskOptions {
  student_id: number;
  date: string;
  options: KioskScheduleOption[];
}

export interface AlertItem {
  type: string;
  icon: string;
  message: string;
}

export interface CheckinResult {
  checkin_id: number;
  is_debt: boolean;
  subscription_id: number | null;
  alerts: AlertItem[];
  created?: boolean;
  duplicate?: boolean;
  subscription_effect?: "deducted" | "none" | string;
  debt_effect?: "created" | "none" | string;
  salary_queued?: boolean;
  parent_notification_queued?: boolean;
  grade_progress_queued?: boolean;
}

export interface BrandingData {
  primary_color: string;
  accent_color: string;
  club_name_display: string;
  logo_url: string;
}

// ── API functions ──────────────────────────────

export async function activateKiosk(
  pin: string,
): Promise<{ token: string; club_id: number; club_name: string }> {
  const res = await kioskApi.post("/checkins/kiosk/activate/", { pin });
  return res.data;
}

export async function lookupStudents(
  phoneSuffix: string,
  signal?: AbortSignal,
): Promise<StudentMatch[]> {
  if (!/^\d{4}$/.test(phoneSuffix)) {
    throw new Error("Kiosk lookup requires exactly 4 digits");
  }

  const res = await kioskApi.post(
    "/checkins/kiosk/lookup/",
    { phone_suffix: phoneSuffix },
    { signal },
  );
  return res.data;
}

export async function fetchKioskRoster(): Promise<StudentMatch[]> {
  const res = await kioskApi.get("/checkins/kiosk/roster/");
  return res.data;
}

export async function kioskCheckin(
  studentId: number,
  scheduleId: number,
  trainingTypeId: number,
  checkinDate?: string,
): Promise<CheckinResult> {
  const res = await kioskApi.post("/checkins/kiosk/", {
    student_id: studentId,
    schedule_id: scheduleId,
    training_type_id: trainingTypeId,
    ...(checkinDate ? { checkin_date: checkinDate } : {}),
  });
  return res.data;
}

export async function kioskGuestBookAndCheckin(
  studentId: number,
  scheduleId: number,
  trainingTypeId: number,
  checkinDate?: string,
): Promise<CheckinResult> {
  const res = await kioskApi.post("/checkins/kiosk/guest-book-and-checkin/", {
    student_id: studentId,
    schedule_id: scheduleId,
    training_type_id: trainingTypeId,
    ...(checkinDate ? { checkin_date: checkinDate } : {}),
  });
  return res.data;
}

export async function fetchKioskOptions(
  studentId: number,
  date?: string,
): Promise<KioskOptions> {
  const res = await kioskApi.post("/checkins/kiosk/options/", {
    student_id: studentId,
    ...(date ? { date } : {}),
  });
  return res.data;
}

export async function fetchTodaySchedules(): Promise<Schedule[]> {
  const res = await kioskApi.get("/checkins/kiosk/schedules/today/");
  return res.data.map(normalizeSchedule);
}

export async function fetchBranding(): Promise<BrandingData> {
  const res = await kioskApi.get("/checkins/kiosk/branding/");
  return res.data;
}

// ── Training auto-detect (D-07) ────────────────

function parseTimeToday(timeStr: string): Date {
  const [h, m] = timeStr.split(":").map(Number);
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d;
}

function normalizeSchedule(raw: Record<string, unknown>): Schedule {
  return {
    schedule_id: Number(raw.schedule_id),
    effective_date: String(raw.effective_date ?? ""),
    start_time: String(raw.effective_start_time ?? raw.start_time ?? ""),
    end_time: String(raw.effective_end_time ?? raw.end_time ?? ""),
    group_name: String(raw.group_name ?? ""),
    trainer_name: String(raw.trainer_name ?? ""),
    location_name: String(raw.location_name ?? ""),
    training_type_id:
      raw.training_type_id === null || raw.training_type_id === undefined
        ? null
        : Number(raw.training_type_id),
    training_type_name: String(raw.training_type_name ?? ""),
  };
}

/**
 * D-07: If exactly 1 training within +-30 min of now, return it.
 * Otherwise return null (caller shows training selection).
 */
export function autoDetectTraining(schedules: Schedule[]): Schedule | null {
  const now = new Date();
  const today = toISODate(now);
  const windowMs = 30 * 60 * 1000;

  const active = schedules.filter((s) => {
    if (s.effective_date !== today) return false;
    if (s.training_type_id === null) return false;
    const start = parseTimeToday(s.start_time);
    const end = parseTimeToday(s.end_time);
    return (
      Math.abs(now.getTime() - start.getTime()) <= windowMs ||
      (now >= start && now <= end)
    );
  });

  return active.length === 1 ? active[0] : null;
}
