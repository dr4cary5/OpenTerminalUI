import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchSecFilingFactCheck, type SecFiledFact, type SecFilingFactCheckResponse } from "../api/marketContext";
import { SecFilingFactCheck } from "../components/market/SecFilingFactCheck";

vi.mock("../api/marketContext", () => ({ fetchSecFilingFactCheck: vi.fn() }));
const checkMock = vi.mocked(fetchSecFilingFactCheck);
const FACT: SecFiledFact = {
  accession: "0000320193-26-000001", taxonomy: "us-gaap", concept: "EarningsPerShareDiluted", unit: "USD/shares",
  form: "10-Q", filed_date: "2026-08-01", period_start: "2026-04-01", period_end: "2026-06-30", value: 1.234567,
};
const SOURCE = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/";

function result(changes: Partial<SecFilingFactCheckResponse> = {}): SecFilingFactCheckResponse {
  return {
    contract_version: 1, symbol: "AAPL", cik: 320193, fact: FACT, retrieved_at: "2026-10-16T12:00:00Z",
    status: "corroborated", reason: "exact_fact_matches", evidence_scope: "filing_xbrl_exact_entity_concept_unit_duration",
    filing_url: SOURCE + "0000320193-26-000001-index.html", document_url: SOURCE + "issuer_htm.xml",
    filing_form: FACT.form, filing_date: FACT.filed_date, accepted_at: "2026-08-01T15:30:00Z",
    observed_values: [FACT.value], matching_fact_count: 1, distinct_value_count: 1, ...changes,
  };
}

function renderCheck() {
  const client = new QueryClient();
  const view = render(<QueryClientProvider client={client}><SecFilingFactCheck symbol="AAPL" fact={FACT} /></QueryClientProvider>);
  return { ...view, client };
}

describe("SEC source filing check", () => {
  beforeEach(() => {
    checkMock.mockReset();
  });

  it("checks only on click, shows exact evidence, and reopens a dated result without refetching", async () => {
    let finish!: (value: SecFilingFactCheckResponse) => void;
    checkMock.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    renderCheck();
    expect(checkMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Check source filing/ }));
    expect(await screen.findByText(/Checking the source filing for AAPL/)).toBeInTheDocument();
    expect(screen.queryByText("Value corroborated in filing")).not.toBeInTheDocument();
    expect(checkMock).toHaveBeenCalledWith("AAPL", FACT, expect.any(AbortSignal));
    await act(async () => finish(result()));
    expect(await screen.findByText("Value corroborated in filing")).toBeInTheDocument();
    expect(screen.getByText(/1.234567 USD\/shares · us-gaap:EarningsPerShareDiluted/)).toBeInTheDocument();
    expect(screen.getByText(/1.234567 USD\/shares · 1 matching fact, 1 distinct value/)).toBeInTheDocument();
    expect(screen.getByText("2026-08-01T15:30:00Z")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open SEC filing" })).toHaveAttribute("href", SOURCE + "0000320193-26-000001-index.html");
    expect(screen.getByRole("link", { name: "Open XBRL source" })).toHaveAttribute("href", SOURCE + "issuer_htm.xml");
    expect(screen.getByRole("link", { name: "Open XBRL source" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/does not establish revision order/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Hide source filing/ }));
    expect(screen.queryByText("Value corroborated in filing")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Check source filing/ }));
    expect(screen.getByText("Value corroborated in filing")).toBeInTheDocument();
    fireEvent.focus(window);
    fireEvent(window, new Event("online"));
    expect(checkMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["mismatched", "exact_value_differs", "Filing evidence differs", "reports a different value"],
    ["mismatched", "filing_metadata_differs", "Filing evidence differs", "form or filed date differs"],
    ["ambiguous", "conflicting_exact_facts", "Filing check is ambiguous", "contains conflicting values"],
    ["ambiguous", "multiple_instances", "Filing check is ambiguous", "lists multiple XBRL instances"],
    ["missing", "exact_fact_absent", "Exact fact not found", "does not establish that no disclosure"],
    ["missing", "claim_not_in_current_company_facts", "Exact fact not found", "Refresh SEC facts and choose again"],
    ["unsupported", "instance_not_listed", "Filing outside check coverage", "No supported XBRL instance"],
    ["unsupported", "document_size_limit", "Filing outside check coverage", "exceeds the supported download limit"],
    ["unparseable", "invalid_xbrl_instance", "Filing data could not be read", "could not be interpreted reliably"],
    ["configuration_required", null, "SEC filing checks are off", "host must set SEC_USER_AGENT"],
    ["not_covered", null, "SEC ticker coverage unavailable", "does not prove that the issuer has no filings"],
    ["ambiguous_ticker", null, "Company identity is ambiguous", "Multiple SEC company identifiers"],
  ] as const)("keeps %s/%s distinct from corroboration", async (status, reason, title, message) => {
    checkMock.mockResolvedValue(result({ status, reason, accepted_at: null, observed_values: [], matching_fact_count: 0,
      distinct_value_count: 0, filing_url: null, document_url: null, filing_form: null, filing_date: null }));
    renderCheck();
    fireEvent.click(screen.getByRole("button", { name: /Check source filing/ }));
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(message))).toBeInTheDocument();
    expect(screen.getByText("Unknown in this check")).toBeInTheDocument();
    expect(screen.queryByText("Value corroborated in filing")).not.toBeInTheDocument();
    expect(screen.queryByText("Values in filing:")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("keeps original, amended, and conflicting claims separate for the same exact period", async () => {
    const amendment: SecFiledFact = { ...FACT, accession: "0000320193-26-000002", form: "10-Q/A", filed_date: "2026-09-01", value: 1.345678 };
    const conflict: SecFiledFact = { ...FACT, value: 1.5 };
    checkMock.mockImplementation(async (symbol, fact) => result({
      symbol, fact, status: fact.value === 1.5 ? "mismatched" : "corroborated",
      reason: fact.value === 1.5 ? "exact_value_differs" : "exact_fact_matches",
      observed_values: [fact.value === 1.5 ? FACT.value : fact.value],
      filing_form: fact.form, filing_date: fact.filed_date,
      filing_url: `https://www.sec.gov/Archives/edgar/data/320193/${fact.accession.replaceAll("-", "")}/${fact.accession}-index.html`,
    }));
    const client = new QueryClient();
    render(<QueryClientProvider client={client}>
      <section aria-label="Original"><SecFilingFactCheck symbol="AAPL" fact={FACT} /></section>
      <section aria-label="Amendment"><SecFilingFactCheck symbol="AAPL" fact={amendment} /></section>
      <section aria-label="Conflict"><SecFilingFactCheck symbol="AAPL" fact={conflict} /></section>
    </QueryClientProvider>);
    for (const name of ["Original", "Amendment", "Conflict"]) {
      const region = within(screen.getByRole("region", { name }));
      fireEvent.click(region.getByRole("button", { name: /Check source filing/ }));
      expect(await region.findByText(name === "Conflict" ? "Filing evidence differs" : "Value corroborated in filing")).toBeInTheDocument();
    }
    expect(checkMock).toHaveBeenCalledTimes(3);
    expect(checkMock).toHaveBeenCalledWith("AAPL", amendment, expect.any(AbortSignal));
    expect(checkMock).toHaveBeenCalledWith("AAPL", conflict, expect.any(AbortSignal));
    expect(within(screen.getByRole("region", { name: "Amendment" })).getByRole("link", { name: "Open SEC filing" })).toHaveAttribute("href", expect.stringContaining(amendment.accession));
  });

  it("offers deliberate retries and removes a previous success if a fresh check fails", async () => {
    checkMock.mockRejectedValueOnce(new Error("Network unavailable")).mockResolvedValueOnce(result()).mockRejectedValueOnce(new Error("Connection lost"));
    renderCheck();
    fireEvent.click(screen.getByRole("button", { name: /Check source filing/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Network unavailable");
    expect(checkMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /Retry source check/ }));
    expect(await screen.findByText("Value corroborated in filing")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Check source again/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
    expect(screen.queryByText("Value corroborated in filing")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("remains unverified");
    expect(checkMock).toHaveBeenCalledTimes(3);
  });

  it("lets a provider timeout be retried without converting it into a missing fact", async () => {
    checkMock.mockResolvedValueOnce(result({ status: "provider_error", reason: "check_timed_out", observed_values: [] })).mockResolvedValueOnce(result());
    renderCheck();
    fireEvent.click(screen.getByRole("button", { name: /Check source filing/ }));
    expect(await screen.findByText("Filing check failed")).toBeInTheDocument();
    expect(screen.getByText(/reached its time limit/)).toBeInTheDocument();
    expect(screen.queryByText("Exact fact not found")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Retry source check/ }));
    expect(await screen.findByText("Value corroborated in filing")).toBeInTheDocument();
  });

  it("cancels the browser request when its fact is unmounted", async () => {
    checkMock.mockImplementation(() => new Promise(() => {}));
    const view = renderCheck();
    fireEvent.click(screen.getByRole("button", { name: /Check source filing/ }));
    await waitFor(() => expect(checkMock).toHaveBeenCalledTimes(1));
    const signal = checkMock.mock.calls[0][2]!;
    expect(signal.aborted).toBe(false);
    view.unmount();
    expect(signal.aborted).toBe(true);
  });
});
