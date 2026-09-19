import { useAuthStore, type UserRole } from "@/features/auth/auth-store";
import type { BankPaymentOrderLink } from "@/components/portal/payment-link-state";
import { decodeJwtPayload } from "@/lib/jwt";

export const PAYMENT_RESUME_STORAGE_KEY = "jaguar-payment-resume";
const CONTEXT_TTL_MS = 30 * 60_000;

interface StoredPaymentResumeContext {
  readonly orderId: number;
  readonly childId?: number;
  readonly role: UserRole;
  readonly clubId: number;
  readonly actorSubject: string;
  readonly expiresAt: number;
}

export interface PaymentResumeContext {
  readonly orderId: number;
  readonly role: UserRole;
  readonly childId?: number;
  readonly endpoint: string;
  readonly refreshEndpoint: string;
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function currentActorSubject(): string | null {
  const token = useAuthStore.getState().accessToken;
  if (!token) return null;
  const payload = decodeJwtPayload(token);
  const subject = payload?.sub ?? payload?.user_id;
  if (typeof subject === "string" && subject.length > 0 && subject.length <= 128) return subject;
  if (isPositiveSafeInteger(subject)) return String(subject);
  return null;
}

function getExactOrderEndpoint(context: StoredPaymentResumeContext): string | null {
  if (context.role === "student") {
    return `/students/me/bank-payment-orders/${context.orderId}/`;
  }
  if (context.role === "parent" && isPositiveSafeInteger(context.childId)) {
    return `/parents/children/${context.childId}/bank-payment-orders/${context.orderId}/`;
  }
  return null;
}

export function clearPaymentResumeContext() {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.removeItem(PAYMENT_RESUME_STORAGE_KEY);
  } catch {
    // Storage failure must not prevent logout or payment status rendering.
  }
}

export function rememberPaymentResumeContext(order: BankPaymentOrderLink) {
  if (typeof localStorage === "undefined") return;
  const { role, clubId } = useAuthStore.getState();
  const actorSubject = currentActorSubject();
  if (!role || !actorSubject || !isPositiveSafeInteger(clubId) || !isPositiveSafeInteger(order.id)) return;
  const childId = role === "parent" ? order.student_id : undefined;
  if (role !== "student" && !(role === "parent" && isPositiveSafeInteger(childId))) return;
  const context: StoredPaymentResumeContext = {
    orderId: order.id,
    ...(isPositiveSafeInteger(childId) ? { childId } : {}),
    role,
    clubId,
    actorSubject,
    expiresAt: Date.now() + CONTEXT_TTL_MS,
  };
  try {
    localStorage.setItem(PAYMENT_RESUME_STORAGE_KEY, JSON.stringify(context));
  } catch {
    // Browser storage is only a best-effort closed-tab resume aid.
  }
}

export function getPaymentResumeContext(): PaymentResumeContext | null {
  if (typeof localStorage === "undefined") return null;
  try {
    const raw = localStorage.getItem(PAYMENT_RESUME_STORAGE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Partial<StoredPaymentResumeContext>;
    const current = useAuthStore.getState();
    const actorSubject = currentActorSubject();
    if (!actorSubject) return null;
    if (
      !isPositiveSafeInteger(stored.orderId) ||
      !isPositiveSafeInteger(stored.clubId) ||
      !isPositiveSafeInteger(stored.expiresAt) ||
      stored.expiresAt <= Date.now() ||
      stored.role !== current.role ||
      stored.clubId !== current.clubId ||
      stored.actorSubject !== actorSubject
    ) {
      clearPaymentResumeContext();
      return null;
    }
    const endpoint = getExactOrderEndpoint(stored as StoredPaymentResumeContext);
    if (!endpoint) {
      clearPaymentResumeContext();
      return null;
    }
    return {
      orderId: stored.orderId,
      role: stored.role as UserRole,
      ...(isPositiveSafeInteger(stored.childId) ? { childId: stored.childId } : {}),
      endpoint,
      refreshEndpoint: `${endpoint}refresh/`,
    };
  } catch {
    clearPaymentResumeContext();
    return null;
  }
}
