import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { registerPrivateQueryClient } from "@/api/private-query-cache";

export function Providers({ children }: { children: ReactNode }) {
  const [{ queryClient, unregisterPrivateQueryClient }] = useState(
    () => {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
      });
      return {
        queryClient,
        unregisterPrivateQueryClient: registerPrivateQueryClient(queryClient),
      };
    },
  );
  useEffect(() => unregisterPrivateQueryClient, [unregisterPrivateQueryClient]);
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}
