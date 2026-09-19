export interface ScheduleOut {
  id: number;
  day_of_week: number;
  start_time: string;
  end_time: string;
  group_name: string;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id?: number;
  is_active: boolean;
  one_time_date?: string | null;
  student_count?: number;
  newcomer_count?: number;
}

export interface ScheduleOccurrenceOut {
  schedule_id: number;
  group_name: string;
  effective_date: string;
  effective_start_time: string;
  effective_end_time: string;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  one_time_date?: string | null;
  is_rescheduled?: boolean;
  is_substitute?: boolean;
  training_type_id?: number | null;
  training_type_name?: string;
  training_type_kind?: string;
}

export type AlertType =
  | "newcomer"
  | "debtor"
  | "contraindications"
  | "returned"
  | "expiring"
  | "last_training"
  | "birthday"
  | "first_after_grade"
  | "child";

export interface AlertOut {
  type: AlertType;
  icon: string;
  message: string;
}

export interface StudentWithAlerts {
  id: number;
  first_name: string;
  last_name: string;
  alerts: AlertOut[];
  enrollment_id?: number | null;
  created_from?: string | null;
  starts_on?: string | null;
  ends_on?: string | null;
  is_guest_visit?: boolean;
  enrollment_status?: string | null;
  checkin_blocked_reason?: string | null;
}

export type SessionRosterStatus = "checked_in" | "waiting" | "blocked";

export interface SessionRosterStudent extends StudentWithAlerts {
  checkin_status: SessionRosterStatus;
  checkin_id?: number | null;
  checkin_source?: string;
  checked_in_at?: string | null;
}

export interface SessionSummaryOut {
  expected_count: number;
  checked_in_count: number;
  waiting_count: number;
  blocked_count: number;
}

export interface SessionDetailOut {
  schedule_id: number;
  date: string;
  occurrence: ScheduleOccurrenceOut;
  is_closed: boolean;
  group_session_id?: number | null;
  closed_at?: string | null;
  closed_by_id?: number | null;
  close_source?: string;
  can_close: boolean;
  close_allowed_at: string;
  close_block_reason?: string;
  summary: SessionSummaryOut;
  roster: SessionRosterStudent[];
}

export interface GroupSessionOut {
  id: number;
  schedule_id: number;
  date: string;
  trainer_id: number;
  attendee_count: number;
  topic_tags: string[];
  notes: string;
  closed_at?: string | null;
  closed_by_id?: number | null;
  close_source: string;
}

export interface CheckinResultOut {
  checkin_id: number;
  student_id: number;
  is_debt: boolean;
  subscription_id: number | null;
  alerts: AlertOut[];
  created: boolean;
  duplicate: boolean;
  subscription_effect: string;
  debt_effect: string;
  salary_queued: boolean;
  parent_notification_queued: boolean;
  grade_progress_queued: boolean;
  group_analytics_queued: boolean;
  retention_auto_close_queued: boolean;
  post_trial_task_queued: boolean;
  trainings_left_push_queued: boolean;
}

export interface SubmittedStudentWithCheckin extends StudentWithAlerts {
  checkin: CheckinResultOut;
}

export interface BatchCheckinResultOut {
  checkins: CheckinResultOut[];
  group_session_id: number;
}

export interface StudentListItem {
  id: number;
  first_name: string;
  last_name: string;
  phone: string;
  guardian_phone?: string;
  email: string;
  status: string;
  is_child: boolean;
  date_of_birth: string | null;
  source: string;
  commercial_segment?:
    | "former"
    | "at_risk"
    | "pending_admission"
    | "active_entitlement"
    | "no_crm_entitlement"
    | null;
}

export interface PersonSearchResult {
  id: number | null;
  display_name: string | null;
  masked_phone: string | null;
  target_workspace: "leads_active" | "leads_archived" | "students" | null;
  route: string | null;
  identity_visibility: "full" | "masked" | "none";
  allowed_action: string | null;
  commercial_segment?: StudentListItem["commercial_segment"];
}

export interface GuestVisitCandidate {
  id: number;
  kind: "student" | "lead";
  first_name: string;
  last_name: string;
  masked_phone: string;
  status: string;
}

export interface GuestVisitOut {
  enrollment_id: number;
  student_id: number;
  display_name: string;
  schedule_id: number;
  created_from: string;
  is_guest_visit: boolean;
  starts_on?: string | null;
  ends_on?: string | null;
  created: boolean;
  already_member: boolean;
  origin: string;
  financial_preview: {
    code: string;
    message: string;
  };
}

export interface StudentNote {
  id: number;
  text: string;
  author_email: string;
  created_at: string;
}

export interface AccountAccessSummary {
  role: string;
  status: string;
  username: string;
  must_change_password: boolean;
  issued_at: string;
  reset_at: string | null;
}

export interface AccountAccessIssue extends AccountAccessSummary {
  student_id: number;
  temporary_password: string | null;
  created_user: boolean;
  created_membership: boolean;
  created_access: boolean;
}

export interface OperationalAdmission {
  payment_id: number;
  payment_status: string;
  payment_method: string;
  subscription_status: string | null;
  enrollment_status: string;
  group_label: string;
  training_group_id?: number | null;
  group_membership_id?: number | null;
  start_date: string;
  checkin_ready: boolean;
  account_access_eligible: boolean;
  covered_visit_count: number;
}

export interface OperationalAdmissionV2 {
  kind: "group" | "personal";
  payment_id: number;
  recorded_by_id: number;
  payment_status: string;
  payment_method: string;
  subscription_status: string | null;
  start_date: string;
  checkin_ready: boolean;
  account_access_eligible: boolean;
  is_qualifying: boolean;
  group_label: string | null;
  training_group_id: number | null;
  group_membership_id: number | null;
  enrollment_status: string | null;
  booking_id: number | null;
  session_id: number | null;
  booking_state: string | null;
}

export interface StudentDetail extends StudentListItem {
  contraindications: string;
  notes: StudentNote[];
  account_access: AccountAccessSummary | null;
  operational_admission: OperationalAdmission | null;
  operational_admission_v2?: OperationalAdmissionV2 | null;
  account_access_eligible: boolean;
  has_parent_user: boolean;
  can_manage_sensitive_actions?: boolean | null;
  can_manage_account_access: boolean;
  can_manage_feedback: boolean;
}

export interface StudentSubscription {
  id: number;
  tariff_name: string;
  renewal_target_tariff_id?: number | null;
  renewal_target_tariff_name?: string | null;
  renewal_target_price?: string | number | null;
  training_type_id?: number | null;
  training_type_name?: string;
  trainings_used: number;
  trainings_total: number | null;
  trainings_left: number | null;
  expires_at: string | null;
  status: string;
  paid_amount?: string | number;
  freeze_status?: string | null;
  training_type_kind?: string;
  package_owner_trainer_id?: number | null;
  package_owner_trainer_name?: string;
  scope?: string;
  location_id?: number | null;
  has_components?: boolean;
  booking_entitlements?: SubscriptionBookingEntitlement[];
  booking_date?: string | null;
}

export interface SubscriptionBookingEntitlement {
  training_type_id: number;
  training_type_name: string;
  training_type_kind: string;
  credits_left: number | null;
  weekly_limit: number | null;
  weekly_used?: number | null;
  scope: string;
  location_id: number | null;
}

export interface PersonalBooking {
  schedule_id: number;
  enrollment_id: number;
  student_id: number;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  starts_at: string;
  ends_at: string;
  created_from: string;
  status: string;
  booking_id?: number | null;
  booking_kind?: "entitlement" | "online_payment" | "drop_in";
  attendance_state?: "scheduled" | "attended" | "cancelled" | "no_show";
  financial_state?:
    | "covered"
    | "pay_at_club"
    | "debt_open"
    | "payment_pending"
    | "paid"
    | "not_due";
  price_snapshot?: string | number | null;
  tariff_id?: number | null;
  debt_id?: number | null;
  payment_id?: number | null;
  bank_payment_order_id?: number | null;
  payment_status?: string | null;
  can_manage: boolean;
  can_cancel?: boolean;
  can_mark_no_show?: boolean;
  can_reschedule: boolean;
  next_action_label?: string | null;
}

export type TrainerPersonalAvailabilityStatus =
  | "published"
  | "held"
  | "booked"
  | "blocked"
  | "cancelled";

export interface TrainerPersonalAvailabilitySlot {
  id: number;
  date: string;
  starts_at: string;
  ends_at: string;
  trainer_id: number;
  trainer_name: string;
  location_id: number;
  location_name: string;
  training_type_id: number;
  training_type_name: string;
  training_type_kind: string;
  status: TrainerPersonalAvailabilityStatus;
  block_reason: string;
  booked_enrollment_id: number | null;
  can_block: boolean;
  can_unblock: boolean;
  can_cancel: boolean;
  /** Server-resolved personal offer. These fields are absent on the legacy path. */
  offer_tariff_id?: number | null;
  offer_tariff_name?: string;
  offer_price?: string | number | null;
  offer_base_amount?: string | number | null;
  offer_discount_id?: number | null;
  offer_discount_name?: string;
  offer_discount_type?: "percent" | "fixed" | "";
  offer_discount_value?: string | number | null;
  offer_discount_amount?: string | number | null;
  offer_payable_amount?: string | number | null;
  offer_trainer_id?: number | null;
  offer_digest?: string;
  offer_error_code?: string;
}

export interface TrainerPersonalAvailabilityGeneratePayload {
  date_from: string;
  date_to: string;
  weekdays: number[];
  start_time: string;
  end_time: string;
  slot_duration_minutes: number;
  buffer_minutes: number;
  location_id: number;
  training_type_id: number;
}

export interface TrainerPersonalAvailabilitySkipped {
  date: string;
  starts_at: string;
  ends_at: string;
  reason_code: string;
}

export interface TrainerPersonalAvailabilityGenerateResult {
  created: TrainerPersonalAvailabilitySlot[];
  skipped: TrainerPersonalAvailabilitySkipped[];
}

export interface GradeProgress {
  student_grade_id: number;
  grade_system_id: number;
  grade_system_name: string | null;
  current_grade: {
    id: number;
    name: string;
    order: number;
    min_trainings: number;
  } | null;
  trainings_since_last_grade: number;
  next_grade: {
    id: number;
    name: string;
    order: number;
    min_trainings: number;
  } | null;
  trainings_to_next: number | null;
}

export interface AttendanceItem {
  id: number;
  date: string;
  group_name: string;
  trainer_name: string;
  location_name: string;
  training_type_name: string;
  start_time: string;
}

export type TaskLevel = "yellow" | "red" | "churned" | "";
export type TaskStatus = "open" | "in_progress" | "snoozed" | "closed";

export type TaskType = "retention" | "new_lead" | "post_trial" | "renewal";

export interface RetentionTask {
  id: number;
  student_id: number;
  student_name: string;
  student_phone: string;
  last_visit_date: string | null;
  days_missed: number;
  trainer_id: number;
  level: TaskLevel;
  status: TaskStatus;
  due_date: string;
  resolved_at: string | null;
  resolution: string;
  notes: string;
  created_at: string;
  last_activity_date: string | null;
  task_type: TaskType;
  attempt_count: number;
  automation_source: string | null;
  automation_step_message: string | null;
}

export interface TaskComment {
  id: number;
  author_email: string;
  text: string;
  created_at: string;
}

export interface EarningSummary {
  total_amount: string | number;
  total_sessions: number;
  by_type?: Record<string, unknown>;
}
