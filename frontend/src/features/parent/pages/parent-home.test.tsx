import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ParentHome from "./parent-home";
import { getPersonalAvailabilityCapabilityQueryKey } from "@/api/unified-client-journey";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

vi.mock("@/features/notifications/components/push-permission-banner", () => ({
  default: () => <div>push-banner</div>,
}));

vi.mock("@/features/notifications/components/notification-preferences", () => ({
  default: () => <div>notification-preferences</div>,
}));

function renderParentHome({
  unifiedClientJourneyEnabled = false,
  skipPersonalAvailabilityCapabilitySeed = false,
}: {
  unifiedClientJourneyEnabled?: boolean;
  skipPersonalAvailabilityCapabilitySeed?: boolean;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  if (!skipPersonalAvailabilityCapabilitySeed) {
    queryClient.setQueryData(getPersonalAvailabilityCapabilityQueryKey(
      1,
      useAuthStore.getState().accessToken,
      useAuthStore.getState().role,
    ), {
      enabled: unifiedClientJourneyEnabled,
      staff_command_protocol_version: "v1",
    });
  }

  return render(
    <MemoryRouter initialEntries={["/parent"]}>
      <QueryClientProvider client={queryClient}>
        <ParentHome />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ParentHome redesign contract", () => {
  beforeEach(() => {
    get.mockReset();
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJwYXJlbnQtMSJ9.signature",
      role: "parent",
      clubId: 1,
      isAuthenticated: true,
    });
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
  });

  it("keeps a child terminal personal receipt visible after the club returns to legacy mode", async () => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: 6,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: null,
              next_training_day_of_week: null,
              next_training_start_time: null,
              next_training_group_name: null,
              next_training_trainer_name: null,
            },
          ],
        });
      }
      if (url === "/personal-availability/self-service/commands/") {
        expect(config?.params).toEqual({ child_student_id: 1 });
        return Promise.resolve({
          data: {
            live: [],
            latest_terminal: [
              {
                command_id: 51,
                slot_id: 77,
                capability: "can_pay",
                status: "cancelled",
                starts_at: "2099-08-13T10:00:00+05:00",
                ends_at: "2099-08-13T11:00:00+05:00",
                booking_id: null,
                reservation_id: 41,
                bank_payment_order_id: 123,
                provider_payment_url: "",
                amount_snapshot: "2700.00",
                order_status: "cancelled",
                allowed_actions: [],
              },
            ],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome();

    expect(await screen.findByText("Отменено")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Персональные тренировки: Masha Ivanova" })).toBeInTheDocument();
  });

  it("renders a dedicated main child hero for a single child instead of only a flat list", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: 6,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: "2026-04-15",
              next_training_day_of_week: 2,
              next_training_start_time: "17:30",
              next_training_group_name: "Kids Muay Thai",
              next_training_trainer_name: "Ivan Petrov",
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome();

    const singleChildRegion = await screen.findByRole("region", {
      name: "Ребёнок",
    });

    expect(screen.getByRole("heading", { name: "Мой ребёнок" })).toBeInTheDocument();
    const quickActions = screen.getByRole("region", {
      name: "Быстрые действия",
    });
    expect(within(quickActions).getByRole("link", { name: /Абонемент/ })).toHaveAttribute(
      "href",
      "/parent/child/1#subscription",
    );
    expect(within(quickActions).getByRole("link", { name: /Группы/ })).toHaveAttribute(
      "href",
      "/parent/child/1#groups",
    );
    expect(within(quickActions).getByRole("link", { name: /Активность/ })).toHaveAttribute(
      "href",
      "/parent/child/1#activity",
    );
    expect(within(quickActions).getByRole("link", { name: /Опрос/ })).toHaveAttribute(
      "href",
      "/parent/child/1/feedback",
    );
    expect(within(singleChildRegion).getByText("Masha Ivanova")).toBeInTheDocument();
    expect(within(singleChildRegion).getByText("Среда, 17:30")).toBeInTheDocument();
    expect(within(singleChildRegion).getByText(/Kids Muay Thai/)).toBeInTheDocument();
    expect(within(singleChildRegion).getByText(/Ivan Petrov/)).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Главный ребёнок" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Остальные дети" }),
    ).not.toBeInTheDocument();
  });

  it("keeps parent personal-payment history labelled and scoped to each child when enabled", async () => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 8,
              first_name: "Маша",
              last_name: "Иванова",
              status: "active",
              is_child: true,
              grade_name: null,
              subscription_remaining: 3,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: null,
              next_training_day_of_week: null,
              next_training_start_time: null,
              next_training_group_name: null,
              next_training_trainer_name: null,
            },
          ],
        });
      }
      if (url === "/personal-availability/self-service/commands/") {
        expect(config?.params).toEqual({ child_student_id: 8 });
        return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome({ unifiedClientJourneyEnabled: true });

    expect(await screen.findByText("Персональные тренировки: Маша Иванова")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/personal-availability/self-service/commands/", {
      params: { child_student_id: 8 },
    });
    expect(await screen.findByText("Нет активных или недавних персональных записей")).toBeInTheDocument();
  });

  it("does not request child personal commands while the capability fails", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") return Promise.reject(new Error("offline"));
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: 6,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: "2026-04-15",
              next_training_day_of_week: 2,
              next_training_start_time: "17:30",
              next_training_group_name: "Kids Muay Thai",
              next_training_trainer_name: "Ivan Petrov",
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome({ skipPersonalAvailabilityCapabilitySeed: true });

    expect(
      await screen.findByText("Персональная запись временно недоступна"),
    ).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/self-service/commands/",
      expect.anything(),
    );
  });

  it("renders a main child focus with compact secondary children when the family has multiple children", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: 6,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: "2026-04-15",
            },
            {
              id: 2,
              first_name: "Petr",
              last_name: "Petrov",
              status: "at_risk",
              is_child: true,
              grade_name: "Orange belt",
              subscription_remaining: 1,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: "2026-03-20",
            },
            {
              id: 3,
              first_name: "Lena",
              last_name: "Sidorova",
              status: "trial",
              is_child: true,
              grade_name: null,
              subscription_remaining: null,
              subscription_total: null,
              subscription_status: null,
              last_visit_date: null,
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome();

    const mainChildHero = await screen.findByRole("region", {
      name: "Главный ребёнок",
    });
    const secondaryChildren = screen.getByRole("region", {
      name: "Остальные дети",
    });

    expect(screen.getByRole("heading", { name: "Мои дети" })).toBeInTheDocument();
    expect(within(mainChildHero).getByText("Masha Ivanova")).toBeInTheDocument();
    expect(within(secondaryChildren).getByText("Petr Petrov")).toBeInTheDocument();
    expect(within(secondaryChildren).getByText("Lena Sidorova")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Требует внимания" }),
    ).toBeInTheDocument();
  });

  it("shows active unlimited subscriptions as active instead of requiring attention", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: null,
              subscription_total: null,
              subscription_status: "active",
              last_visit_date: "2026-04-15",
              next_training_day_of_week: null,
              next_training_start_time: null,
              next_training_group_name: null,
              next_training_trainer_name: null,
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome();

    expect(await screen.findByText("Безлимит")).toBeInTheDocument();
    expect(screen.queryByText("Нет активного абонемента")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Требует внимания" })).not.toBeInTheDocument();
  });

  it("shows pending freeze requests on active child subscriptions", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: 5,
              subscription_total: 8,
              subscription_status: "active",
              subscription_freeze_status: "pending",
              last_visit_date: "2026-04-15",
              next_training_day_of_week: null,
              next_training_start_time: null,
              next_training_group_name: null,
              next_training_trainer_name: null,
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome();

    expect(
      (await screen.findAllByText("Заморозка ожидает подтверждения")).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("Осталось 5 занятий").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("region", { name: "Требует внимания" }),
    ).toBeInTheDocument();
  });

  it("shows readable next-training exception state in the child home preview", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              first_name: "Masha",
              last_name: "Ivanova",
              status: "active",
              is_child: true,
              grade_name: "Yellow belt",
              subscription_remaining: 6,
              subscription_total: 8,
              subscription_status: "active",
              last_visit_date: "2026-04-15",
              next_training_day_of_week: 2,
              next_training_start_time: "17:30",
              next_training_group_name: "Kids Muay Thai",
              next_training_trainer_name: "Alex Backup",
              next_training_is_rescheduled: true,
              next_training_is_substitute: true,
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentHome();

    const singleChildRegion = await screen.findByRole("region", {
      name: "Ребёнок",
    });

    expect(within(singleChildRegion).getByText("Перенос")).toBeInTheDocument();
    expect(within(singleChildRegion).getByText("Замена тренера")).toBeInTheDocument();
    expect(within(singleChildRegion).getByText(/Alex Backup/)).toBeInTheDocument();
  });

  it("shows a distinct retryable error state when children cannot load", async () => {
    get.mockRejectedValue(new Error("children failed"));

    renderParentHome();

    expect(
      await screen.findByText("Не удалось загрузить кабинет родителя"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(screen.queryByText("Нет привязанных детей")).not.toBeInTheDocument();
  });
});
