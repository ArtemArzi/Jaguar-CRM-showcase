import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  OnlinePaymentLinkPanel,
  type BankPaymentOrderLink,
} from "./online-payment-link-panel";

const { renderQrSvg } = vi.hoisted(() => ({
  renderQrSvg: vi.fn(() => "<svg><path /></svg>"),
}));

vi.mock("uqr", () => ({
  renderSVG: renderQrSvg,
}));

function order(overrides: Partial<BankPaymentOrderLink> = {}): BankPaymentOrderLink {
  return {
    id: 44,
    subscription_id: 54,
    tariff_id: 3,
    debt_ids: [],
    status: "pending",
    amount_snapshot: "5000.00",
    currency: "RUB",
    provider_payment_link_id: "jgr-44-test",
    provider_payment_url: "https://pay.example/jgr-44-test",
    provider_status: "CREATED",
    expires_at: "2099-06-28T12:00:00Z",
    paid_at: null,
    purpose_snapshot: "Абонемент Base",
    can_pay: true,
    can_share: false,
    can_copy: false,
    can_show_qr: false,
    can_request_refresh: true,
    can_cancel: true,
    ...overrides,
  };
}

describe("OnlinePaymentLinkPanel", () => {
  beforeEach(() => {
    renderQrSvg.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses same-context SBP payment for self-service orders", () => {
    render(<OnlinePaymentLinkPanel order={order()} />);

    expect(screen.getByRole("link", { name: "Оплатить через СБП" })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-44-test",
    );
    expect(screen.getByText("Абонемент Base")).toBeInTheDocument();
    expect(screen.queryByText("https://pay.example/jgr-44-test")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать QR" })).not.toBeInTheDocument();
  });

  it("shows the bought tariff separately from the accepted renewal target", () => {
    render(
      <OnlinePaymentLinkPanel
        order={order({
          renewed_from_subscription_id: 54,
          renewal_source_tariff_id: 3,
          renewal_source_tariff_name: "Base старый",
          renewal_target_tariff_id: 9,
          renewal_target_tariff_name: "Base новый",
          renewal_target_price: "6500.00",
        })}
      />,
    );

    expect(screen.getByText("Купленный абонемент:")).toBeInTheDocument();
    expect(screen.getByText("Base старый")).toBeInTheDocument();
    expect(screen.getByText("Продление:")).toBeInTheDocument();
    expect(screen.getAllByText(/Base новый · 6\s?500/)).not.toHaveLength(0);
  });

  it("uses staff-only share, copy, QR, and labelled preview actions", async () => {
    render(
      <OnlinePaymentLinkPanel
        order={order({
          can_pay: false,
          can_share: true,
          can_copy: true,
          can_show_qr: true,
        })}
      />,
    );

    expect(screen.getByRole("button", { name: "Отправить ссылку" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Ссылка на оплату" })).toHaveClass(
      "pb-[max(0.75rem,env(safe-area-inset-bottom))]",
    );
    expect(screen.getByRole("button", { name: "Скопировать ссылку" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть предпросмотр" })).toHaveAttribute("target", "_blank");
    fireEvent.click(screen.getByRole("button", { name: "Показать QR" }));
    await waitFor(() => {
      expect(screen.getByRole("img", { name: "QR-код ссылки на оплату" })).toBeInTheDocument();
    });
    expect(renderQrSvg).toHaveBeenCalledWith(
      "https://pay.example/jgr-44-test",
      expect.objectContaining({ ecc: "M", border: 1 }),
    );
  });

  it("hides payment actions for terminal order statuses", () => {
    render(
      <OnlinePaymentLinkPanel
        order={order({ status: "approved" })}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Оплачена");
    expect(screen.getByText(/Оплата подтверждена. Платить повторно не нужно/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Оплатить через СБП/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Скопировать ссылку" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать QR" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Отменить/ })).not.toBeInTheDocument();
  });

  it("keeps payment confirmation separate from pending fulfillment", () => {
    const onRefresh = vi.fn();
    const onRequestRefresh = vi.fn();
    render(
      <OnlinePaymentLinkPanel
        order={order({
          status: "approved",
          fulfillment_state: "fulfillment_pending",
          can_request_refresh: false,
        })}
        onRefresh={onRefresh}
        onRequestRefresh={onRequestRefresh}
      />,
    );

    expect(screen.getByText("Оплата подтверждена")).toBeInTheDocument();
    expect(
      screen.getAllByText(/абонемент или запись ещё активируются/),
    ).toHaveLength(2);
    expect(screen.queryByRole("link", { name: /Оплатить через СБП/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent(
      /Оплачена.*абонемент или запись ещё активируются/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Обновить данные" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onRequestRefresh).not.toHaveBeenCalled();
  });

  it("keeps self-service payment but hides cancel when the order cannot be cancelled", () => {
    render(
      <OnlinePaymentLinkPanel
        order={order({ can_cancel: false })}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("link", { name: /Оплатить через СБП/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Скопировать ссылку" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать QR" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Отменить/ })).not.toBeInTheDocument();
  });

  it("uses an in-app confirmation sheet before cancelling a payment link", () => {
    const paymentOrder = order();
    const onCancel = vi.fn();
    render(
      <OnlinePaymentLinkPanel
        order={paymentOrder}
        cancelLabel="Отменить продление"
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Отменить продление" }));

    expect(onCancel).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Отменить оплату?")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/локальная отмена не отзывает/),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Отменить продление" }));

    expect(onCancel).toHaveBeenCalledWith(paymentOrder);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("uses unique QR regions when the same staff order is rendered twice", () => {
    render(
      <>
        <OnlinePaymentLinkPanel order={order({ can_pay: false, can_show_qr: true })} />
        <OnlinePaymentLinkPanel order={order({ can_pay: false, can_show_qr: true })} />
      </>,
    );

    const qrToggles = screen.getAllByRole("button", { name: "Показать QR" });
    expect(qrToggles[0]).toHaveAttribute("aria-controls");
    expect(qrToggles[1]).toHaveAttribute("aria-controls");
    expect(qrToggles[0].getAttribute("aria-controls")).not.toBe(
      qrToggles[1].getAttribute("aria-controls"),
    );
  });

  it("expires its actions on time even without a refresh callback", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
    render(
      <OnlinePaymentLinkPanel
        order={order({ expires_at: "2030-01-01T00:00:01Z" })}
      />,
    );

    expect(screen.getByRole("link", { name: "Оплатить через СБП" })).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_100);
    });

    expect(screen.queryByRole("link", { name: "Оплатить через СБП" })).not.toBeInTheDocument();
    expect(screen.getByText(/Срок действия ссылки истёк/)).toBeInTheDocument();
  });

  it.each([
    ["manual_review", "Не создавайте новую попытку"],
    ["refunded", "Оплата возвращена полностью"],
    ["refunded_partially", "Часть оплаты возвращена"],
  ])("uses status-specific copy for %s", (status, copy) => {
    render(
      <OnlinePaymentLinkPanel
        order={order({ status, can_pay: false, can_cancel: false })}
        subtitle="Проверяем итог"
      />,
    );

    expect(screen.getByText("Проверяем итог")).toBeInTheDocument();
    expect(screen.getByText(/Ссылка действует до/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(copy))).toBeInTheDocument();
  });
});
