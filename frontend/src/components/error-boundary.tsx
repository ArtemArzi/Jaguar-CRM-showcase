import { useRouteError, isRouteErrorResponse } from "react-router";

export function RouteErrorBoundary() {
  const error = useRouteError();
  const isChunkError =
    error instanceof Error &&
    (error.message.includes("Loading chunk") ||
      error.message.includes("Failed to fetch dynamically imported module"));

  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-100 p-4">
      <div className="text-center">
        <h1 className="text-xl font-semibold">
          {isChunkError ? "Ошибка загрузки" : "Что-то пошло не так"}
        </h1>
        <p className="mt-2 text-muted-foreground">
          {isChunkError
            ? "Проверьте подключение к интернету"
            : isRouteErrorResponse(error)
              ? error.statusText
              : "Произошла ошибка"}
        </p>
        <button
          onClick={() => window.location.reload()}
          className="mt-4 rounded-lg bg-[var(--branding-accent)] px-6 py-2 text-white"
        >
          Обновить страницу
        </button>
      </div>
    </div>
  );
}
