import { describe, expect, it } from "vitest";
import { buttonVariants } from "./button-variants";

describe("buttonVariants", () => {
  it("supports wrapped labels for narrow mobile layouts", () => {
    const classes = buttonVariants({ size: "lg", wrap: true } as never);

    expect(classes).toContain("whitespace-normal");
    expect(classes).toContain("h-auto");
    expect(classes).toContain("min-h-9");
  });
});
