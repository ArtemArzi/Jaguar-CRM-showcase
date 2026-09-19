import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SalaryCard, type EarningData } from "./salary-card";

function earning(overrides: Partial<EarningData> = {}): EarningData {
  return {
    id: 1,
    earning_type: "personal",
    amount: "3000.00",
    rate_percent: "50.00",
    subscription_price: "6000.00",
    checkin_date: "2026-06-16",
    schedule_name: "Personal Training",
    ...overrides,
  };
}

describe("SalaryCard", () => {
  it("shows package transfer metadata without changing payable amount", () => {
    render(
      <SalaryCard
        earning={earning({
          package_owner_trainer_name: "Owner Coach",
          package_transfer_amount_basis: "3000.00",
          package_transfer_payable_delta: "0.00",
          package_transfer_affects_payroll: false,
          package_transfer_reason: "package_owner_differs_from_actual_trainer",
        })}
      />,
    );

    expect(screen.getByText("Пакет куплен у Owner Coach")).toBeInTheDocument();
    expect(screen.getByText("Не влияет на сумму зарплаты")).toBeInTheDocument();
    expect(screen.getByText(/3\s000\s₽/)).toBeInTheDocument();
  });

  it("shows a data error when package owner identity is incomplete", () => {
    render(
      <SalaryCard
        earning={earning({
          package_owner_trainer_id: 12,
          package_owner_trainer_name: "",
          package_transfer_reason: "package_owner_differs_from_actual_trainer",
        })}
      />,
    );

    expect(
      screen.getByText("Пакет привязан к другому тренеру, но имя не загрузилось"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Проверьте владельца пакета в карточке начисления."),
    ).toBeInTheDocument();
  });

  it("renders manual adjustment rows as corrections", () => {
    render(
      <SalaryCard
        earning={earning({
          row_type: "adjustment",
          earning_type: "manual_adjustment",
          amount: "-500.00",
          rate_percent: "0.00",
          subscription_price: null,
          schedule_name: "",
          adjustment_reason: "manual penalty",
          adjustment_direction: "debit",
        })}
      />,
    );

    expect(screen.getAllByText("Корректировка").length).toBeGreaterThan(0);
    expect(screen.getByText("manual penalty")).toBeInTheDocument();
    expect(screen.getByText(/-500\s₽/)).toBeInTheDocument();
  });

  it("labels refund payroll adjustments explicitly", () => {
    render(
      <SalaryCard
        earning={earning({
          row_type: "adjustment",
          earning_type: "refund",
          amount: "-200.00",
          rate_percent: "0.00",
          subscription_price: null,
          schedule_name: "",
          adjustment_reason: "Provider refund",
          adjustment_direction: "debit",
        })}
      />,
    );

    expect(screen.getAllByText("Возврат оплаты").length).toBeGreaterThan(0);
    expect(screen.getByText("Provider refund")).toBeInTheDocument();
    expect(screen.getByText(/-200\s₽/)).toBeInTheDocument();
  });
});
