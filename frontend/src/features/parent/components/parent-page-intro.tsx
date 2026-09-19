import type { ReactNode } from "react";
import { PortalPageIntro } from "@/components/portal/portal-page-intro";

interface ParentPageIntroProps {
  eyebrow?: string;
  title: string;
  description?: string;
  aside?: ReactNode;
  className?: string;
}

export function ParentPageIntro({
  eyebrow,
  title,
  description,
  aside,
  className,
}: ParentPageIntroProps) {
  return (
    <PortalPageIntro
      eyebrow={eyebrow}
      title={title}
      description={description}
      aside={aside}
      className={className}
    />
  );
}
