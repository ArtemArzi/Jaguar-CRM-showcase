import type { ComponentProps } from "react";
import { PortalSurfaceCard } from "@/components/portal/portal-surface-card";

export function ParentSurfaceCard({
  className,
  ...props
}: ComponentProps<typeof PortalSurfaceCard>) {
  return <PortalSurfaceCard className={className} {...props} />;
}
