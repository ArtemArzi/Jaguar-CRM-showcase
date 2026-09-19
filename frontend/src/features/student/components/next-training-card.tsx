import { Card, CardContent } from "@/components/ui/card";
import { CalendarDays, Clock3, MapPin, UserRound } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { PortalActionLink } from "@/components/portal/portal-action-link";
import { formatClockTime } from "../lib/student-schedule-utils";

interface NextTrainingCardProps {
  groupName: string;
  eyebrow?: string;
  dayLabel: string;
  startTime: string;
  endTime: string;
  trainerName: string;
  locationName: string;
  actionLabel?: string;
  actionTo?: string;
}

export function NextTrainingCard({
  groupName,
  eyebrow = "Следующая тренировка",
  dayLabel,
  startTime,
  endTime,
  trainerName,
  locationName,
  actionLabel,
  actionTo,
}: NextTrainingCardProps) {
  return (
    <Card className="border-0 bg-white/90 shadow-sm ring-1 ring-black/6 backdrop-blur-sm">
      <CardContent className="space-y-3 py-3.5">
        <div className="ui-row-between">
          <div className="flex items-center gap-3">
            <div
              className="flex h-11 w-11 items-center justify-center rounded-2xl"
              style={{
                background:
                  "linear-gradient(0deg, rgba(255,255,255,0.86), rgba(255,255,255,0.86)), var(--branding-accent)",
              }}
            >
              <CalendarDays size={20} style={{ color: "var(--branding-accent)" }} />
            </div>
            <div className="space-y-1">
              <p className="ui-overline">
                {eyebrow}
              </p>
              <p className="text-[17px] font-semibold leading-tight">{groupName}</p>
            </div>
          </div>
          <Badge variant="outline" className="border-black/8 bg-black/3 px-2.5 py-1 text-foreground/70">
            {dayLabel}
          </Badge>
        </div>

        <div className="rounded-2xl border border-black/6 bg-black/[0.02] px-3 py-2.5">
          <div className="flex items-center gap-2 text-[13px] font-medium">
            <Clock3 size={14} style={{ color: "var(--branding-accent)" }} />
            <span>
              {formatClockTime(startTime)}&ndash;{formatClockTime(endTime)}
            </span>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-1.5 text-[13px] text-muted-foreground">
          <div className="ui-row-2">
            <UserRound size={14} />
            <span>{trainerName}</span>
          </div>
          <div className="ui-row-2">
            <MapPin size={14} />
            <span>{locationName}</span>
          </div>
        </div>

        {actionLabel && actionTo ? (
          <PortalActionLink to={actionTo}>
            {actionLabel}
          </PortalActionLink>
        ) : null}
      </CardContent>
    </Card>
  );
}
