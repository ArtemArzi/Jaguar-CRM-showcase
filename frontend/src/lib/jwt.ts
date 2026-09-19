/** Decode JWT payload without library dependency. Returns null on malformed input. */
export function decodeJwtPayload(
  token: string,
): Record<string, unknown> | null {
  try {
    const base64 = token.split(".")[1];
    if (!base64) return null;
    return JSON.parse(atob(base64)) as Record<string, unknown>;
  } catch {
    return null;
  }
}
