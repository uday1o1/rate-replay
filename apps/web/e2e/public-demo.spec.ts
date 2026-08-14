import { expect, test, type BrowserContext, type Page } from "@playwright/test";

type RequestRecord = {
  method: string;
  url: string;
};

function recordRequests(page: Page): RequestRecord[] {
  const requests: RequestRecord[] = [];
  page.on("request", (request) => {
    requests.push({ method: request.method(), url: request.url() });
  });
  return requests;
}

function expectStaticOnly(requests: RequestRecord[], origin: string): void {
  expect(requests.length).toBeGreaterThan(0);
  for (const request of requests) {
    const url = new URL(request.url);
    expect(request.method).toBe("GET");
    expect(url.origin).toBe(origin);
    expect(url.pathname.startsWith("/v1/")).toBe(false);
  }
}

async function expectNoVisitorState(context: BrowserContext, page: Page) {
  expect(await context.cookies()).toEqual([]);
  await expect
    .poll(() =>
      page.evaluate(() => ({
        localStorage: window.localStorage.length,
        sessionStorage: window.sessionStorage.length,
      })),
    )
    .toEqual({ localStorage: 0, sessionStorage: 0 });
}

async function advance(
  page: Page,
  buttonName: "Start the walkthrough" | "Continue",
) {
  await page.getByRole("button", { name: buttonName }).click();
}

test("completes the content-locked public demo without API or mutable visitor state", async ({
  context,
  page,
  baseURL,
}) => {
  const requests = recordRequests(page);
  await page.goto("/#demo");

  await expect(
    page.getByRole("heading", {
      name: "One simulated July story, fully traceable",
    }),
  ).toBeVisible();
  await expect(page.getByText(/^Demo manifest [0-9a-f]{64}$/)).toBeVisible();
  await advance(page, "Start the walkthrough");
  await expect(
    page.getByRole("heading", {
      name: "The complete July profile is calculation ready",
    }),
  ).toBeVisible();

  await advance(page, "Continue");
  await expect(
    page.getByRole("heading", {
      name: "Supported charges are separate from the gap",
    }),
  ).toBeVisible();
  await expect(page.getByText(/leaves that gap visible/i)).toBeVisible();
  await expect(page.getByText(/entered bill = .* supported/i)).toBeVisible();

  await advance(page, "Continue");
  await expect(
    page.getByRole("heading", {
      name: "The ranking passes every eligibility and coverage gate",
    }),
  ).toBeVisible();
  await expect(page.getByText("E-TOU-D", { exact: true })).toHaveCount(2);

  await advance(page, "Continue");
  await expect(
    page.getByRole("heading", {
      name: "Move one simulated EV addition on July's actual timestamps",
    }),
  ).toBeVisible();
  await expect(page.getByText(/not a future forecast/i)).toBeVisible();
  await expect(
    page.getByLabel("Reference, heuristic, and exact schedule heatmap"),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Optimal", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/all four objective stages were proved optimal/i),
  ).toBeVisible();
  await expect(
    page.getByText(/independent verification returned valid/i),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Locked simulated baseline" }),
  ).toBeVisible();
  await page.getByText("Schedule values table").click();
  const values = page.getByRole("table", {
    name: /reference, heuristic, and exact energy/i,
  });
  await expect(values).toBeVisible();
  await expect(values.getByRole("row")).toHaveCount(29);

  await advance(page, "Continue");
  const report = page.getByRole("region", {
    name: "Redacted historical scheduling report",
  });
  await expect(report).toBeVisible();
  await expect(report.getByText(/deny-by-default/i)).toBeVisible();
  const displayedLabels = await report.locator("dt").allTextContents();
  expect(displayedLabels).not.toEqual(
    expect.arrayContaining([
      "Account identifier",
      "Interval history",
      "Daily series",
      "Occurrence window",
      "Reference slot",
      "Optimized slot",
    ]),
  );

  expectStaticOnly(
    requests,
    new URL(baseURL ?? "http://127.0.0.1:4175").origin,
  );
  await expectNoVisitorState(context, page);
});

test("keeps two simultaneous browser contexts independent and stateless", async ({
  browser,
  baseURL,
}) => {
  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();
  const firstPage = await firstContext.newPage();
  const secondPage = await secondContext.newPage();
  const firstRequests = recordRequests(firstPage);
  const secondRequests = recordRequests(secondPage);
  try {
    await Promise.all([
      firstPage.goto(`${baseURL}/#demo`),
      secondPage.goto(`${baseURL}/#demo`),
    ]);
    await expect(
      firstPage.getByRole("heading", {
        name: "One simulated July story, fully traceable",
      }),
    ).toBeVisible();
    await expect(
      secondPage.getByRole("heading", {
        name: "One simulated July story, fully traceable",
      }),
    ).toBeVisible();

    await advance(firstPage, "Start the walkthrough");
    await expect(
      firstPage.getByRole("heading", {
        name: "The complete July profile is calculation ready",
      }),
    ).toBeVisible();
    await expect(
      secondPage.getByRole("heading", {
        name: "One simulated July story, fully traceable",
      }),
    ).toBeVisible();

    await secondPage.getByRole("button", { name: "Redacted report" }).click();
    await expect(
      secondPage.getByRole("heading", {
        name: "Redacted historical scheduling report",
      }),
    ).toBeVisible();
    await expect(
      firstPage.getByRole("heading", {
        name: "The complete July profile is calculation ready",
      }),
    ).toBeVisible();

    await firstPage.reload();
    await expect(
      firstPage.getByRole("heading", {
        name: "One simulated July story, fully traceable",
      }),
    ).toBeVisible();
    await expectNoVisitorState(firstContext, firstPage);
    await expectNoVisitorState(secondContext, secondPage);
    const origin = new URL(baseURL ?? "http://127.0.0.1:4175").origin;
    expectStaticOnly(firstRequests, origin);
    expectStaticOnly(secondRequests, origin);
  } finally {
    await firstContext.close();
    await secondContext.close();
  }
});

test("supports keyboard navigation without page-level overflow at a narrow viewport", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();
  try {
    await page.goto(`${baseURL}/#demo`);
    const start = page.getByRole("button", { name: "Start the walkthrough" });
    await start.focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("heading", {
        name: "The complete July profile is calculation ready",
      }),
    ).toBeVisible();
    await expect(page.locator(".demo-step-shell")).toBeFocused();

    for (let step = 0; step < 4; step += 1) {
      const next = page.getByRole("button", { name: "Continue" });
      await next.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(".demo-step-shell")).toBeFocused();
      await expect
        .poll(() =>
          page.evaluate(() => {
            const progress = document.querySelector(
              'nav[aria-label="Public demo progress"]',
            );
            const current = progress?.querySelector(
              'li[aria-current="step"] button',
            );
            if (progress === null || current === null || current === undefined)
              return false;
            const progressRect = progress.getBoundingClientRect();
            const currentRect = current.getBoundingClientRect();
            return (
              currentRect.left >= progressRect.left &&
              currentRect.right <= progressRect.right
            );
          }),
        )
        .toBe(true);
      await expect
        .poll(() =>
          page.evaluate(
            () =>
              document.documentElement.scrollWidth <=
              document.documentElement.clientWidth,
          ),
        )
        .toBe(true);
    }
    await expect(
      page.getByRole("heading", {
        name: "Redacted historical scheduling report",
      }),
    ).toBeVisible();
    await expect(
      page.locator(
        'nav[aria-label="Public demo progress"] li[aria-current="step"]',
      ),
    ).toContainText("Redacted report");
  } finally {
    await context.close();
  }
});

test("fails closed when the static artifact integrity check is corrupted", async ({
  browser,
  baseURL,
}) => {
  const context = await browser.newContext();
  await context.addInitScript(() => {
    const digest = window.crypto.subtle.digest.bind(window.crypto.subtle);
    Object.defineProperty(window.crypto.subtle, "digest", {
      configurable: true,
      value: async (...args: Parameters<SubtleCrypto["digest"]>) => {
        const result = await digest(...args);
        const corrupted = new Uint8Array(result.slice(0));
        corrupted[0] = (corrupted[0] ?? 0) ^ 0xff;
        return corrupted.buffer;
      },
    });
  });
  const page = await context.newPage();
  try {
    await page.goto(`${baseURL}/#demo`);
    await expect(page.getByRole("alert")).toContainText(
      "Artifact integrity check failed",
    );
    await expect(page.getByRole("alert")).toContainText("PUBLIC_DEMO_");
    await expect(
      page.getByRole("heading", {
        name: "One simulated July story, fully traceable",
      }),
    ).toHaveCount(0);
  } finally {
    await context.close();
  }
});
