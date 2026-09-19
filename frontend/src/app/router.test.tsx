import { describe, it, expect } from "vitest";
import { router } from "./router";

describe("router", () => {
  it("has routes for login and all 4 shells", () => {
    const paths = router.routes.map((r) => r.path);
    expect(paths).toContain("/app");
    expect(paths).toContain("/login");
    expect(paths).toContain("/parent-invite/:token");
    expect(paths).toContain("/kiosk/*");
    expect(paths).toContain("/trainer");
    expect(paths).toContain("/student");
    expect(paths).toContain("/parent");
  });

  it("keeps parent invite acceptance outside the protected parent shell", () => {
    const inviteRoute = router.routes.find((r) => r.path === "/parent-invite/:token");
    const parentRoute = router.routes.find((r) => r.path === "/parent");

    expect(inviteRoute).toBeDefined();
    expect(parentRoute?.children).toBeDefined();
    expect(parentRoute?.children?.map((r) => r.path)).not.toContain("/parent-invite/:token");
    expect(parentRoute?.children?.map((r) => r.path)).not.toContain("parent-invite/:token");
  });

  it("has catch-all redirect", () => {
    const catchAll = router.routes.find((r) => r.path === "*");
    expect(catchAll).toBeDefined();
    expect(catchAll?.element).toMatchObject({
      props: { to: "/app", replace: true },
    });
  });
});
