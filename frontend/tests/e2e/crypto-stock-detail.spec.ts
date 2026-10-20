import { expect, test } from "@playwright/test";

test("@smoke crypto stock detail keeps chart and snapshot on the crypto feed", async ({ page }) => {
  const subscribed: string[] = [];
  const snapshotMarkets: string[] = [];
  await page.addInitScript(() => {
    localStorage.setItem("stock-state", JSON.stringify({
      state: { ticker: "BTC-USD", interval: "1d", range: "1y" }, version: 0,
    }));
  });
  await page.routeWebSocket(/\/api\/ws\//, (socket) => {
    socket.onMessage((message) => {
      const payload = JSON.parse(String(message));
      if (payload.op !== "subscribe") return;
      for (const token of payload.symbols ?? []) {
        subscribed.push(token);
        if (!token.endsWith(":BTC-USD")) continue;
        const valid = token === "CRYPTO:BTC-USD";
        socket.send(JSON.stringify({ type: "tick", symbol: token,
          ltp: valid ? 82521.45 : 0, change: valid ? 535.14 : 0,
          change_pct: valid ? 0.65 : 0, ts: new Date().toISOString(),
        }));
      }
    });
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [] };
    if (url.pathname.endsWith("/quotes") && url.searchParams.get("symbols")?.includes("BTC-USD")) {
      snapshotMarkets.push(url.searchParams.get("market")!);
      data = { market: "CRYPTO", quotes: [{ symbol: "BTC-USD", last: 82500, change: 500, changePct: 0.6 }] };
    } else if (url.pathname.endsWith("/crypto/candles")) {
      data = { ticker: "BTC-USD", currency: "USD", interval: "1d", data: [
        { t: 1792432800, o: 81000, h: 83000, l: 80000, c: 82000, v: 10 },
        { t: 1792519200, o: 82000, h: 83000, l: 81000, c: 82500, v: 12 },
      ] };
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
  });
  await page.goto("/equity/stocks");
  const latest = page.getByText("Latest Price", { exact: true }).locator("xpath=ancestor::section[1]");
  await expect(latest).toContainText("82,521.45");
  await expect(latest).toContainText("+535.14");
  await expect(latest).toContainText("+0.65%");
  await expect(latest).not.toContainText("$0.00");
  await expect.poll(() => snapshotMarkets).toContain("CRYPTO");
  expect(snapshotMarkets).not.toContain("NASDAQ");
  expect(subscribed).toContain("CRYPTO:BTC-USD");
  expect(subscribed).not.toContain("NASDAQ:BTC-USD");
});
