import { expect, it, vi } from "vitest";

import { api } from "../api/base";
import { fetchSecFilingFactCheck, type SecFiledFact } from "../api/marketContext";

it("sends one exact fact with cancellation and a timeout beyond the backend deadline", async () => {
  const fact: SecFiledFact = {
    accession: "0000320193-26-000001", taxonomy: "us-gaap", concept: "NetIncomeLoss", unit: "USD",
    form: "10-Q", filed_date: "2026-08-01", period_start: "2026-04-01", period_end: "2026-06-30", value: 123,
  };
  const controller = new AbortController();
  const post = vi.spyOn(api, "post").mockResolvedValue({ data: { status: "provider_error", reason: "check_timed_out" } });
  try {
    const result = await fetchSecFilingFactCheck("AAPL", fact, controller.signal);
    expect(post).toHaveBeenCalledWith("/market-context/sec-filing-fact-check", { symbol: "AAPL", fact }, { timeout: 60_000, signal: controller.signal });
    expect(result.status).toBe("provider_error");
  } finally {
    post.mockRestore();
  }
});
