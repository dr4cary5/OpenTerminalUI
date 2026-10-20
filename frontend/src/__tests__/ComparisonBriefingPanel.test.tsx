import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { streamComparisonBriefing } from "../api/comparisonBriefing";
import { ComparisonBriefingPanel } from "../components/market/ComparisonBriefingPanel";
import { briefingSelection, makeBriefing } from "./fixtures/comparisonBriefing";

vi.mock("../api/comparisonBriefing", async (original) => ({
  ...await original<typeof import("../api/comparisonBriefing")>(), streamComparisonBriefing: vi.fn(),
}));
const generate = vi.mocked(streamComparisonBriefing);
function pending() {
  let resolve!: (value: ReturnType<typeof makeBriefing>) => void;
  const promise = new Promise<ReturnType<typeof makeBriefing>>((done) => { resolve = done; });
  return { promise, resolve };
}
const clickGenerate = () => fireEvent.click(screen.getByRole("button", { name: "Generate briefing" }));

describe("ComparisonBriefingPanel", () => {
  beforeEach(() => { generate.mockReset(); });

  it("is opt-in and keeps scope and mandatory evidence warnings beside plain-text prose", async () => {
    const value = makeBriefing();
    value.observations[0].text = '<img src=x onerror=alert(1)> [untrusted](https://example.com)';
    generate.mockResolvedValue(value);
    const { container } = render(<ComparisonBriefingPanel selection={briefingSelection} />);
    expect(generate).not.toHaveBeenCalled();
    expect(screen.getByText(/not included—even if open/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /save|index/i })).not.toBeInTheDocument();
    clickGenerate();
    expect(await screen.findByText(value.observations[0].text)).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByLabelText("Mandatory evidence caveats")).toBeVisible();
    expect(container.querySelector("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "Inspect P1 for observed movement 1" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "P1 evidence" })).toHaveFocus());
    const evidence = screen.getByRole("region", { name: "P1 evidence" });
    expect(evidence).toBeVisible();
    expect(within(evidence).getByText(/1\.2345%/)).toBeVisible();
    expect(within(evidence).getByText(/22 shared UTC closes/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Inspect SCOPE for unknowns 1" }));
    expect(screen.getByRole("region", { name: "SCOPE evidence" })).toHaveFocus();
    expect(screen.getByText(`Snapshot ID: ${value.evidence!.packet_id}`)).toBeVisible();
    expect(generate).toHaveBeenCalledOnce();
  });

  it("shows progress, cancels, and ignores late results and progress from the cancelled run", async () => {
    const old = pending(); generate.mockReturnValueOnce(old.promise).mockResolvedValueOnce(makeBriefing());
    render(<ComparisonBriefingPanel selection={briefingSelection} />);
    clickGenerate();
    const options = generate.mock.calls[0][1]!;
    act(() => options.onProgress!("retrying"));
    expect(screen.getByRole("status")).toHaveTextContent("Repairing the model response once");
    fireEvent.click(screen.getByRole("button", { name: "Cancel briefing" }));
    expect(options.signal!.aborted).toBe(true);
    expect(screen.getByRole("status")).toHaveTextContent("Briefing cancelled");
    clickGenerate();
    await screen.findByText(makeBriefing().observations[0].text);
    const late = makeBriefing(); late.observations[0].text = "Cancelled answer must never be displayed.";
    await act(async () => { options.onProgress!("generating"); old.resolve(late); });
    expect(screen.queryByText(late.observations[0].text)).not.toBeInTheDocument();
    expect(screen.getByText(makeBriefing().observations[0].text)).toBeVisible();
  });

  it("clears the previous answer before a retry and retains only the failed run's evidence", async () => {
    const failed = makeBriefing(); failed.status = "unavailable"; failed.engine = "unavailable";
    failed.observations = []; failed.limitations = []; failed.unknowns = [];
    failed.failure = { code: "provider_unavailable", message: "The model is unavailable.", retryable: true };
    failed.evidence!.packet_id = "b".repeat(64);
    const next = pending(); generate.mockResolvedValueOnce(makeBriefing()).mockReturnValueOnce(next.promise);
    render(<ComparisonBriefingPanel selection={briefingSelection} />);
    clickGenerate(); await screen.findByText(makeBriefing().observations[0].text);
    fireEvent.click(screen.getByRole("button", { name: "Generate new briefing" }));
    expect(screen.queryByText(makeBriefing().observations[0].text)).not.toBeInTheDocument();
    expect(screen.queryByText(/Snapshot ID/)).not.toBeInTheDocument();
    await act(async () => next.resolve(failed));
    expect(screen.getByRole("alert")).toHaveTextContent("The model is unavailable");
    expect(screen.queryByRole("region", { name: "Observed movement" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/Inspect briefing evidence snapshot/));
    expect(screen.getByText(`Snapshot ID: ${"b".repeat(64)}`)).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry briefing" })).toBeEnabled();
  });

  it("handles transport failure without a partial answer or automatic retry", async () => {
    generate.mockRejectedValue(new Error("The briefing stream ended before completion."));
    render(<ComparisonBriefingPanel selection={briefingSelection} />); clickGenerate();
    expect(await screen.findByRole("alert")).toHaveTextContent("ended before completion");
    expect(screen.queryByText(/Inspect briefing evidence/)).not.toBeInTheDocument();
    expect(generate).toHaveBeenCalledOnce();
  });

  it("keeps stale, mixed, unknown-unit and withheld-close caveats visible with unavailable pairs", async () => {
    const value = makeBriefing(); const pair = value.evidence!.comparisons[0];
    pair.returns!.freshness = "stale"; pair.pair_reported_basis_status = "mixed_reported";
    pair.anchor.withheld_provider_close_count = 2;
    pair.caveats.push("stale_history", "mixed_reported_price_basis", "unknown_quote_unit", "withheld_conflicting_closes");
    value.evidence!.status = "partial";
    value.evidence!.comparisons.push({ ...pair, citation_id: "P2", status: "unavailable",
      comparison: { ...pair.comparison, symbol: "QQQ" }, returns: null, reason: "missing_history",
      caveats: ["native_quote_not_fx_normalized", "utc_dates_not_simultaneous_sessions", "no_causal_or_investment_conclusion", "comparison_unavailable"],
    });
    value.unknowns.push({ text: "No QQQ comparison return was calculated.", citation_ids: ["P2"] });
    generate.mockResolvedValue(value);
    render(<ComparisonBriefingPanel selection={{ ...briefingSelection, comparisons: ["SPY", "QQQ"] }} />);
    clickGenerate();
    const warnings = await screen.findByLabelText("Mandatory evidence caveats");
    expect(warnings).toBeVisible();
    expect(warnings).toHaveTextContent("History is stale");
    expect(warnings).toHaveTextContent("Reported price bases differ");
    expect(warnings).toHaveTextContent("unknown quote unit");
    expect(warnings).toHaveTextContent("Conflicting closes were withheld");
    expect(warnings).toHaveTextContent("not a zero return");
    fireEvent.click(screen.getByRole("button", { name: "Inspect P2 for unknowns 2" }));
    expect(screen.getByRole("region", { name: "P2 evidence" })).toHaveTextContent("Return unavailable: missing history. No zero return is implied.");
  });

  it("handles evidence preparation failure without assuming a snapshot exists", async () => {
    const value = makeBriefing(); value.status = "unavailable"; value.engine = "unavailable";
    value.evidence = null; value.observations = []; value.limitations = []; value.unknowns = [];
    value.failure = { code: "evidence_timed_out", message: "Evidence preparation timed out.", retryable: true };
    generate.mockResolvedValue(value);
    render(<ComparisonBriefingPanel selection={briefingSelection} />); clickGenerate();
    expect(await screen.findByRole("alert")).toHaveTextContent("Evidence preparation timed out");
    expect(screen.queryByText(/Inspect briefing evidence/)).not.toBeInTheDocument();
  });

  it("invalidates and aborts when selection changes or the panel unmounts", async () => {
    const old = pending(); generate.mockReturnValue(old.promise);
    const { rerender, unmount } = render(<ComparisonBriefingPanel selection={briefingSelection} />);
    clickGenerate(); const oldSignal = generate.mock.calls[0][1]!.signal!;
    rerender(<ComparisonBriefingPanel selection={{ ...briefingSelection, anchor: "MSFT" }} />);
    expect(oldSignal.aborted).toBe(true);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(async () => old.resolve(makeBriefing()));
    expect(screen.queryByText(makeBriefing().observations[0].text)).not.toBeInTheDocument();
    generate.mockReturnValue(pending().promise); clickGenerate();
    const signal = generate.mock.calls[1][1]!.signal!; unmount();
    expect(signal.aborted).toBe(true);
  });

  it("disables generation while the selection is unapplied", () => {
    render(<ComparisonBriefingPanel selection={briefingSelection} disabledReason="Apply your selection first." />);
    expect(screen.getByRole("button", { name: "Generate briefing" })).toBeDisabled();
    expect(screen.getByText("Apply your selection first.")).toBeVisible();
    expect(generate).not.toHaveBeenCalled();
  });
});
