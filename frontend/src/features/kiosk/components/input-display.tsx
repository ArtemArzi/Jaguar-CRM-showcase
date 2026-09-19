interface InputDisplayProps {
  digits: string[];
  maxLength: number;
}

export function InputDisplay({ digits, maxLength }: InputDisplayProps) {
  return (
    <div className="flex justify-center gap-3" aria-label="Введённые цифры">
      {Array.from({ length: maxLength }).map((_, i) => {
        const filled = i < digits.length && digits[i] !== "";
        const isCurrent =
          i === digits.length ||
          (i === digits.length - 1 && digits.length === maxLength);

        return (
          <div
            key={i}
            className={`flex h-16 w-14 items-center justify-center rounded-lg border-2 transition-colors ${
              filled
                ? "border-[var(--branding-accent,#ff6b00)] bg-[#2A2A2A]"
                : isCurrent
                  ? "border-[var(--branding-accent,#ff6b00)] bg-[#2A2A2A]"
                  : "border-neutral-600 bg-[#2A2A2A]"
            }`}
          >
            {filled && (
              <span className="text-5xl font-semibold leading-none text-white">
                {digits[i]}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
