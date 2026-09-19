const DOCUMENT_ROUTE_PREFIXES = ["/dashboard/"];

export function isDocumentRoute(path: string): boolean {
  return DOCUMENT_ROUTE_PREFIXES.some((prefix) => path.startsWith(prefix));
}

export function navigateToDocumentRoute(path: string) {
  window.location.assign(path);
}
