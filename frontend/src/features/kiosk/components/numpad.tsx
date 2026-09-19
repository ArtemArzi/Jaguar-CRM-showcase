import { useCallback } from "react";
import { ArrowLeft, Check } from "lucide-react";

interface NumpadProps {
  onDigit: (digit: string) => void;
  onBackspace: () => void;
  onConfirm: () => void;
  disabled?: boolean;
}

const KEYS = [
  ["1", "2", "3"],
  ["4", "5", "6"],
  ["7", "8", "9"],
  ["backspace", "0", "confirm"],
] as const;

export function Numpad({
  onDigit,
  onBackspace,
  onConfirm,
  disabled = false,
}: NumpadProps) {
  const handlePress = useCallback(
    (key: string) => {
      if (disabled) return;
      if (key === "backspace") {
        onBackspace();
      } else if (key === "confirm") {
        onConfirm();
      } else {
        onDigit(key);
      }
    },
    [disabled, onDigit, onBackspace, onConfirm],
  );

  return (
    <div
      className={`grid grid-cols-3 gap-2 transition-opacity ${disabled ? "opacity-50" : ""}`}
      style={{ touchAction: "manipulation" }}
    >
      {KEYS.flat().map((key) => {
        const isConfirm = key === "confirm";
        const isBackspace = key === "backspace";
        const isDigit = !isConfirm && !isBackspace;

        return (
          <button
            key={key}
            type="button"
            onClick={() => handlePress(key)}
            disabled={disabled}
            aria-label={
              isBackspace
                ? "Удалить последнюю цифру"
                : isConfirm
                  ? "Подтвердить"
                  : `Цифра ${key}`
            }
            className={`flex min-h-16 min-w-16 items-center justify-center rounded-xl text-2xl font-semibold transition-transform duration-[60ms] ease-out active:scale-95 motion-reduce:transition-none motion-reduce:active:scale-100 ${
              isConfirm
                ? "bg-[var(--branding-accent,#ff6b00)] text-white"
                : "bg-[#2A2A2A] text-white hover:bg-[#333333]"
            } disabled:pointer-events-none`}
          >
            {isBackspace && <ArrowLeft className="h-6 w-6" />}
            {isConfirm && <Check className="h-6 w-6" />}
            {isDigit && key}
          </button>
        );
      })}
    </div>
  );
}
