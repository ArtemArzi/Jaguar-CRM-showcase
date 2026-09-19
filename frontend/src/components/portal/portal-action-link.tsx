import type { ComponentProps } from "react";
import { Link } from "react-router";
import { cn } from "@/lib/utils";

type PortalActionLinkProps = ComponentProps<typeof Link>;

export function PortalActionLink({
  className,
  ...props
}: PortalActionLinkProps) {
  return (
    <Link
      className={cn(
        "inline-flex min-h-[44px] w-full items-center justify-center rounded-2xl border border-black/6 bg-black/[0.03] px-4 text-[14px] font-semibold text-foreground transition-[transform,background-color,box-shadow] duration-150 ease-out active:scale-[0.99]",
        className,
      )}
      {...props}
    />
  );
}
