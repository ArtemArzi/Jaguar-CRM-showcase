import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useBrandingStore } from "@/features/branding/use-branding";
import { StudentBrandedHeader } from "./student-branded-header";

describe("StudentBrandedHeader", () => {
  beforeEach(() => {
    useBrandingStore.setState({
      primaryColor: "#000000",
      accentColor: "#FF6B00",
      clubName: "CRM Jaguar",
      logoUrl: "",
    });
  });

  it("renders the club name from branding store", () => {
    useBrandingStore.setState({ clubName: "Fight Club Ural" });

    render(<StudentBrandedHeader />);

    expect(screen.getByText("FIGHT CLUB URAL")).toBeInTheDocument();
    expect(screen.queryByText("Клуб")).not.toBeInTheDocument();
  });

  it("falls back to icon when logo is absent", () => {
    render(<StudentBrandedHeader />);

    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("[data-student-shell-header] svg")).not.toBeNull();
  });

  it("renders a real image when logo is present", () => {
    useBrandingStore.setState({ logoUrl: "https://example.com/logo.png" });

    render(<StudentBrandedHeader />);

    expect(document.querySelector("img")).not.toBeNull();
  });
});
