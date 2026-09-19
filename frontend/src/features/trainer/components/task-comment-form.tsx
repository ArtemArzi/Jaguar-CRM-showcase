import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";

interface TaskCommentFormProps {
  taskId: number;
  onCommentAdded?: () => void;
}

export function TaskCommentForm({
  taskId,
  onCommentAdded,
}: TaskCommentFormProps) {
  const [text, setText] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (commentText: string) =>
      apiClient.post(`/retention/tasks/${taskId}/comments/`, {
        text: commentText,
      }),
    onSuccess: () => {
      setText("");
      queryClient.invalidateQueries({
        queryKey: ["retention-task", taskId, "comments"],
      });
      onCommentAdded?.();
    },
  });

  function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed) return;
    mutation.mutate(trimmed);
  }

  return (
    <div className="flex gap-2 items-end">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Добавить комментарий..."
        className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-[14px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring min-h-[44px] max-h-[120px] resize-none"
        rows={1}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSubmit();
          }
        }}
      />
      <Button
        size="icon"
        onClick={handleSubmit}
        disabled={!text.trim() || mutation.isPending}
        className="h-[44px] w-[44px] shrink-0 bg-[var(--branding-accent)] text-white hover:opacity-90"
      >
        <Send size={18} />
      </Button>
    </div>
  );
}
