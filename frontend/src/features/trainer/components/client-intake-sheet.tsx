import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";

type IntakeKind = "new_contact" | "existing_student";

interface IntakeResult {
  detail?: string;
  code?: string;
  result_kind?: string;
  target_workspace?: string;
  route?: string | null;
  identity_visibility?: string;
  allowed_action?: string | null;
  commercial_segment?: "no_crm_entitlement" | null;
  duplicate_scope?: "own" | "pool" | "other" | "club";
  can_open_existing?: boolean;
  existing_student?: {
    id: number;
    display_name: string;
  };
}

interface ClientIntakeSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNavigate?: (route: string) => void;
  onOpenPoolLeads?: () => void;
  unifiedEnabled: boolean;
}

function newIdempotencyKey(): string {
  return globalThis.crypto.randomUUID();
}

export function ClientIntakeSheet({
  open,
  onOpenChange,
  onNavigate,
  onOpenPoolLeads,
  unifiedEnabled,
}: ClientIntakeSheetProps) {
  const [intakeKind, setIntakeKind] = useState<IntakeKind>("new_contact");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [phone, setPhone] = useState("");
  const [isChild, setIsChild] = useState(false);
  const [dateOfBirth, setDateOfBirth] = useState("");
  const [source, setSource] = useState("other");
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey);
  const [result, setResult] = useState<IntakeResult | null>(null);
  const [hasSubmittedCommand, setHasSubmittedCommand] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  const intake = useMutation({
    mutationFn: (retry?: { idempotencyKey?: string; confirmDistinctChild?: boolean }) => {
      const contact = {
        first_name: firstName.trim(),
        last_name: lastName.trim(),
        phone: isChild ? "" : normalizedPhone(),
        guardian_phone: isChild ? normalizedPhone() : "",
        is_child: isChild,
      };
      if (!unifiedEnabled) return apiClient.post<IntakeResult>("/leads/", contact);
      return apiClient.post<IntakeResult>("/students/intakes/", {
        idempotency_key: retry?.idempotencyKey ?? idempotencyKey,
        intake_kind: intakeKind,
        ...contact,
        date_of_birth: dateOfBirth || null,
        source,
        confirm_distinct_child: retry?.confirmDistinctChild ?? false,
      });
    },
    onSuccess: (response) => {
      if (!unifiedEnabled) {
        reset();
        onOpenChange(false);
        queryClient.invalidateQueries({ queryKey: ["leads"] });
        queryClient.invalidateQueries({ queryKey: ["students"] });
        return;
      }
      setResult(response.data);
      queryClient.invalidateQueries({ queryKey: ["leads"] });
      queryClient.invalidateQueries({ queryKey: ["students"] });
    },
    onError: (error) => {
      const data = (error as { response?: { data?: IntakeResult } })?.response?.data;
      setResult(data ?? null);
    },
  });

  function normalizedPhone(): string {
    if (phone.startsWith("7")) return `+${phone}`;
    if (phone.startsWith("8")) return `+7${phone.slice(1)}`;
    return `+7${phone}`;
  }

  function formatPhone(value: string): string {
    const digits = value.replace(/\D/g, "");
    if (!digits) return "";
    const local = digits.startsWith("7") || digits.startsWith("8") ? digits.slice(1) : digits;
    let formatted = "+7";
    if (local.length > 0) formatted += ` (${local.slice(0, 3)}`;
    if (local.length >= 3) formatted += ")";
    if (local.length > 3) formatted += ` ${local.slice(3, 6)}`;
    if (local.length > 6) formatted += `-${local.slice(6, 8)}`;
    if (local.length > 8) formatted += `-${local.slice(8, 10)}`;
    return formatted;
  }

  function reset() {
    setIntakeKind("new_contact");
    setFirstName("");
    setLastName("");
    setPhone("");
    setIsChild(false);
    setDateOfBirth("");
    setSource("other");
    setIdempotencyKey(newIdempotencyKey());
    setResult(null);
    setHasSubmittedCommand(false);
    intake.reset();
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) reset();
    onOpenChange(nextOpen);
  }

  function selectKind(kind: IntakeKind) {
    setIntakeKind(kind);
    setResult(null);
    setIdempotencyKey(newIdempotencyKey());
    setHasSubmittedCommand(false);
  }

  function changeCommandInput(update: () => void) {
    update();
    setResult(null);
    if (unifiedEnabled && hasSubmittedCommand) {
      setIdempotencyKey(newIdempotencyKey());
      setHasSubmittedCommand(false);
    }
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!firstName.trim() || !phone) return;
    if (unifiedEnabled) setHasSubmittedCommand(true);
    intake.mutate({});
  }

  function confirmDistinctChild() {
    const nextKey = newIdempotencyKey();
    setIdempotencyKey(nextKey);
    setResult(null);
    setHasSubmittedCommand(true);
    intake.mutate({ idempotencyKey: nextKey, confirmDistinctChild: true });
  }

  function openResult() {
    if (!result?.route) return;
    const route = result.route;
    reset();
    onOpenChange(false);
    onNavigate?.(route);
  }

  function openLegacyExisting() {
    const studentId = result?.existing_student?.id;
    if (!studentId) return;
    reset();
    onOpenChange(false);
    onNavigate?.(`/trainer/students/${studentId}`);
  }

  function openLegacyPool() {
    reset();
    onOpenChange(false);
    if (onOpenPoolLeads) onOpenPoolLeads();
    else onNavigate?.("/trainer/leads");
  }

  function scrollFocusedFieldIntoView(event: React.FocusEvent<HTMLInputElement>) {
    const target = event.currentTarget;
    window.setTimeout(() => {
      if (target.isConnected && typeof target.scrollIntoView === "function") {
        target.scrollIntoView({ block: "nearest" });
      }
    }, 80);
  }

  const created = result?.result_kind?.startsWith("created_");
  const resultTitle = intakeKind === "existing_student" ? "Ученик добавлен" : "Заявка создана";

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        ref={dialogRef}
        initialFocus={dialogRef}
        side="bottom"
        showCloseButton={false}
        className="max-h-[92dvh] overflow-hidden rounded-t-2xl pb-[env(safe-area-inset-bottom)]"
      >
        <SheetHeader>
          <SheetTitle>{unifiedEnabled ? "Добавить ученика" : "Новая заявка"}</SheetTitle>
          <SheetDescription>
            {unifiedEnabled ? "Один вход для нового контакта и действующего ученика" : "Добавьте контакт, чтобы он попал в работу"}
          </SheetDescription>
        </SheetHeader>
        <form
          onSubmit={handleSubmit}
          className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto overscroll-contain px-4 pb-4 pt-0"
        >
          {unifiedEnabled ? <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Тип добавления">
            {([
              ["new_contact", "Новый клиент"],
              ["existing_student", "Уже занимается"],
            ] as const).map(([kind, label]) => (
              <Button
                key={kind}
                type="button"
                role="radio"
                aria-checked={intakeKind === kind}
                variant={intakeKind === kind ? "default" : "outline"}
                onClick={() => selectKind(kind)}
                className="min-h-11"
              >
                {label}
              </Button>
            ))}
          </div> : null}
          {created ? (
            <div className="flex flex-col gap-3 rounded-xl border border-emerald-200 bg-emerald-50 p-4" role="status">
              <p className="font-medium text-emerald-950">{resultTitle}</p>
              <p className="text-sm text-emerald-900">
                {result?.commercial_segment === "no_crm_entitlement"
                  ? "Без абонемента в CRM"
                  : "Контакт добавлен в работу"}
              </p>
              {result?.route ? (
                <Button type="button" onClick={openResult} className="min-h-11">
                  {intakeKind === "existing_student" ? "Открыть ученика" : "Открыть заявку"}
                </Button>
              ) : null}
            </div>
          ) : (
            <>
              <div>
                <label htmlFor="client-intake-first-name" className="ui-field-label">
                  {isChild ? "Имя ребёнка *" : "Имя клиента *"}
                </label>
                <Input
                  id="client-intake-first-name"
                  value={firstName}
                  onChange={(event) => changeCommandInput(() => setFirstName(event.target.value))}
                  required
                  onFocus={scrollFocusedFieldIntoView}
                />
              </div>
              <div>
                <label htmlFor="client-intake-last-name" className="ui-field-label">Фамилия</label>
                <Input
                  id="client-intake-last-name"
                  value={lastName}
                  onChange={(event) => changeCommandInput(() => setLastName(event.target.value))}
                  onFocus={scrollFocusedFieldIntoView}
                />
              </div>
              <div>
                <label htmlFor="client-intake-phone" className="ui-field-label">
                  {isChild ? "Телефон родителя *" : "Телефон клиента *"}
                </label>
                <Input
                  id="client-intake-phone"
                  value={formatPhone(phone)}
                  onChange={(event) => {
                    changeCommandInput(() => setPhone(event.target.value.replace(/\D/g, "").slice(0, 11)));
                  }}
                  type="tel"
                  required
                  onFocus={scrollFocusedFieldIntoView}
                />
                {isChild ? <p className="mt-1 text-[13px] text-muted-foreground">Один номер родителя можно использовать для нескольких детей.</p> : null}
              </div>
              <div className="flex items-center justify-between">
                <label className="text-[16px] text-foreground">Ребёнок</label>
                <Switch checked={isChild} onCheckedChange={(checked) => changeCommandInput(() => setIsChild(checked))} />
              </div>
              {unifiedEnabled ? (
                <>
                  <div>
                    <label htmlFor="client-intake-date-of-birth" className="ui-field-label">
                      Дата рождения
                    </label>
                    <Input
                      id="client-intake-date-of-birth"
                      type="date"
                      value={dateOfBirth}
                      onChange={(event) => changeCommandInput(() => setDateOfBirth(event.target.value))}
                      onFocus={scrollFocusedFieldIntoView}
                    />
                  </div>
                  <div>
                    <label htmlFor="client-intake-source" className="ui-field-label">Источник</label>
                    <select
                      id="client-intake-source"
                      value={source}
                      onChange={(event) => changeCommandInput(() => setSource(event.target.value))}
                      className="min-h-11 w-full rounded-md border border-input bg-background px-3 text-sm"
                    >
                      <option value="other">Другое</option>
                      <option value="recommendation">Рекомендация</option>
                      <option value="instagram">Instagram</option>
                      <option value="vk">ВКонтакте</option>
                      <option value="signboard">Вывеска</option>
                      <option value="website">Сайт</option>
                    </select>
                  </div>
                </>
              ) : null}
              {result && !unifiedEnabled && result.code === "duplicate_phone" ? (
                <div className="flex flex-col gap-3 rounded-xl border border-amber-200 bg-amber-50 p-3" role="status">
                  <p className="text-sm font-medium text-amber-950">
                    {result.duplicate_scope === "pool"
                      ? "Такая заявка уже есть в свободных"
                      : result.duplicate_scope === "other"
                        ? "Заявка уже в работе у другого тренера"
                        : "Этот телефон уже есть в CRM"}
                  </p>
                  {result.existing_student?.display_name ? (
                    <p className="text-[13px] text-amber-900">{result.existing_student.display_name}</p>
                  ) : null}
                  {result.can_open_existing && result.existing_student ? (
                    <Button type="button" onClick={openLegacyExisting} className="min-h-10">Открыть карточку</Button>
                  ) : result.duplicate_scope === "pool" ? (
                    <Button type="button" onClick={openLegacyPool} className="min-h-10">Открыть свободные заявки</Button>
                  ) : (
                    <Button type="button" variant="outline" onClick={() => setResult(null)} className="min-h-10">Понятно</Button>
                  )}
                </div>
              ) : result ? (
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-3" role="status">
                  <p className="text-sm font-medium text-amber-950">
                    {result.detail ?? "Такой контакт уже есть в CRM"}
                  </p>
                  {result.route ? <Button type="button" onClick={openResult} className="mt-3 min-h-11">Открыть карточку</Button> : null}
                  {result.allowed_action === "confirm_distinct_child" ? (
                    <Button type="button" onClick={confirmDistinctChild} className="mt-3 min-h-11">
                      Это другой ребёнок
                    </Button>
                  ) : null}
                </div>
              ) : null}
              <div className="sticky bottom-0 -mx-4 bg-popover px-4 pb-1 pt-2">
                <Button type="submit" className="ui-brand-button" disabled={intake.isPending || !firstName.trim() || !phone}>
                  {intake.isPending ? "Создание..." : intakeKind === "existing_student" ? "Добавить ученика" : "Создать заявку"}
                </Button>
                {intake.isError && !result ? <p className="mt-2 text-center text-sm text-destructive">Не удалось добавить. Попробуйте снова.</p> : null}
              </div>
            </>
          )}
        </form>
      </SheetContent>
    </Sheet>
  );
}
