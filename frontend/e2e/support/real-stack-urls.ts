export function backendUrl(path: string): string {
  const baseUrl = process.env.REAL_STACK_E2E_BACKEND_URL ?? process.env.VITE_API_URL ?? "http://127.0.0.1:8011";
  return new URL(path, baseUrl).toString();
}
