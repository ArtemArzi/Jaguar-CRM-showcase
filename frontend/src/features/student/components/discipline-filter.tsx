import { useState } from "react";
import { Check, ChevronDown, SlidersHorizontal } from "lucide-react";
import { useBrandingStore } from "@/features/branding/use-branding";
import { isDarkBackground } from "@/features/branding/use-contrast-color";
import { cn } from "@/lib/utils";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

interface DisciplineFilterProps {
  disciplines: string[];
  selected: string | null;
  onSelect: (value: string | null) => void;
}

export function DisciplineFilter({
  disciplines,
  selected,
  onSelect,
}: DisciplineFilterProps) {
  const [open, setOpen] = useState(false);
  const primaryColor = useBrandingStore((s) => s.primaryColor);
  const accentColor = useBrandingStore((s) => s.accentColor);
  const activeTextClass = isDarkBackground(accentColor)
    ? "text-white"
    : "text-neutral-950";

  if (disciplines.length <= 1) return null;

  const chips: { label: string; value: string | null }[] = [
    { label: "Все", value: null },
    ...disciplines.map((d) => ({ label: d, value: d })),
  ];
  const selectedChip = chips.find((chip) => chip.value === selected) ?? chips[0];

  return (
    <>
      <div className="space-y-2">
        <div className="flex items-center justify-between px-1">
          <p className="ui-overline">
            Дисциплины
          </p>
          <p className="ui-muted-12">
            Фильтр по направлению
          </p>
        </div>

        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex min-h-[52px] w-full items-center justify-between rounded-[22px] border border-black/6 bg-white/92 px-4 py-3 text-left shadow-sm ring-1 ring-white/55 transition active:scale-[0.99]"
        >
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-black/[0.04]">
              <SlidersHorizontal className="size-4 text-muted-foreground" />
            </div>
            <div className="min-w-0">
              <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                Выбрано
              </p>
              <p className="truncate text-[15px] font-semibold text-foreground">
                {selectedChip.label}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <span className="rounded-full bg-black/[0.04] px-2.5 py-1 text-[11px] font-semibold text-muted-foreground">
              {chips.length}
            </span>
            <ChevronDown className="size-4 text-muted-foreground" />
          </div>
        </button>
      </div>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="bottom" showCloseButton={false} className="rounded-t-3xl">
          <SheetHeader>
            <SheetTitle>Выбор дисциплины</SheetTitle>
          </SheetHeader>
          <div className="space-y-2 px-4 pb-5">
            {chips.map((chip) => {
              const isActive = chip.value === selected;
              return (
                <button
                  key={chip.label}
                  type="button"
                  onClick={() => {
                    onSelect(chip.value);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex min-h-[52px] w-full items-center justify-between rounded-2xl border px-4 py-3 text-left transition",
                    isActive
                      ? "border-transparent shadow-sm"
                      : "border-black/6 bg-white text-foreground",
                  )}
                  style={
                    isActive
                      ? {
                          backgroundColor: accentColor,
                          color: isDarkBackground(accentColor) ? "white" : "#111827",
                          boxShadow: isDarkBackground(primaryColor)
                            ? "0 12px 24px rgba(15, 23, 42, 0.18)"
                            : "0 10px 20px rgba(0, 0, 0, 0.08)",
                        }
                      : undefined
                  }
                >
                  <div className="min-w-0">
                    <p className="truncate text-[15px] font-semibold">
                      {chip.label}
                    </p>
                    <p className={cn("mt-1 text-[12px]", isActive ? "opacity-80" : "text-muted-foreground")}>
                      {chip.value === null ? "Все направления" : "Показывать только это направление"}
                    </p>
                  </div>
                  {isActive ? (
                    <Check className={cn("size-4 shrink-0", activeTextClass)} />
                  ) : null}
                </button>
              );
            })}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
