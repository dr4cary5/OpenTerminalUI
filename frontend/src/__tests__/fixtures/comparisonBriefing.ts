import type { BriefingAsset, ComparisonBriefingResult } from "../../api/comparisonBriefing";

const asset = (symbol: string): BriefingAsset => ({
  symbol, history_adapter: "yahoo", history_feed: "yahoo_chart", latest_date: "2026-10-20",
  quote_unit: "USD", quote_unit_source: "yahoo_chart_meta", reported_adjustment_basis: "unspecified",
  withheld_provider_close_count: 0, withheld_date_scope: "selected_history_payload",
});

export function makeBriefing(): ComparisonBriefingResult {
  return {
    status: "complete", engine: "llm", model: "test-local-model", generated_at: "2026-10-20T12:01:00Z",
    scope_citation_id: "SCOPE", citation_validation: "references_only_not_fact_checked",
    scope_notice: "References are checked, not the truth of the model prose. Only shared-UTC native returns were included.",
    failure: null,
    observations: [{ text: "The selected paths had different native-quote returns over the shared window.", citation_ids: ["P1"] }],
    limitations: [{ text: "The selected price adjustment basis remains unverified.", citation_ids: ["P1"] }],
    unknowns: [{ text: "News and macro observations were not included, not established absent.", citation_ids: ["SCOPE"] }],
    evidence: {
      schema_version: "1", citation_scope: "packet_local", packet_id: "a".repeat(64), status: "available",
      anchor: "AAPL", period: "1M", retrieved_at: "2026-10-20T12:00:00Z", data_source: "unified_history",
      method: "same_utc_date_daily_closes", return_basis: "native_quote_currency_provider_closes",
      included_evidence: "shared_utc_provider_close_returns",
      not_included: ["headlines", "fundamentals", "sec_filing_checks", "macro", "technical_measures",
        "fx_views", "adjusted_views", "exchange_local_views", "private_research"],
      comparisons: [{
        citation_id: "P1", status: "available", reason: null, anchor: asset("AAPL"), comparison: asset("SPY"),
        pair_reported_basis_status: "unverified",
        returns: {
          start_date: "2026-09-20", end_date: "2026-10-20", observations: 22, freshness: "current",
          anchor_return_pct: 1.2345, comparison_return_pct: 2, relative_return_pp: -0.7655,
        },
        caveats: ["native_quote_not_fx_normalized", "utc_dates_not_simultaneous_sessions", "no_causal_or_investment_conclusion", "unverified_price_basis"],
      }],
    },
  };
}

export const briefingSelection = { anchor: "AAPL", comparisons: ["SPY"], period: "1M" as const };
