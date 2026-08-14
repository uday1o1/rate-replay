import { expect, test, type Page } from "@playwright/test";

import {
  installPrivateApi,
  type PrivateApiOptions,
  type PrivateApiRequest,
} from "./mock-private-api";

const password = "correct horse battery staple";

function recordPageErrors(page: Page): Error[] {
  const errors: Error[] = [];
  page.on("pageerror", (error) => errors.push(error));
  return errors;
}

async function createAccountAndReplay(
  page: Page,
  options: PrivateApiOptions,
): Promise<PrivateApiRequest[]> {
  const requests = await installPrivateApi(page, options);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Private local account" }),
  ).toBeVisible();
  await page.getByLabel("Username").fill("owner_one");
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create private account" }).click();

  await expect(
    page.getByRole("heading", { name: "Import interval data" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Use built-in simulated July profile" })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "SIMULATED NREL-derived",
  );
  await expect(
    page.getByText(/never enter utility credentials here/i),
  ).toBeVisible();

  await page
    .getByLabel("Current bill total in dollars, optional")
    .fill("110.00");
  await page
    .getByLabel("Unsupported line description, optional")
    .fill("Local tax");
  await page
    .getByLabel("Unsupported line amount in dollars, optional")
    .fill("2.00");
  await page.getByLabel(/I attest that every locked account fact/i).check();
  await page.getByRole("button", { name: "Create historical replay" }).click();

  await expect(page.getByRole("heading", { name: "$98.19" })).toBeVisible();
  await expect(
    page.getByText(/residual remains signed and visible/i),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Where supported energy charges occurred",
    }),
  ).toBeVisible();
  return requests;
}

function expectProtectedMutation(request: PrivateApiRequest): void {
  expect(request.headers["x-csrf-token"]).toBe("csrf-token");
  expect(request.headers["idempotency-key"]).toMatch(/^browser-/);
}

test("uploads, reviews, acknowledges, and confirms a private interval file", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  const requests = await installPrivateApi(page, {
    comparison: "rankable",
    scenario: "optimal",
  });
  await page.goto("/");
  await page.getByLabel("Username").fill("owner_one");
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create private account" }).click();

  await page
    .getByLabel(/choose one downloaded usage file/i)
    .setInputFiles("../../data/fixtures/espi/independent-pacific-hourly.xml");
  await page.getByRole("button", { name: "Upload securely" }).click();
  await expect(page.getByRole("status")).toContainText("Upload accepted");
  await expect(page.getByText("362", { exact: true })).toBeVisible();
  const confirm = page.getByRole("button", { name: "Confirm complete period" });
  await expect(confirm).toBeDisabled();
  await page.getByLabel("Acknowledge INTERVAL_GAP").check();
  await expect(confirm).toBeDisabled();
  await page.getByLabel(/I confirm this is my PG&E/i).check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(
    page.getByText(/raw upload has entered immediate deletion/i),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create historical replay" }),
  ).toBeVisible();

  const upload = requests.find(
    (request) => request.method === "POST" && request.path === "/v1/imports",
  );
  const confirmation = requests.find(
    (request) =>
      request.method === "POST" &&
      request.path === "/v1/imports/import-one/confirm",
  );
  expect(upload).toBeDefined();
  expect(confirmation).toBeDefined();
  expectProtectedMutation(upload as PrivateApiRequest);
  expect(upload?.headers["content-type"]).toContain("multipart/form-data");
  const confirmationBody = JSON.parse(confirmation?.body ?? "null") as {
    acknowledged_warning_ids: string[];
    pge_service_attested: boolean;
  };
  expect(confirmationBody).toMatchObject({
    acknowledged_warning_ids: ["warning-one"],
    pge_service_attested: true,
  });
  expect(errors).toEqual([]);
});

test("completes the private simulated workflow through a redacted report", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  const requests = await createAccountAndReplay(page, {
    comparison: "rankable",
    scenario: "optimal",
  });

  await page.getByRole("button", { name: "Replay selected plans" }).click();
  const comparison = page.getByRole("region", { name: "Compare July plans" });
  await expect(
    comparison.getByRole("heading", { name: "E-TOU-D", exact: true }),
  ).toBeVisible();
  await expect(comparison.getByText("$17.07", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Preview reference" }).click();
  await expect(
    page.getByRole("heading", { name: "Reference feasibility preview" }),
  ).toBeVisible();
  await expect(
    page.getByText(/server will independently validate every slot/i),
  ).toBeVisible();
  await page.getByRole("button", { name: "Run verified optimization" }).click();
  await expect(
    page.getByRole("heading", { name: "Optimal", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/all four objective stages were proved optimal/i),
  ).toBeVisible();
  await expect(page.getByText(/exact measured reconstruction/i)).toBeVisible();
  await expect(
    page.getByLabel("Reference, heuristic, and exact private schedule heatmap"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Generate redacted report" }).click();
  const report = page.getByRole("region", {
    name: "Redacted historical scheduling report",
  });
  await expect(report).toBeVisible();
  await expect(report.getByText(/no utility identifier/i)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Download displayed redacted JSON" }),
  ).toBeVisible();
  const labels = await report.locator("dt").allTextContents();
  expect(labels).not.toEqual(
    expect.arrayContaining([
      "Account identifier",
      "Interval history",
      "Daily series",
      "Occurrence window",
      "Reference slot",
      "Optimized slot",
    ]),
  );

  const replay = requests.find((request) => request.path === "/v1/replays");
  const comparisonRequest = requests.find(
    (request) => request.path === "/v1/comparisons",
  );
  const scenario = requests.find((request) => request.path === "/v1/scenarios");
  const reportRequest = requests.find(
    (request) => request.path === "/v1/reports/scenario-one/exports",
  );
  expect(replay).toBeDefined();
  expect(comparisonRequest).toBeDefined();
  expect(scenario).toBeDefined();
  expect(reportRequest).toBeDefined();
  for (const request of [replay, comparisonRequest, scenario, reportRequest]) {
    expectProtectedMutation(request as PrivateApiRequest);
  }
  const replayBody = JSON.parse(replay?.body ?? "null") as {
    current_bill_total_cents: number;
    user_unsupported_lines: Array<{ amount_cents: number }>;
  };
  expect(replayBody.current_bill_total_cents).toBe(11000);
  expect(replayBody.user_unsupported_lines).toEqual([
    expect.objectContaining({ amount_cents: 200 }),
  ]);
  const scenarioBody = JSON.parse(scenario?.body ?? "null") as {
    loads: Array<{
      mode: string;
      occurrences: Array<{ reference_schedule: object[] }>;
    }>;
  };
  expect(scenarioBody.loads[0]?.mode).toBe("HISTORICAL_ADDITION");
  expect(
    scenarioBody.loads[0]?.occurrences[0]?.reference_schedule,
  ).toHaveLength(8);
  expect(
    requests.some((request) => request.path.includes("E2E_UNHANDLED_REQUEST")),
  ).toBe(false);
  expect(errors).toEqual([]);
});

test("shows blocked ranking and best-found solver language without false savings claims", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  await createAccountAndReplay(page, {
    comparison: "blocked",
    scenario: "best-found",
  });

  await page.getByRole("button", { name: "Replay selected plans" }).click();
  const comparison = page.getByRole("region", { name: "Compare July plans" });
  await expect(
    comparison.getByRole("heading", { name: "No comparable winner" }),
  ).toBeVisible();
  await expect(
    comparison.getByText(/Candidate Eligibility Unknown/i),
  ).toBeVisible();
  await expect(comparison.getByText("$17.07", { exact: true })).toHaveCount(0);
  await expect(
    comparison.getByText(/Supported-charge savings from E-1/i),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Preview reference" }).click();
  await page.getByRole("button", { name: "Run verified optimization" }).click();
  await expect(
    page.getByRole("heading", { name: "Best found", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/first open stage is 1/i)).toBeVisible();
  await expect(
    page.getByText(/all four objective stages were proved optimal/i),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("reconstructs an existing contiguous appliance reference without double counting", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  const requests = await createAccountAndReplay(page, {
    comparison: "rankable",
    scenario: "existing",
  });

  await page.getByLabel("Load treatment").selectOption("SHIFT_EXISTING");
  await page.getByLabel("Load kind").selectOption("DISHWASHER");
  await page
    .getByLabel("Execution model")
    .selectOption("CONTIGUOUS_FIXED_SHAPE");
  await page
    .getByLabel(/Cycle energy by contiguous slot, Wh/i)
    .fill("100,200,200");
  await page.getByLabel(/complete user-supplied reference represents/i).check();
  await page.getByRole("button", { name: "Preview reference" }).click();
  await expect(page.getByText("0.5 kWh", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Run verified optimization" }).click();

  await expect(
    page.getByRole("heading", { name: "Optimal", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/exact measured reconstruction/i)).toBeVisible();
  const decomposition = page
    .getByRole("heading", {
      name: "Original decomposition and reconstructed unchanged profile",
    })
    .locator("..");
  await expect(
    decomposition.getByText("0.5 kWh", { exact: true }),
  ).toBeVisible();
  await expect(decomposition.getByText("4 kWh", { exact: true })).toHaveCount(
    2,
  );

  const scenarioRequest = requests.find(
    (request) => request.path === "/v1/scenarios",
  );
  expect(scenarioRequest).toBeDefined();
  const body = JSON.parse(scenarioRequest?.body ?? "null") as {
    loads: Array<{
      kind: string;
      mode: string;
      load_id: string;
      execution_spec: { execution_type: string; fixed_slot_shape_wh: number[] };
      occurrences: Array<{ reference_schedule: Array<{ energy_wh: number }> }>;
    }>;
    shift_existing_attestation_load_ids: string[];
  };
  expect(body.loads[0]).toMatchObject({
    kind: "DISHWASHER",
    mode: "SHIFT_EXISTING",
    execution_spec: {
      execution_type: "CONTIGUOUS_FIXED_SHAPE",
      fixed_slot_shape_wh: [100, 200, 200],
    },
  });
  expect(
    body.loads[0]?.occurrences[0]?.reference_schedule.map(
      (slot) => slot.energy_wh,
    ),
  ).toEqual([0, 0, 0, 100, 200, 200, 0, 0]);
  expect(body.shift_existing_attestation_load_ids).toEqual([
    body.loads[0]?.load_id,
  ]);
  expect(errors).toEqual([]);
});

test("blocks invalid references before submission and exposes model failure without a schedule", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  const requests = await createAccountAndReplay(page, {
    comparison: "rankable",
    scenario: "model-invalid",
  });

  const cap = page.getByLabel("Flexible-load aggregate cap, W, optional");
  await cap.fill("100");
  await page.getByRole("button", { name: "Preview reference" }).click();
  await expect(
    page.getByRole("heading", { name: "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED" }),
  ).toBeVisible();
  expect(
    requests.filter((request) => request.path === "/v1/scenarios"),
  ).toHaveLength(0);

  await cap.fill("7200");
  await page.getByRole("button", { name: "Preview reference" }).click();
  await page.getByRole("button", { name: "Run verified optimization" }).click();
  await expect(
    page.getByRole("heading", { name: "EXACT_SOLVER_MODEL_INVALID" }),
  ).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: "published no schedule" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Optimal", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Best found", exact: true }),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("deletes a private profile through its session-independent receipt", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  const requests = await createAccountAndReplay(page, {
    comparison: "rankable",
    scenario: "optimal",
  });

  await page
    .getByLabel("Type DELETE PROFILE to confirm")
    .fill("DELETE PROFILE");
  await page.getByRole("button", { name: "Delete current profile" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Profile deletion started",
  );
  await expect(
    page.getByText(/session-independent deletion receipt/i),
  ).toBeVisible();
  await page.getByRole("button", { name: "Check deletion status" }).click();
  await expect(page.getByRole("status")).toContainText("verified complete");
  await expect(
    page.getByText(/confirm one complete July 2026 profile/i),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "$98.19" })).toHaveCount(0);

  const deletion = requests.find(
    (request) =>
      request.method === "DELETE" &&
      request.path === "/v1/profiles/profile-one",
  );
  const receipt = requests.find(
    (request) =>
      request.method === "GET" && request.path.startsWith("/v1/deletions/"),
  );
  expect(deletion).toBeDefined();
  expect(receipt).toBeDefined();
  expectProtectedMutation(deletion as PrivateApiRequest);
  expect(deletion?.headers["x-deletion-receipt-secret"]).toMatch(
    /^[A-Za-z0-9_-]{43}$/,
  );
  expect(receipt?.headers["x-deletion-receipt-secret"]).toBe(
    deletion?.headers["x-deletion-receipt-secret"],
  );
  expect(errors).toEqual([]);
});

test("clears private state and returns to sign-in when an authenticated request expires", async ({
  page,
}) => {
  const errors = recordPageErrors(page);
  await page.route("**/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/auth/session") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user: { user_id: "owner", username: "owner_one" },
          csrf_token: "csrf-token",
        }),
      });
      return;
    }
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({
        code: "SESSION_EXPIRED",
        message: "The private session expired.",
      }),
    });
  });

  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Private local account" }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toContainText(
    "private session expired",
  );
  await expect(
    page.getByRole("heading", { name: "Replay July E-1" }),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
});
