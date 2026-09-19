import { useState, useRef, useEffect, useCallback } from "react";
import { activateKiosk } from "@/features/kiosk/lib/kiosk-api";
import { useKioskStore } from "@/features/kiosk/lib/kiosk-store";

interface PinActivationProps {
  onActivated: () => void;
}

const PIN_LENGTH = 6;
const MAX_ATTEMPTS = 3;
const COOLDOWN_SECONDS = 30;

function getActivationErrorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null || !("response" in error)) {
    return null;
  }

  const response = (error as { response?: { data?: { code?: unknown } } }).response;
  return typeof response?.data?.code === "string" ? response.data.code : null;
}

function getActivationErrorMessage(error: unknown): string {
  switch (getActivationErrorCode(error)) {
    case "kiosk_pin_expired":
      return "PIN-код истёк. Сгенерируйте новый PIN в панели администратора.";
    case "kiosk_activation_locked":
      return "Слишком много попыток. Подождите и сгенерируйте новый PIN.";
    case "invalid_kiosk_pin":
      return "Неверный PIN-код";
    default:
      return "Не удалось активировать киоск. Проверьте PIN и подключение.";
  }
}

export default function PinActivation({ onActivated }: PinActivationProps) {
  const [digits, setDigits] = useState<string[]>(Array(PIN_LENGTH).fill(""));
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [failedAttempts, setFailedAttempts] = useState(0);
  const [cooldownLeft, setCooldownLeft] = useState(0);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);
  const activate = useKioskStore((s) => s.activate);

  // Auto-focus first input on mount
  useEffect(() => {
    inputRefs.current[0]?.focus();
  }, []);

  // Cooldown timer
  useEffect(() => {
    if (cooldownLeft <= 0) return;
    const timer = setInterval(() => {
      setCooldownLeft((prev) => {
        if (prev <= 1) return 0;
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldownLeft]);

  const submitPin = useCallback(
    async (pin: string) => {
      if (pin.length !== PIN_LENGTH || cooldownLeft > 0) return;

      setIsLoading(true);
      setError(null);

      try {
        const result = await activateKiosk(pin);
        await activate(result.token, result.club_id, result.club_name);
        onActivated();
      } catch (activationError) {
        const attempts = failedAttempts + 1;
        setFailedAttempts(attempts);
        setError(getActivationErrorMessage(activationError));

        if (attempts >= MAX_ATTEMPTS) {
          setCooldownLeft(COOLDOWN_SECONDS);
          setFailedAttempts(0);
        }

        // Clear input
        setDigits(Array(PIN_LENGTH).fill(""));
        inputRefs.current[0]?.focus();
      } finally {
        setIsLoading(false);
      }
    },
    [cooldownLeft, failedAttempts, activate, onActivated],
  );

  const handleSubmit = useCallback(() => {
    submitPin(digits.join(""));
  }, [digits, submitPin]);

  const handleDigitChange = useCallback(
    (index: number, value: string) => {
      if (!/^\d?$/.test(value)) return;

      const newDigits = [...digits];
      newDigits[index] = value;
      setDigits(newDigits);
      setError(null);

      if (value && index < PIN_LENGTH - 1) {
        inputRefs.current[index + 1]?.focus();
      }

      // Auto-submit on last digit — pass PIN directly, no setTimeout race
      if (value && index === PIN_LENGTH - 1) {
        const pin = newDigits.join("");
        if (pin.length === PIN_LENGTH) {
          submitPin(pin);
        }
      }
    },
    [digits, submitPin],
  );

  const handleKeyDown = useCallback(
    (index: number, e: React.KeyboardEvent) => {
      if (e.key === "Backspace" && !digits[index] && index > 0) {
        inputRefs.current[index - 1]?.focus();
      }
      if (e.key === "Enter") {
        handleSubmit();
      }
    },
    [digits, handleSubmit],
  );

  const isCoolingDown = cooldownLeft > 0;

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-[#1A1A1A] px-4">
      <div className="w-full max-w-sm text-center">
        {/* Logo placeholder */}
        <div className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-[#2A2A2A]">
          <svg
            className="h-8 w-8 text-[var(--branding-accent,#ff6b00)]"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
            />
          </svg>
        </div>

        <h1 className="mb-2 text-xl font-semibold text-white">
          Активация киоска
        </h1>
        <p className="mb-8 text-sm text-neutral-400">
          Введите 6-значный PIN-код из панели администратора
        </p>

        {/* PIN input cells */}
        <div className="mb-6 flex justify-center gap-2">
          {digits.map((digit, i) => (
            <input
              key={i}
              ref={(el) => {
                inputRefs.current[i] = el;
              }}
              type="text"
              inputMode="numeric"
              maxLength={1}
              value={digit}
              onChange={(e) => handleDigitChange(i, e.target.value)}
              onKeyDown={(e) => handleKeyDown(i, e)}
              disabled={isLoading || isCoolingDown}
              className={`h-16 w-16 rounded-lg border-2 bg-[#2A2A2A] text-center text-2xl font-semibold text-white outline-none transition-colors ${
                error
                  ? "border-red-500"
                  : digit
                    ? "border-[var(--branding-accent,#ff6b00)]"
                    : "border-neutral-600 focus:border-[var(--branding-accent,#ff6b00)]"
              } disabled:opacity-50`}
              aria-label={`PIN цифра ${i + 1}`}
            />
          ))}
        </div>

        {/* Error message */}
        {error && (
          <div className="mb-4 text-sm text-red-400" aria-live="assertive">
            <p className="font-medium">{error}</p>
            <p className="mt-1 text-neutral-500">
              Попробуйте ещё раз или обратитесь к владельцу клуба
            </p>
          </div>
        )}

        {/* Cooldown */}
        {isCoolingDown && (
          <div className="mb-4 text-sm text-neutral-400" aria-live="polite">
            Повторная попытка через {cooldownLeft} сек.
          </div>
        )}

        {/* Submit button */}
        <button
          type="button"
          onClick={handleSubmit}
          disabled={
            isLoading || isCoolingDown || digits.join("").length !== PIN_LENGTH
          }
          className="min-h-16 w-full rounded-lg bg-[var(--branding-accent,#ff6b00)] px-6 py-3.5 text-base font-semibold text-white transition-transform active:scale-95 disabled:opacity-50 disabled:active:scale-100"
        >
          {isLoading ? "Активация..." : "Активировать"}
        </button>
      </div>
    </div>
  );
}
