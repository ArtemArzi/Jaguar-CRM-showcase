import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import { formatDateRu } from "@/lib/locale";
import type { StudentNote, StudentDetail } from "../types";

interface StudentNotesProps {
  studentId: number;
  notes: StudentNote[];
}

function authorName(email: string): string {
  if (!email || email === "you") return email;
  const atIndex = email.indexOf("@");
  return atIndex > 0 ? email.slice(0, atIndex) : email;
}

export function StudentNotes({ studentId, notes }: StudentNotesProps) {
  const queryClient = useQueryClient();
  const [noteText, setNoteText] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const addNoteMutation = useMutation({
    mutationFn: (text: string) =>
      apiClient.post(`/students/${studentId}/notes/`, { text }),
    onMutate: async (text) => {
      await queryClient.cancelQueries({
        queryKey: ["student", String(studentId)],
      });
      const prev = queryClient.getQueryData<StudentDetail>([
        "student",
        String(studentId),
      ]);
      if (prev) {
        queryClient.setQueryData(["student", String(studentId)], {
          ...prev,
          notes: [
            {
              id: Date.now(),
              text,
              author_email: "you",
              created_at: new Date().toISOString(),
            },
            ...prev.notes,
          ],
        });
      }
      return { prev };
    },
    onError: (_err, _text, context) => {
      if (context?.prev) {
        queryClient.setQueryData(
          ["student", String(studentId)],
          context.prev,
        );
      }
      setErrorMsg("Не удалось добавить заметку");
    },
    onSettled: () => {
      queryClient.invalidateQueries({
        queryKey: ["student", String(studentId)],
      });
      setNoteText("");
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = noteText.trim();
    if (!trimmed) return;
    setErrorMsg(null);
    addNoteMutation.mutate(trimmed);
  }

  return (
    <div className="ui-col-3">
      {/* Add note form */}
      <form onSubmit={handleSubmit} className="ui-col-2">
        <textarea
          value={noteText}
          onChange={(e) => setNoteText(e.target.value)}
          placeholder="Добавить заметку..."
          rows={2}
          className="w-full rounded-xl border border-input bg-white p-3 text-[14px] text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-[var(--branding-accent)]"
        />
        <Button
          type="submit"
          size="sm"
          className="self-end bg-[var(--branding-accent)] text-white hover:opacity-90"
          disabled={!noteText.trim() || addNoteMutation.isPending}
        >
          {addNoteMutation.isPending ? "Отправка..." : "Добавить"}
        </Button>
      </form>

      {/* Error */}
      {errorMsg && (
        <p className="ui-error-center">{errorMsg}</p>
      )}

      {/* Notes feed */}
      {notes.length === 0 ? (
        <p className="ui-muted-14">Нет заметок</p>
      ) : (
        <div className="ui-col-2">
          {notes.map((note) => (
            <div
              key={note.id}
              className="rounded-lg bg-neutral-50 p-3 ring-1 ring-foreground/5"
            >
              <p className="text-[14px] text-foreground">{note.text}</p>
              <div className="mt-1 flex items-center gap-2 text-[12px] text-muted-foreground">
                <span>{authorName(note.author_email)}</span>
                <span>&middot;</span>
                <span>{formatDateRu(note.created_at)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
