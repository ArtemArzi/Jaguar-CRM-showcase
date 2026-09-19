import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import { getApiError } from "@/lib/utils";
import type { ScheduleOut } from "../types";

interface LocationItem {
  id: number;
  name: string;
}

interface TrainingTypeItem {
  id: number;
  name: string;
  slug: string;
  is_active: boolean;
}

interface ScheduleFormSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  trainerId: number;
  editSchedule?: ScheduleOut | null;
  defaultDate?: string;
}

interface ScheduleFormState {
  date: string;
  startTime: string;
  endTime: string;
  groupName: string;
  locationId: number | "";
  trainingTypeId: number | "";
}

function getInitialFormState({
  isEdit,
  editSchedule,
  defaultDate,
}: {
  isEdit: boolean;
  editSchedule?: ScheduleOut | null;
  defaultDate?: string;
}): ScheduleFormState {
  if (isEdit && editSchedule) {
    return {
      date: editSchedule.one_time_date ?? "",
      startTime: editSchedule.start_time.slice(0, 5),
      endTime: editSchedule.end_time.slice(0, 5),
      groupName: editSchedule.group_name,
      locationId: editSchedule.location_id,
      trainingTypeId: editSchedule.training_type_id ?? "",
    };
  }

  return {
    date: defaultDate ?? "",
    startTime: "",
    endTime: "",
    groupName: "",
    locationId: "",
    trainingTypeId: "",
  };
}

export function ScheduleFormSheet({
  open,
  onOpenChange,
  trainerId,
  editSchedule,
  defaultDate,
}: ScheduleFormSheetProps) {
  const queryClient = useQueryClient();
  const isEdit = !!editSchedule;
  const initialForm = getInitialFormState({ isEdit, editSchedule, defaultDate });

  const [date, setDate] = useState(initialForm.date);
  const [startTime, setStartTime] = useState(initialForm.startTime);
  const [endTime, setEndTime] = useState(initialForm.endTime);
  const [groupName, setGroupName] = useState(initialForm.groupName);
  const [locationId, setLocationId] = useState<number | "">(initialForm.locationId);
  const [trainingTypeId, setTrainingTypeId] = useState<number | "">(
    initialForm.trainingTypeId,
  );
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const { data: locations } = useQuery<LocationItem[]>({
    queryKey: ["locations"],
    queryFn: () => apiClient.get("/clubs/locations/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: open,
  });

  const { data: trainingTypes } = useQuery<TrainingTypeItem[]>({
    queryKey: ["training-types"],
    queryFn: () => apiClient.get("/billing/training-types/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: open,
  });

  const createMutation = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      apiClient.post("/schedules/", payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Ошибка при создании"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      apiClient.put(`/schedules/${editSchedule!.id}/`, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Ошибка при сохранении"));
    },
  });

  const isPending = createMutation.isPending || updateMutation.isPending;

  function isFormValid(): boolean {
    if (
      !date ||
      !startTime ||
      !endTime ||
      !groupName.trim() ||
      !locationId ||
      !trainingTypeId
    ) {
      return false;
    }
    return endTime > startTime;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrorMsg(null);

    if (!isFormValid()) {
      setErrorMsg("Заполните все поля. Время окончания должно быть позже начала.");
      return;
    }

    const payload = {
      start_time: startTime,
      end_time: endTime,
      group_name: groupName.trim(),
      location_id: locationId,
      training_type_id: trainingTypeId,
      one_time_date: date,
    };

    if (isEdit) {
      updateMutation.mutate({
        ...payload,
      });
    } else {
      createMutation.mutate({
        ...payload,
        day_of_week: 0,
        trainer_id: trainerId,
      });
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="bottom">
        <SheetHeader>
          <SheetTitle>
            {isEdit ? "Редактировать тренировку" : "Новая тренировка"}
          </SheetTitle>
          <SheetDescription>
            {isEdit ? "Измените данные тренировки" : "Создайте разовую тренировку"}
          </SheetDescription>
        </SheetHeader>
        <form onSubmit={handleSubmit} className="ui-sheet-body">
          <div>
            <label
              htmlFor="schedule-date"
              className="ui-field-label"
            >
              Дата *
            </label>
            <Input
              id="schedule-date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="schedule-start-time"
                className="ui-field-label"
              >
                Начало *
              </label>
              <Input
                id="schedule-start-time"
                type="time"
                value={startTime}
                onChange={(e) => setStartTime(e.target.value)}
                required
              />
            </div>
            <div>
              <label
                htmlFor="schedule-end-time"
                className="ui-field-label"
              >
                Конец *
              </label>
              <Input
                id="schedule-end-time"
                type="time"
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
                required
              />
            </div>
          </div>
          <div>
            <label
              htmlFor="schedule-group-name"
              className="ui-field-label"
            >
              Группа *
            </label>
            <Input
              id="schedule-group-name"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              placeholder="Название группы"
              required
            />
          </div>
          <div>
            <label
              htmlFor="schedule-training-type"
              className="ui-field-label"
            >
              Тип тренировки *
            </label>
            <select
              id="schedule-training-type"
              value={trainingTypeId}
              onChange={(e) =>
                setTrainingTypeId(e.target.value ? Number(e.target.value) : "")
              }
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm"
              required
            >
              <option value="">Выберите тип</option>
              {trainingTypes?.map((type) => (
                <option key={type.id} value={type.id}>
                  {type.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor="schedule-location"
              className="ui-field-label"
            >
              Локация *
            </label>
            <select
              id="schedule-location"
              value={locationId}
              onChange={(e) =>
                setLocationId(e.target.value ? Number(e.target.value) : "")
              }
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm"
              required
            >
              <option value="">Выберите локацию</option>
              {locations?.map((loc) => (
                <option key={loc.id} value={loc.id}>
                  {loc.name}
                </option>
              ))}
            </select>
          </div>

          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}

          <Button
            type="submit"
            className="ui-brand-button"
            disabled={isPending || !isFormValid()}
          >
            {isPending
              ? "Сохранение..."
              : isEdit
                ? "Сохранить"
                : "Создать тренировку"}
          </Button>
        </form>
      </SheetContent>
    </Sheet>
  );
}
