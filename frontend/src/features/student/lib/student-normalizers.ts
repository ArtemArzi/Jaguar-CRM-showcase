import type { AttendanceItem } from "../components/attendance-list";

interface AttendanceSummaryResponse {
  attended_count?: number;
  count?: number;
  items?: AttendanceItem[];
}

interface ChecklistDocumentTypeResponse {
  id: number;
  name: string;
  description: string;
  is_required: boolean;
  is_active: boolean;
}

interface ChecklistItemResponse {
  document_type: ChecklistDocumentTypeResponse;
  is_provided: boolean;
  has_file: boolean;
}

export interface StudentChecklistItem {
  documentTypeId: number;
  documentTypeName: string;
  description: string;
  isRequired: boolean;
  isActive: boolean;
  isProvided: boolean;
  hasFile: boolean;
}

export function readAttendanceSummary(raw: AttendanceSummaryResponse): {
  items: AttendanceItem[];
  totalCount: number;
} {
  const items = raw.items ?? [];
  const totalCount = raw.attended_count ?? raw.count ?? items.length;
  return { items, totalCount };
}

export function normalizeChecklistItems(
  raw: ChecklistItemResponse[],
): StudentChecklistItem[] {
  return raw.map((item) => ({
    documentTypeId: item.document_type.id,
    documentTypeName: item.document_type.name,
    description: item.document_type.description,
    isRequired: item.document_type.is_required,
    isActive: item.document_type.is_active,
    isProvided: item.is_provided,
    hasFile: item.has_file,
  }));
}
