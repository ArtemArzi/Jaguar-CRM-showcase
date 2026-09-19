import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Sheet, SheetContent } from "./sheet";

describe("SheetContent", () => {
  it("locks bottom sheets to the viewport while keeping inner scrolling contained", () => {
    render(
      <Sheet open>
        <SheetContent side="bottom">Нижний экран</SheetContent>
      </Sheet>,
    );

    const dialog = screen.getByRole("dialog");

    expect(dialog).toHaveAttribute("data-side", "bottom");
    expect(dialog).toHaveClass("data-[side=bottom]:w-full");
    expect(dialog).toHaveClass("data-[side=bottom]:max-w-[100vw]");
    expect(dialog).toHaveClass("data-[side=bottom]:max-h-[92dvh]");
    expect(dialog).toHaveClass("data-[side=bottom]:overflow-x-hidden");
    expect(dialog).toHaveClass("data-[side=bottom]:overflow-y-auto");
    expect(dialog).toHaveClass("data-[side=bottom]:overscroll-contain");
  });
});
