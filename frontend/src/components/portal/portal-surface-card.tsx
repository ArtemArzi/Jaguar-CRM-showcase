import type { ComponentProps } from "react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type PortalSurfaceCardProps = ComponentProps<typeof Card>;

export function PortalSurfaceCard({
  className,
  ...props
}: PortalSurfaceCardProps) {
  return (
    <Card
      className={cn(
        "border-0 bg-white/90 shadow-sm ring-1 ring-black/6 backdrop-blur-sm",
        className,
      )}
      {...props}
    />
  );
}
