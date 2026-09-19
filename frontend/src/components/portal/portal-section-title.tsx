import { cn } from "@/lib/utils";

interface PortalSectionTitleProps {
  eyebrow?: string;
  title: string;
  className?: string;
}

export function PortalSectionTitle({
  eyebrow,
  title,
  className,
}: PortalSectionTitleProps) {
  return (
    <div className={cn("space-y-1 px-1", className)}>
      {eyebrow ? (
        <p className="ui-overline">
          {eyebrow}
        </p>
      ) : null}
      <h2 className="portal-balanced-title text-[17px] font-semibold leading-tight">
        {title}
      </h2>
    </div>
  );
}
