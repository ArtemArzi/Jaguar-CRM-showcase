import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SubscriptionSection } from "./student-detail";

describe("SubscriptionSection", () => {
  it("renders every active subscription for multi-discipline students", () => {
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 1,
            tariff_name: "BJJ Plan",
            trainings_used: 1,
            trainings_total: 12,
            trainings_left: 11,
            expires_at: "2026-06-01",
            status: "active",
          },
          {
            id: 2,
            tariff_name: "Boxing Plan",
            trainings_used: 3,
            trainings_total: 8,
            trainings_left: 5,
            expires_at: "2026-06-15",
            status: "active",
          },
        ]}
      />,
    );

    expect(screen.getByText("BJJ Plan")).toBeInTheDocument();
    expect(screen.getByText("Boxing Plan")).toBeInTheDocument();
  });

  it("renders a pending subscription instead of the empty active state", () => {
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 1,
            tariff_name: "Pending BJJ",
            trainings_used: 0,
            trainings_total: 8,
            trainings_left: 8,
            expires_at: null,
            status: "pending",
          },
        ]}
      />,
    );

    expect(screen.getByText("Pending BJJ")).toBeInTheDocument();
    expect(screen.getByText("Ожидает")).toBeInTheDocument();
    expect(screen.queryByText("Нет активного абонемента")).not.toBeInTheDocument();
  });

  it("renders package owner for personal subscriptions", () => {
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 1,
            tariff_name: "Personal Plan",
            trainings_used: 1,
            trainings_total: 8,
            trainings_left: 7,
            expires_at: "2026-06-15",
            status: "active",
            training_type_kind: "personal",
            package_owner_trainer_id: 4,
            package_owner_trainer_name: "Owner Coach",
          },
        ]}
      />,
    );

    expect(screen.getByText("Пакет тренера: Owner Coach")).toBeInTheDocument();
  });

  it("shows pending freeze requests separately from active status", () => {
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 1,
            tariff_name: "BJJ Plan",
            trainings_used: 1,
            trainings_total: 8,
            trainings_left: 7,
            expires_at: "2026-06-15",
            status: "active",
            freeze_status: "pending",
          },
        ]}
      />,
    );

    expect(screen.getByText("Активен")).toBeInTheDocument();
    expect(screen.getByText("Заявка на заморозку ждёт")).toBeInTheDocument();
  });

  it("does not hide frozen or pending subscriptions when an active one exists", () => {
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 1,
            tariff_name: "Active BJJ",
            trainings_used: 1,
            trainings_total: 12,
            trainings_left: 11,
            expires_at: "2026-06-01",
            status: "active",
          },
          {
            id: 2,
            tariff_name: "Frozen Boxing",
            trainings_used: 3,
            trainings_total: 8,
            trainings_left: 5,
            expires_at: "2026-06-15",
            status: "frozen",
          },
          {
            id: 3,
            tariff_name: "Pending MMA",
            trainings_used: 0,
            trainings_total: 10,
            trainings_left: 10,
            expires_at: null,
            status: "pending",
          },
        ]}
      />,
    );

    expect(screen.getByText("Active BJJ")).toBeInTheDocument();
    expect(screen.getByText("Frozen Boxing")).toBeInTheDocument();
    expect(screen.getByText("Pending MMA")).toBeInTheDocument();
  });

  it("shows a cancelled subscription as a refund outcome when no entitlement remains", () => {
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 7,
            tariff_name: "Refunded BJJ",
            trainings_used: 3,
            trainings_total: 8,
            trainings_left: 5,
            expires_at: "2026-06-15",
            status: "cancelled",
          },
        ]}
      />,
    );

    expect(screen.getByText("Refunded BJJ")).toBeInTheDocument();
    expect(screen.getByText("Отменён после возврата")).toBeInTheDocument();
    expect(screen.queryByText("Нет активного абонемента")).not.toBeInTheDocument();
  });

  it("shows the server-selected renewal target while keeping the bought tariff as the source", () => {
    const onStartContextualRenewal = vi.fn();
    const subscription = {
      id: 7,
      tariff_name: "Base старый",
      renewal_target_tariff_id: 9,
      renewal_target_tariff_name: "Base новый",
      renewal_target_price: "6500.00",
      trainings_used: 3,
      trainings_total: 8,
      trainings_left: 5,
      expires_at: "2026-06-15",
      status: "active",
    };

    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[subscription]}
        canStartContextualRenewal
        onStartContextualRenewal={onStartContextualRenewal}
      />,
    );

    expect(screen.getByText("Base старый")).toBeInTheDocument();
    expect(screen.getByText(/Продление: Base новый · 6\s?500/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Продлить абонемент" }));
    expect(onStartContextualRenewal).toHaveBeenCalledWith(subscription);
  });

  it("shows and refreshes a recent manual-review renewal hidden behind a current subscription", () => {
    const onRefreshOrders = vi.fn();
    render(
      <SubscriptionSection
        isLoading={false}
        isError={false}
        subscriptions={[
          {
            id: 1,
            tariff_name: "Current BJJ",
            trainings_used: 1,
            trainings_total: 12,
            trainings_left: 11,
            expires_at: "2026-09-01",
            status: "active",
          },
          {
            id: 2,
            tariff_name: "Renewal BJJ",
            trainings_used: 0,
            trainings_total: 12,
            trainings_left: 12,
            expires_at: null,
            status: "cancelled",
          },
        ]}
        bankPaymentOrders={[
          {
            id: 901,
            subscription_id: 2,
            tariff_id: 3,
            debt_ids: [],
            status: "manual_review",
            amount_snapshot: "5000.00",
            currency: "RUB",
            purpose_snapshot: "Продление BJJ",
            expires_at: "2099-06-28T12:00:00Z",
            can_request_refresh: true,
          },
        ]}
        onRefreshOrders={onRefreshOrders}
      />,
    );

    expect(screen.getByText("Current BJJ")).toBeInTheDocument();
    expect(screen.getByText("Последняя онлайн-оплата")).toBeInTheDocument();
    expect(screen.getAllByText("Проверка")).not.toHaveLength(0);
    expect(screen.queryByRole("link", { name: /Оплатить через СБП/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Обновить статус" }));
    expect(onRefreshOrders).toHaveBeenCalledTimes(1);
  });
});
