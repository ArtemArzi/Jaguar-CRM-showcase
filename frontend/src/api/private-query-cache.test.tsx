import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";
import {
  privateQueryScope,
  registerPrivateQueryClient,
} from "./private-query-cache";

function testJwt(subject: string) {
  return `header.${btoa(JSON.stringify({ sub: subject }))}.signature`;
}

function NestedPrivateConsumer() {
  usePrivateQueryScope("parent");
  return <span>nested private consumer</span>;
}

function PrivateConsumerFixture() {
  const [nested, setNested] = useState(true);
  return (
    <>
      {nested ? <NestedPrivateConsumer /> : null}
      <button type="button" onClick={() => setNested(false)}>
        Unmount nested
      </button>
    </>
  );
}

describe("private query client registration", () => {
  afterEach(() => {
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: null,
      clubId: null,
      isAuthenticated: false,
    });
  });

  it("keeps the app registration after a nested consumer unmounts and purges on actor transition", () => {
    const queryClient = new QueryClient();
    const unregisterApp = registerPrivateQueryClient(queryClient);
    const parentAToken = testJwt("parent-a");
    useAuthStore.setState({
      accessToken: parentAToken,
      role: "parent",
      clubId: 1,
      isAuthenticated: true,
    });
    const privateKey = [
      "parent",
      "child",
      7,
      ...privateQueryScope({
        clubId: 1,
        actorSubject: "parent-a",
        audience: "parent",
        role: "parent",
      }),
    ] as const;
    queryClient.setQueryData(privateKey, { private_name: "Parent A" });

    render(
      <QueryClientProvider client={queryClient}>
        <PrivateConsumerFixture />
      </QueryClientProvider>,
    );
    expect(screen.getByText("nested private consumer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Unmount nested" }));
    expect(screen.queryByText("nested private consumer")).not.toBeInTheDocument();

    act(() => useAuthStore.getState().setTokens(testJwt("parent-b")));
    expect(queryClient.getQueryData(privateKey)).toBeUndefined();
    unregisterApp();
  });
});
