import { expect, test, type Page } from "@playwright/test";

type RequestRecord = {
  method: string;
  url: string;
};

const VIDEO_PATH = "../../docs/demo/ratereplay-demo.webm";

async function hold(page: Page, milliseconds: number): Promise<void> {
  await page.waitForTimeout(milliseconds);
}

async function show(page: Page, text: string): Promise<void> {
  const content = page.getByText(text, { exact: false }).first();
  await content.scrollIntoViewIfNeeded();
}

test("records the complete immutable public walkthrough", async ({
  browser,
  baseURL,
}) => {
  if (baseURL === undefined) {
    throw new Error("DEMO_VIDEO_BASE_URL_MISSING");
  }
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    recordVideo: {
      dir: "../../test-results/demo-video/raw",
      size: { width: 1280, height: 720 },
    },
  });
  const page = await context.newPage();
  const requests: RequestRecord[] = [];
  page.on("request", (request) => {
    requests.push({ method: request.method(), url: request.url() });
  });

  const video = page.video();
  expect(video).not.toBeNull();
  try {
    await page.goto(`${baseURL}/#demo`);
    await expect(
      page.getByRole("heading", {
        name: "One simulated July story, fully traceable",
      }),
    ).toBeVisible();

    await hold(page, 6_000);
    await show(page, "One simulated July story, fully traceable");
    await hold(page, 10_000);

    await page.getByRole("button", { name: "Start the walkthrough" }).click();
    await expect(
      page.getByRole("heading", {
        name: "The complete July profile is calculation ready",
      }),
    ).toBeVisible();
    await hold(page, 12_000);

    await page.getByRole("button", { name: "Continue" }).click();
    await expect(
      page.getByRole("heading", {
        name: "Supported charges are separate from the gap",
      }),
    ).toBeVisible();
    await hold(page, 12_000);
    await show(page, "The signed residual is the entered bill");
    await hold(page, 7_000);

    await page.getByRole("button", { name: "Continue" }).click();
    await expect(
      page.getByRole("heading", {
        name: "The ranking passes every eligibility and coverage gate",
      }),
    ).toBeVisible();
    await hold(page, 14_000);

    await page.getByRole("button", { name: "Continue" }).click();
    await expect(
      page.getByRole("heading", {
        name: "Move one simulated EV addition on July's actual timestamps",
      }),
    ).toBeVisible();
    await hold(page, 15_000);
    await page
      .getByLabel("Reference, heuristic, and exact schedule heatmap")
      .scrollIntoViewIfNeeded();
    await hold(page, 8_000);
    await page.getByText("Schedule values table").click();
    await expect(
      page.getByRole("table", {
        name: /reference, heuristic, and exact energy/i,
      }),
    ).toBeVisible();
    await hold(page, 6_000);

    await page.getByRole("button", { name: "Continue" }).click();
    await expect(
      page.getByRole("heading", {
        name: "Redacted historical scheduling report",
      }),
    ).toBeVisible();
    await hold(page, 13_000);
    await show(page, "Allowlisted supported charge aggregates");
    await hold(page, 7_000);

    const origin = new URL(baseURL).origin;
    expect(requests.length).toBeGreaterThan(0);
    for (const request of requests) {
      const url = new URL(request.url);
      expect(request.method).toBe("GET");
      expect(url.origin).toBe(origin);
      expect(url.pathname.startsWith("/v1/")).toBe(false);
    }
    expect(await context.cookies()).toEqual([]);
    await expect
      .poll(() =>
        page.evaluate(() => ({
          localStorage: window.localStorage.length,
          sessionStorage: window.sessionStorage.length,
        })),
      )
      .toEqual({ localStorage: 0, sessionStorage: 0 });
  } finally {
    await page.close();
    await context.close();
  }

  await video?.saveAs(VIDEO_PATH);
});
