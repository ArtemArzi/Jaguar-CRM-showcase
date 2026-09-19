import { PortalPageIntro } from "@/components/portal/portal-page-intro";

interface StudentPageIntroProps {
  eyebrow?: string;
  title: string;
  description?: string;
  className?: string;
}

export function StudentPageIntro({
  eyebrow,
  title,
  description,
  className,
}: StudentPageIntroProps) {
  return (
    <PortalPageIntro
      eyebrow={eyebrow}
      title={title}
      description={description}
      className={className}
    />
  );
}
