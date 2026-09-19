export type FeedbackQuestionType = "rating" | "yes_no" | "text";

export interface FeedbackQuestion {
  id: number;
  question_type: FeedbackQuestionType;
  text: string;
  order: number;
  is_required: boolean;
}

export interface FeedbackForm {
  id: number;
  name: string;
  is_active: boolean;
  questions: FeedbackQuestion[];
  created_at: string;
}

export interface FeedbackAnswerPayload {
  question_id: number;
  rating_value?: number | null;
  bool_value?: boolean | null;
  text_value?: string;
}

export interface FeedbackSubmitPayload {
  form_id: number;
  answers: FeedbackAnswerPayload[];
}

export interface FeedbackSubmitResponse {
  id: number;
  student_id: number;
  form_id: number;
  submitted_at: string;
  answers?: FeedbackAnswer[];
  already_submitted: boolean;
}

export interface FeedbackAnswer {
  question_id: number;
  question_type: FeedbackQuestionType;
  question_text?: string;
  rating_value?: number | null;
  bool_value?: boolean | null;
  text_value?: string;
}

export interface FeedbackResponse {
  id: number;
  student_id: number;
  form_id: number;
  submitted_at: string;
  answers: FeedbackAnswer[];
}
