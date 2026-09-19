import { PortalSectionTitle } from "@/components/portal/portal-section-title";

interface StudentSectionTitleProps {
  eyebrow?: string;
  title: string;
  className?: string;
}

export function StudentSectionTitle({
  eyebrow,
  title,
  className,
}: StudentSectionTitleProps) {
  return (
    <PortalSectionTitle eyebrow={eyebrow} title={title} className={className} />
  );
}
