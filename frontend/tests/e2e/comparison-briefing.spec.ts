import { expect, test } from "@playwright/test";
import { makeBriefing } from "../../src/__tests__/fixtures/comparisonBriefing";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`@smoke comparison briefing citations and retry at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    let requests = 0;
    await page.route("**/api/**", async (route) => {
      if (new URL(route.request().url()).pathname.endsWith("/market-context/briefing/stream")) {
        requests += 1;
        expect(route.request().postDataJSON()).toEqual({ anchor: "AAPL", comparisons: ["SPY"], period: "1M" });
        const result = makeBriefing();
        if (requests > 1) {
          result.status = "unavailable"; result.engine = "unavailable";
          result.observations = []; result.limitations = []; result.unknowns = [];
          result.failure = { code: "provider_unavailable", message: "The model could not be reached.", retryable: true };
          result.evidence!.packet_id = "b".repeat(64);
        }
        await route.fulfill({ contentType: "application/x-ndjson", body: [
          { type: "start", phase: "queued", deadlines: { evidence_seconds: 45, model_seconds: 240 } },
          { type: "progress", phase: "generating" }, { type: "result", result },
        ].map((event) => JSON.stringify(event) + "\n").join("") });
        return;
      }
      const data = new URL(route.request().url()).pathname.endsWith("/market-context/compare")
        ? { anchor: "AAPL", period: "1M", retrieved_at: "2026-10-20T12:00:00Z", comparisons: [] }
        : { items: [] };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    });
    await page.goto("/equity/market-context?symbol=AAPL&proxies=SPY&period=1M&currency=EUR");
    const generate = page.getByRole("button", { name: "Generate briefing", exact: true });
    await expect(generate).toBeVisible(); expect(requests).toBe(0);
    await generate.click();
    const observation = page.getByText(makeBriefing().observations[0].text, { exact: true });
    await expect(observation).toBeVisible();
    await expect(page.getByLabel("Mandatory evidence caveats")).toBeVisible();
    await page.getByRole("button", { name: "Inspect P1 for observed movement 1" }).click();
    const pair = page.getByRole("region", { name: "P1 evidence", exact: true });
    await expect(pair).toBeFocused(); await expect(pair).toContainText("1.2345%");
    await expect(pair).toContainText("2026-09-20 to 2026-10-20");
    await page.screenshot({ path: testInfo.outputPath("briefing-evidence.png"), fullPage: true });
    await page.getByRole("button", { name: "Generate new briefing" }).click();
    await expect(observation).toHaveCount(0);
    await expect(page.getByRole("alert").filter({ hasText: "The model could not be reached" })).toBeVisible();
    await page.getByText(/Inspect briefing evidence snapshot/).click();
    await expect(page.getByText(`Snapshot ID: ${"b".repeat(64)}`, { exact: true })).toBeVisible();
    await page.getByLabel("Window", { exact: true }).selectOption("3M");
    await expect(page.getByRole("button", { name: "Generate briefing", exact: true })).toBeDisabled();
    await expect(page.getByText(/Snapshot ID:/)).toHaveCount(0);
    expect(requests).toBe(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
