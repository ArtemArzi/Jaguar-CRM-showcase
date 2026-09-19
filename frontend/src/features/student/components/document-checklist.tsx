import { useRef, useState } from "react";
import { Upload } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";

import type { StudentChecklistItem } from "../lib/student-normalizers";
import { StudentSectionTitle } from "./student-section-title";
import { StudentSurfaceCard } from "./student-surface-card";

interface DocumentChecklistProps {
  items: StudentChecklistItem[];
  studentId: number;
  onUploadComplete: () => void;
}

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
const ACCEPTED_DOCUMENT_MIME_TYPES = new Set([
  "application/pdf",
  "image/jpeg",
  "image/png",
  "image/webp",
]);
const DOCUMENT_ACCEPT = "application/pdf,image/jpeg,image/png,image/webp,.pdf,.jpg,.jpeg,.png,.webp";

export function DocumentChecklist({
  items,
  studentId,
  onUploadComplete,
}: DocumentChecklistProps) {
  const [uploadingId, setUploadingId] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pendingTypeIdRef = useRef<number | null>(null);

  if (items.length === 0) {
    return (
      <StudentSurfaceCard className="p-3.5">
        <StudentSectionTitle eyebrow="Профиль" title="Документы" />
        <div className="mt-4 rounded-2xl border border-dashed border-black/8 bg-white/70 px-4 py-5 text-center">
          <p className="text-[14px] text-neutral-500">Документы не требуются</p>
        </div>
      </StudentSurfaceCard>
    );
  }

  const clearMessages = () => {
    setErrorMessage(null);
    setSuccessMessage(null);
  };

  const handleUploadClick = (documentTypeId: number) => {
    clearMessages();
    pendingTypeIdRef.current = documentTypeId;
    fileInputRef.current?.click();
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    const typeId = pendingTypeIdRef.current;
    // Reset input so same file can be re-selected
    if (fileInputRef.current) fileInputRef.current.value = "";

    if (!file || typeId === null) return;

    // Frontend validation: size
    if (file.size > MAX_FILE_SIZE) {
      setErrorMessage("Файл слишком большой. Максимум 10 МБ");
      return;
    }

    // Frontend validation: type
    if (!ACCEPTED_DOCUMENT_MIME_TYPES.has(file.type)) {
      setErrorMessage("Формат не поддерживается. Допустимы: PDF, JPEG, PNG или WebP. HEIC/HEIF пока не поддерживаются.");
      return;
    }

    setUploadingId(typeId);
    clearMessages();

    try {
      const formData = new FormData();
      formData.append("document_type_id", String(typeId));
      formData.append("file", file);

      await apiClient.post(
        `/documents/students/${studentId}/upload/`,
        formData,
        { headers: { "Content-Type": "multipart/form-data" } },
      );

      setSuccessMessage("Документ загружен");
      onUploadComplete();
    } catch {
      setErrorMessage("Не удалось загрузить. Попробуйте ещё раз");
    } finally {
      setUploadingId(null);
      pendingTypeIdRef.current = null;
    }
  };

  return (
    <StudentSurfaceCard className="p-3.5">
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        accept={DOCUMENT_ACCEPT}
        className="hidden"
        onChange={handleFileChange}
      />

      {/* Messages */}
      <div className="space-y-2.5">
        <StudentSectionTitle eyebrow="Профиль" title="Документы" />
        <p className="ui-caption-muted">
          Загружайте PDF, JPEG, PNG или WebP только для актуальных требований.
          HEIC/HEIF пока не поддерживаются. Архивные типы остаются в списке как
          история и недоступны для новой загрузки.
        </p>

        {errorMessage && (
          <p
            className="rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-[14px] text-red-700"
            aria-live="assertive"
          >
            {errorMessage}
          </p>
        )}
        {successMessage && (
          <p
            className="rounded-2xl border border-green-200 bg-green-50 px-3 py-2 text-[14px] text-green-700"
            aria-live="assertive"
          >
            {successMessage}
          </p>
        )}

        {/* Document items */}
        <div className="space-y-2.5">
          {items.map((item) => {
            const isUploading = uploadingId === item.documentTypeId;
            const isArchived = !item.isActive;
            const canUpload = !item.hasFile && item.isActive;
            const statusLabel = item.hasFile
              ? "Загружен"
              : item.isProvided
                ? "Отмечен"
                : isArchived
                  ? "Без файла"
                  : "Не загружен";

            return (
              <div
                key={item.documentTypeId}
                className="rounded-2xl border border-black/6 bg-white/90 p-3.5 shadow-sm"
              >
                <div className="ui-row-between">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] font-semibold text-foreground">
                      {item.documentTypeName}
                    </p>
                    {item.description ? (
                      <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
                        {item.description}
                      </p>
                    ) : null}
                  </div>

                  {canUpload ? (
                    <Button
                      variant="default"
                      size="sm"
                      disabled={isUploading}
                      onClick={() => handleUploadClick(item.documentTypeId)}
                      className="shrink-0 min-h-[44px] px-3"
                      style={{ backgroundColor: "var(--branding-accent)" }}
                    >
                      <Upload className="size-4 mr-1" />
                      {isUploading ? "Загрузка..." : "Загрузить"}
                    </Button>
                  ) : null}
                </div>

                <div className="mt-2.5 flex flex-wrap gap-2">
                  <Badge
                    className={
                      item.hasFile
                        ? "border-green-200 bg-green-100 text-green-800"
                        : "bg-white text-neutral-700"
                    }
                    variant="outline"
                  >
                    {statusLabel}
                  </Badge>
                  <Badge variant="outline">
                    {item.isRequired ? "Обязательный" : "Необязательный"}
                  </Badge>
                  {isArchived ? <Badge variant="outline">Архивный тип</Badge> : null}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </StudentSurfaceCard>
  );
}
