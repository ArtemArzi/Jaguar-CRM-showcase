import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StudentProfileSummary } from "./student-profile-summary";

describe("StudentProfileSummary", () => {
  it("renders existing student fields in human-readable form", () => {
    render(
      <StudentProfileSummary
        student={{
          id: 3,
          first_name: "Masha",
          last_name: "Ivanova",
          phone: "+79991234567",
          email: "masha@test.com",
          status: "at_risk",
          is_child: true,
          date_of_birth: "2015-03-10",
        }}
      />,
    );

    expect(screen.getByText("В риске")).toBeInTheDocument();
    expect(screen.getByText("+79991234567")).toBeInTheDocument();
    expect(screen.getByText("masha@test.com")).toBeInTheDocument();
    expect(screen.getByText("Ребёнок")).toBeInTheDocument();
    expect(screen.getByText("10.03.2015")).toBeInTheDocument();
  });

  it("uses attendance-driven churn copy instead of a harsh left label", () => {
    render(
      <StudentProfileSummary
        student={{
          id: 4,
          first_name: "Petr",
          last_name: "Petrov",
          phone: "",
          email: "",
          status: "churned",
          is_child: false,
          date_of_birth: null,
        }}
      />,
    );

    expect(screen.getByText("Не посещает 30+ дней")).toBeInTheDocument();
    expect(screen.queryByText("Ушёл")).not.toBeInTheDocument();
  });
});
