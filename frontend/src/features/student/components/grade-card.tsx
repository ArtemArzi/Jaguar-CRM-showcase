import { Trophy, Sparkles, Target } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { buildGradeHeroModel } from "../lib/student-home-presenters";

interface GradeCardProps {
  systemName: string;
  currentGradeName: string;
  nextGradeName: string | null;
  currentCheckins: number;
  requiredCheckins: number | null;
  progressPercent: number;
  variant?: "default" | "hero" | "compact";
}

export function GradeCard({
  systemName,
  currentGradeName,
  nextGradeName,
  currentCheckins,
  requiredCheckins,
  progressPercent,
  variant = "default",
}: GradeCardProps) {
  const model = buildGradeHeroModel({
    systemName,
    currentGradeName,
    nextGradeName,
    currentCheckins,
    requiredCheckins,
    progressPercent,
  });

  const ringPercent = Math.max(model.progressPercent, model.isEmpty ? 8 : 0);

  if (variant === "hero") {
    return (
      <Card
        className="relative overflow-hidden border-0 bg-[var(--branding-primary)] text-white shadow-[0_18px_50px_rgba(0,0,0,0.18)] ring-0"
      >
        <div
          className="pointer-events-none absolute inset-0 opacity-90"
          style={{
            background:
              "radial-gradient(circle at top right, var(--branding-accent) 0%, transparent 38%), radial-gradient(circle at left bottom, rgba(255,255,255,0.16) 0%, transparent 32%)",
          }}
        />
        <CardHeader className="relative pb-0">
          <div className="ui-row-between">
            <div className="space-y-2">
              <Badge className="bg-white/14 text-white shadow-none backdrop-blur-sm">
                {model.eyebrow}
              </Badge>
              <div className="space-y-1">
                <p className="text-xs uppercase tracking-[0.22em] text-white/65">
                  Текущий уровень
                </p>
                <CardTitle className="text-[28px] leading-[1.05] font-semibold text-white">
                  {model.title}
                </CardTitle>
              </div>
            </div>
            <div
              className="relative flex h-24 w-24 shrink-0 items-center justify-center rounded-full p-[10px]"
              style={{
                background: `conic-gradient(var(--branding-accent) ${ringPercent}%, rgba(255,255,255,0.18) ${ringPercent}% 100%)`,
              }}
            >
              <div className="flex h-full w-full flex-col items-center justify-center rounded-full bg-black/25 text-center backdrop-blur-sm">
                <span className="text-lg font-semibold leading-none">{model.ringValue}</span>
                <span className="mt-1 text-[10px] uppercase tracking-[0.18em] text-white/70">
                  {model.ringLabel}
                </span>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="relative space-y-4 pt-4">
          <div className="space-y-2">
            <p className="max-w-[26rem] text-[15px] font-medium leading-6 text-white">
              {model.status}
            </p>
            <p className="max-w-[26rem] text-[13px] leading-5 text-white/72">
              {model.meta}
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-2xl border border-white/12 bg-white/10 p-3 backdrop-blur-sm">
              <div className="mb-2 flex items-center gap-2 text-white/72">
                <Sparkles size={14} />
                <span className="text-[11px] uppercase tracking-[0.16em]">
                  {model.primaryMetricLabel}
                </span>
              </div>
              <p className="text-[16px] font-semibold leading-tight">
                {model.primaryMetricValue}
              </p>
            </div>
            <div className="rounded-2xl border border-white/12 bg-white/10 p-3 backdrop-blur-sm">
              <div className="mb-2 flex items-center gap-2 text-white/72">
                <Target size={14} />
                <span className="text-[11px] uppercase tracking-[0.16em]">
                  {model.secondaryMetricLabel}
                </span>
              </div>
              <p className="text-[16px] font-semibold leading-tight">
                {model.secondaryMetricValue}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (variant === "compact") {
    return (
      <Card className="border-0 bg-white/80 shadow-sm ring-1 ring-black/6 backdrop-blur-sm">
        <CardContent className="flex items-start gap-3 py-4">
          <div
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-white"
            style={{ backgroundColor: "var(--branding-primary)" }}
          >
            <Trophy size={18} />
          </div>
          <div className="min-w-0 flex-1 space-y-2">
            <div className="space-y-1">
              <p className="text-[12px] uppercase tracking-[0.16em] text-muted-foreground">
                {model.eyebrow}
              </p>
              <p className="text-[18px] font-semibold leading-tight">
                {model.title}
              </p>
              <p className="ui-caption-muted">
                {model.status}
              </p>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-black/8" role="progressbar">
              <div
                className="h-full rounded-full transition-all"
                style={{
                  width: `${ringPercent}%`,
                  backgroundColor: "var(--branding-accent)",
                }}
              />
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-0 bg-white/90 shadow-sm ring-1 ring-black/6 backdrop-blur-sm">
      <CardHeader className="pb-2">
        <div className="ui-row-between-center">
          <CardTitle className="text-[16px] font-semibold">
            {model.title}
          </CardTitle>
          {systemName ? (
            <Badge variant="outline" className="border-black/10 bg-black/3 text-foreground/70">
              {model.eyebrow}
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-[14px] leading-5 text-muted-foreground">{model.status}</p>
        <div
          className={cn("h-2 overflow-hidden rounded-full bg-black/8")}
          role="progressbar"
          aria-valuenow={currentCheckins}
          aria-valuemin={0}
          aria-valuemax={requiredCheckins ?? Math.max(currentCheckins, 1)}
        >
          <div
            className="h-full rounded-full transition-all"
            style={{
              width: `${ringPercent}%`,
              backgroundColor: "var(--branding-accent)",
            }}
          />
        </div>
        <div className="flex items-center justify-between gap-3 text-[12px] text-muted-foreground">
          <span>{model.primaryMetricValue}</span>
          <span>{model.secondaryMetricValue}</span>
        </div>
      </CardContent>
    </Card>
  );
}
