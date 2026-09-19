import { useState } from "react";
import { useParams, useNavigate } from "react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import { getApiError } from "@/lib/utils";
import type { StudentDetail } from "../types";

interface StudentFormData {
  first_name: string;
  last_name: string;
  phone: string;
  guardian_phone: string;
  email: string;
  date_of_birth: string;
  is_child: boolean;
  contraindications: string;
}

type CreateStudentPayload = Omit<StudentFormData, "contraindications" | "date_of_birth"> & {
  date_of_birth?: string;
};

const EMPTY_FORM: StudentFormData = {
  first_name: "",
  last_name: "",
  phone: "",
  guardian_phone: "",
  email: "",
  date_of_birth: "",
  is_child: false,
  contraindications: "",
};

function getStudentFormData(student: StudentDetail): StudentFormData {
  return {
    first_name: student.first_name,
    last_name: student.last_name,
    phone: student.phone,
    guardian_phone: student.guardian_phone ?? "",
    email: student.email,
    date_of_birth: student.date_of_birth ?? "",
    is_child: student.is_child,
    contraindications: student.contraindications ?? "",
  };
}

function getStudentFormError(error: unknown, fallback: string): string {
  const detail = getApiError(error, "");
  const data = (error as { response?: { data?: { code?: string } } })?.response?.data;
  if (
    data?.code === "duplicate_phone" ||
    detail.includes("phone") ||
    detail.includes("unique") ||
    detail.toLocaleLowerCase("ru-RU").includes("телефон")
  ) {
    return "Ученик с таким номером телефона уже существует";
  }
  return detail || fallback;
}

export default function StudentForm() {
  const { studentId } = useParams();
  const isEdit = !!studentId;

  const { data: student, isLoading: studentLoading } = useQuery<StudentDetail>({
    queryKey: ["student", studentId],
    queryFn: () =>
      apiClient.get(`/students/${studentId}/`).then((r) => r.data),
    enabled: isEdit,
    staleTime: 60_000,
  });

  if (isEdit && studentLoading) {
    return <StudentFormSkeleton />;
  }

  return (
    <StudentFormEditor
      key={student ? `edit-${student.id}` : "new"}
      studentId={studentId}
      isEdit={isEdit}
      initialForm={student ? getStudentFormData(student) : EMPTY_FORM}
    />
  );
}

function StudentFormSkeleton() {
  return (
    <div className="flex flex-col gap-4 px-4 pt-6">
      <Skeleton className="h-[24px] w-[200px] rounded" />
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton key={i} className="h-[44px] rounded-xl" />
      ))}
    </div>
  );
}

function StudentFormEditor({
  studentId,
  isEdit,
  initialForm,
}: {
  studentId?: string;
  isEdit: boolean;
  initialForm: StudentFormData;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form, setForm] = useState<StudentFormData>(initialForm);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: (data: CreateStudentPayload) =>
      apiClient.post("/students/", data),
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ["students"] });
      const newId = response.data?.id;
      if (newId) {
        navigate(`/trainer/students/${newId}`);
      } else {
        navigate("/trainer/students");
      }
    },
    onError: (error: unknown) => {
      setErrorMsg(getStudentFormError(error, "Ошибка при сохранении"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: (data: Partial<StudentFormData>) =>
      apiClient.put(`/students/${studentId}/`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["student", studentId] });
      queryClient.invalidateQueries({ queryKey: ["students"] });
      navigate(-1);
    },
    onError: (error: unknown) => {
      setErrorMsg(getStudentFormError(error, "Ошибка при сохранении"));
    },
  });

  const isPending = createMutation.isPending || updateMutation.isPending;

  function updateField<K extends keyof StudentFormData>(
    key: K,
    value: StudentFormData[K],
  ) {
    setForm((prev) => ({ ...prev, [key]: value }));
    setErrorMsg(null);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const contactPhone = form.is_child ? form.guardian_phone : form.phone;
    if (!form.first_name.trim() || !form.last_name.trim() || !contactPhone.trim()) {
      setErrorMsg("Заполните обязательные поля");
      return;
    }

    if (isEdit) {
      updateMutation.mutate({
        first_name: form.first_name.trim(),
        last_name: form.last_name.trim(),
        phone: form.is_child ? "" : form.phone.trim(),
        guardian_phone: form.is_child ? form.guardian_phone.trim() : "",
        email: form.email.trim() || undefined,
        date_of_birth: form.date_of_birth || undefined,
        is_child: form.is_child,
        contraindications: form.contraindications.trim() || undefined,
      });
    } else {
      createMutation.mutate({
        first_name: form.first_name.trim(),
        last_name: form.last_name.trim(),
        phone: form.is_child ? "" : form.phone.trim(),
        guardian_phone: form.is_child ? form.guardian_phone.trim() : "",
        email: form.email.trim(),
        date_of_birth: form.date_of_birth || undefined,
        is_child: form.is_child,
      });
    }
  }

  return (
    <div className="flex flex-col gap-4 px-4 pt-6 pb-8">
      {/* Back button */}
      <button
        type="button"
        onClick={() => navigate(-1)}
        className="flex items-center gap-1 text-[14px] text-muted-foreground active:opacity-70 self-start"
      >
        <ArrowLeft size={18} />
        <span>Назад</span>
      </button>

      <h1 className="ui-title-20">
        {isEdit ? "Редактировать ученика" : "Новый ученик"}
      </h1>

      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <div>
          <label className="ui-field-label">
            Имя *
          </label>
          <Input
            value={form.first_name}
            onChange={(e) => updateField("first_name", e.target.value)}
            placeholder="Имя"
            required
            autoFocus={!isEdit}
          />
        </div>

        <div>
          <label className="ui-field-label">
            Фамилия *
          </label>
          <Input
            value={form.last_name}
            onChange={(e) => updateField("last_name", e.target.value)}
            placeholder="Фамилия"
            required
          />
        </div>

        <div>
          <label className="ui-field-label">
            {form.is_child ? "Телефон родителя *" : "Телефон *"}
          </label>
          <Input
            value={form.is_child ? form.guardian_phone : form.phone}
            onChange={(e) =>
              updateField(form.is_child ? "guardian_phone" : "phone", e.target.value)
            }
            placeholder="+7 (999) 123-45-67"
            type="tel"
            required
          />
        </div>

        <div>
          <label className="ui-field-label">
            Email
          </label>
          <Input
            value={form.email}
            onChange={(e) => updateField("email", e.target.value)}
            placeholder="email@example.com"
            type="email"
          />
        </div>

        <div>
          <label className="ui-field-label">
            Дата рождения
          </label>
          <Input
            value={form.date_of_birth}
            onChange={(e) => updateField("date_of_birth", e.target.value)}
            type="date"
          />
        </div>

        <div className="flex items-center justify-between">
          <label className="text-[16px] text-foreground">Ребёнок</label>
          <Switch
            checked={form.is_child}
            onCheckedChange={(checked) => updateField("is_child", checked)}
          />
        </div>

        {/* Contraindications (edit only) */}
        {isEdit && (
          <div>
            <label className="ui-field-label">
              Противопоказания
            </label>
            <textarea
              value={form.contraindications}
              onChange={(e) => updateField("contraindications", e.target.value)}
              placeholder="Противопоказания, травмы, ограничения..."
              rows={3}
              className="w-full rounded-xl border border-input bg-white p-3 text-[14px] text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-[var(--branding-accent)]"
            />
          </div>
        )}

        {/* Error */}
        {errorMsg && (
          <p className="ui-error-center">{errorMsg}</p>
        )}

        {/* Submit */}
        <Button
          type="submit"
          className="ui-brand-button"
          disabled={isPending}
        >
          {isPending
            ? "Отправка..."
            : isEdit
              ? "Сохранить"
              : "Создать ученика"}
        </Button>
      </form>
    </div>
  );
}
