/**
 * Returns whether text on a given background color should be light or dark.
 * Uses WCAG relative luminance formula.
 */
function getLuminance(hex: string): number {
  const clean = hex.replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(clean)) {
    throw new Error("Invalid hex color");
  }
  const r = parseInt(clean.substring(0, 2), 16) / 255;
  const g = parseInt(clean.substring(2, 4), 16) / 255;
  const b = parseInt(clean.substring(4, 6), 16) / 255;

  const toLinear = (c: number) =>
    c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);

  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}

// Luminance threshold for dark/light background distinction.
// 0.179 is the WCAG midpoint; we use a higher value (0.4) because
// our primary backgrounds tend to be deeply saturated brand colors
// where the WCAG midpoint yields too-early light-text switching.
const DARK_BG_THRESHOLD = 0.4;

/** Returns true if the background is dark (text should be white) */
export function isDarkBackground(hexColor: string | undefined): boolean {
  if (!hexColor) return true; // default: assume dark
  try {
    return getLuminance(hexColor) < DARK_BG_THRESHOLD;
  } catch {
    return true;
  }
}

/**
 * Returns an accent color that has sufficient contrast against the background.
 * If the accent is too close to the background, returns white (dark bg) or black (light bg).
 */
export function getContrastingAccent(
  bgHex: string | undefined,
  accentHex: string | undefined,
): string {
  if (!bgHex || !accentHex) return "#FFFFFF";
  try {
    const bgLum = getLuminance(bgHex);
    const accentLum = getLuminance(accentHex);
    const lighter = Math.max(bgLum, accentLum);
    const darker = Math.min(bgLum, accentLum);
    const ratio = (lighter + 0.05) / (darker + 0.05);
    // WCAG AA requires 4.5:1 for text; we use 3:1 as minimum for icons/labels
    if (ratio >= 3) return accentHex;
    return bgLum < DARK_BG_THRESHOLD ? "#FFFFFF" : "#1A1A1A";
  } catch {
    return "#FFFFFF";
  }
}
