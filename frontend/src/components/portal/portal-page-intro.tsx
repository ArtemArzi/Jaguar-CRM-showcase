import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface PortalPageIntroProps {
  eyebrow?: string;
  title: string;
  description?: string;
  aside?: ReactNode;
  className?: string;
}

export function PortalPageIntro({
  eyebrow,
  title,
  description,
  aside,
  className,
}: PortalPageIntroProps) {
  return (
    <header
      className={cn(
        "flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between",
        className,
      )}
    >
      <div className="space-y-2">
        {eyebrow ? (
          <p className="ui-overline">
            {eyebrow}
          </p>
        ) : null}
        <h1 className="portal-balanced-title text-[28px] font-semibold leading-tight">
          {title}
        </h1>
        {description ? (
          <p className="portal-pretty-text max-w-[28rem] text-[14px] leading-6 text-muted-foreground">
            {description}
          </p>
        ) : null}
      </div>
      {aside ? <div className="shrink-0">{aside}</div> : null}
    </header>
  );
}
