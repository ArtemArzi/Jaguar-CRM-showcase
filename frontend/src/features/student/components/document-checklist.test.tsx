import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentChecklist } from "./document-checklist";

const post = vi.hoisted(() => vi.fn());

vi.mock("@/api/custom-fetch", () => ({
  default: { post },
}));

const items = [
  {
    documentTypeId: 10,
    documentTypeName: "Медицинская справка",
    description: "",
    isRequired: true,
    isActive: true,
    isProvided: false,
    hasFile: false,
  },
];

describe("DocumentChecklist", () => {
  beforeEach(() => {
    post.mockReset();
  });

  it("limits file picker types to backend-supported document formats", () => {
    const { container } = render(
      <DocumentChecklist items={items} studentId={7} onUploadComplete={vi.fn()} />,
    );

    const input = container.querySelector("input[type='file']");

    expect(input).toHaveAttribute(
      "accept",
      "application/pdf,image/jpeg,image/png,image/webp,.pdf,.jpg,.jpeg,.png,.webp",
    );
  });

  it("rejects HEIC files before upload", () => {
    const { container } = render(
      <DocumentChecklist items={items} studentId={7} onUploadComplete={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    const input = container.querySelector("input[type='file']") as HTMLInputElement;
    const file = new File(["photo"], "photo.heic", { type: "image/heic" });
    fireEvent.change(input, { target: { files: [file] } });

    expect(
      screen.getByText("Формат не поддерживается. Допустимы: PDF, JPEG, PNG или WebP. HEIC/HEIF пока не поддерживаются."),
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});
