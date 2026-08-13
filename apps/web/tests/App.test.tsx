import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";

describe("App", () => {
  it("names the product and makes no unsupported release claim", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "RateReplay" })).toBeVisible();
    expect(screen.queryByText(/official bill/i)).not.toBeInTheDocument();
  });
});
