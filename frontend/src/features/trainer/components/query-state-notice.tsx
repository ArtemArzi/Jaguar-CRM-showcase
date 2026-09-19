import { Button } from "@/components/ui/button";

interface QueryStateNoticeProps {
  readonly title: string;
  readonly message: string;
  readonly onRetry: () => void;
  readonly retrying?: boolean;
  readonly retryLabel?: string;
  readonly className?: string;
}

export function QueryStateNotice({
  title,
  message,
  onRetry,
  retrying = false,
  retryLabel = "Повторить",
  className = "",
}: QueryStateNoticeProps) {
  return (
    <div
      role="alert"
      className={`flex flex-col gap-2 rounded-xl bg-amber-50 p-4 ring-1 ring-amber-200 ${className}`}
    >
      <p className="ui-title-14">{title}</p>
      <p className="ui-caption-muted">{message}</p>
      <Button
        type="button"
        variant="outline"
        className="min-h-11 self-start"
        disabled={retrying}
        onClick={onRetry}
      >
        {retrying ? "Повторная загрузка..." : retryLabel}
      </Button>
    </div>
  );
}
