import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { extractApiErrorMessage } from "../../api/base";
import { fetchSecFilingFactCheck, type SecFiledFact, type SecFilingFactCheckResponse } from "../../api/marketContext";

const OUTCOMES: Record<SecFilingFactCheckResponse["status"], { title: string; message: string }> = {
  corroborated: { title: "Value corroborated in filing", message: "The filing reports this value for the same company, concept, unit and exact fiscal period." },
  mismatched: { title: "Filing evidence differs", message: "The filing evidence differs from the selected Company Facts claim." },
  ambiguous: { title: "Filing check is ambiguous", message: "The filing evidence could not be resolved to one supported value." },
  missing: { title: "Exact fact not found", message: "No matching company-wide fact was found in the checked data. This does not establish that no disclosure was made." },
  unsupported: { title: "Filing outside check coverage", message: "This filing cannot be inspected within the current coverage and limits." },
  unparseable: { title: "Filing data could not be read", message: "The source data could not be interpreted reliably. The selected value remains unverified." },
  configuration_required: { title: "SEC filing checks are off", message: "The host must set SEC_USER_AGENT before a filing can be checked." },
  not_covered: { title: "SEC ticker coverage unavailable", message: "No exact company match was found for this ticker. This does not prove that the issuer has no filings." },
  ambiguous_ticker: { title: "Company identity is ambiguous", message: "Multiple SEC company identifiers match this ticker; no filing was selected." },
  provider_error: { title: "Filing check failed", message: "SEC filing data could not be retrieved. Retry the check." },
};

const REASONS: Partial<Record<NonNullable<SecFilingFactCheckResponse["reason"]>, string>> = {
  unsupported_concept_unit_or_form: "This concept, unit or filing form is outside the current check coverage.",
  claim_not_in_current_company_facts: "The selected fact is no longer present in current Company Facts. Refresh SEC facts and choose again.",
  invalid_filing_index: "The SEC filing index could not be read reliably. The selected value remains unverified.",
  filing_metadata_ambiguous: "The filing's form or filed date could not be resolved reliably.",
  filing_metadata_differs: "The filing's form or filed date differs from the selected Company Facts claim.",
  unsupported_instance_link: "No supported source document link was available for this filing.",
  instance_not_listed: "No supported XBRL instance is listed for this filing.",
  multiple_instances: "The filing lists multiple XBRL instances; no source document was selected.",
  submission_metadata_conflicts: "The SEC submission metadata conflicts with this filing claim.",
  conflicting_exact_facts: "The filing contains conflicting values for the same exact company, concept, unit and fiscal period.",
  exact_value_differs: "The filing reports a different value for this exact concept, unit and fiscal period.",
  document_size_limit: "The filing exceeds the supported download limit. The selected value remains unverified.",
  check_timed_out: "The SEC check reached its time limit. Retry when the provider responds.",
};

function formatValue(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 21 }).format(value);
}

export function SecFilingFactCheck({ symbol, fact }: { symbol: string; fact: SecFiledFact }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const identity = `${symbol} ${fact.concept} ${fact.accession}, ${fact.period_start} to ${fact.period_end}, ${fact.value} ${fact.unit}`;
  const query = useQuery({
    queryKey: ["market-sec-filing-fact-check", symbol, fact],
    queryFn: ({ signal }) => fetchSecFilingFactCheck(symbol, fact, signal),
    enabled: open,
    staleTime: Infinity,
    retry: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const result = query.fetchStatus === "idle" && !query.isError ? query.data : undefined;
  const outcome = result ? OUTCOMES[result.status] : undefined;
  const retry = query.isError || result?.status === "provider_error";

  return (
    <div className="mt-2">
      <button type="button" className="text-terminal-accent underline" aria-expanded={open} aria-controls={panelId}
        aria-label={`${open ? "Hide" : "Check"} source filing for ${identity}`} onClick={() => setOpen((value) => !value)}>
        {open ? "Hide source check" : "Check source filing"}
      </button>
      {open ? (
        <div id={panelId} className="mt-2 space-y-2 rounded border border-terminal-border bg-terminal-bg p-2">
          {query.fetchStatus === "paused" ? <p role="status" className="text-terminal-muted">Waiting for a connection to check the source filing…</p>
            : query.isFetching ? <p role="status" className="text-terminal-muted">Checking the source filing for {symbol}… This may take up to a minute.</p> : null}
          {query.isError ? (
            <p role="alert" className="text-terminal-warn">
              {extractApiErrorMessage(query.error, "Could not check the source filing.")} The selected Company Facts value remains unverified.
            </p>
          ) : null}
          {result && outcome ? (
            <>
              <p role="status" className={result.status === "corroborated" ? "font-medium text-terminal-pos" : "font-medium text-terminal-warn"}>{outcome.title}</p>
              <p className="text-terminal-muted">{(result.reason && REASONS[result.reason]) || outcome.message}</p>
              <dl className="space-y-1 text-terminal-text">
                {result.cik !== null ? <div><dt className="inline font-medium">Company: </dt><dd className="inline">{symbol} · SEC CIK {result.cik}</dd></div> : null}
                <div><dt className="inline font-medium">Selected value: </dt><dd className="inline">{formatValue(fact.value)} {fact.unit} · {fact.taxonomy}:{fact.concept}</dd></div>
                <div><dt className="inline font-medium">Fiscal period: </dt><dd className="inline">{fact.period_start} to {fact.period_end}</dd></div>
                <div><dt className="inline font-medium">Selected filing: </dt><dd className="inline">{fact.form} · filed {fact.filed_date} · {fact.accession}</dd></div>
                {result.filing_form || result.filing_date ? <div><dt className="inline font-medium">Filing index reports: </dt><dd className="inline">{result.filing_form ?? "Form unknown"} · filed {result.filing_date ?? "date unknown"}</dd></div> : null}
                {result.observed_values.length > 0 ? (
                  <div><dt className="inline font-medium">Values in filing: </dt><dd className="inline">{result.observed_values.map(formatValue).join("; ")} {fact.unit} · {result.matching_fact_count} matching fact{result.matching_fact_count === 1 ? "" : "s"}, {result.distinct_value_count} distinct value{result.distinct_value_count === 1 ? "" : "s"}{result.distinct_value_count > result.observed_values.length ? ` (showing ${result.observed_values.length})` : ""}</dd></div>
                ) : null}
                <div><dt className="inline font-medium">SEC-reported acceptance: </dt><dd className="inline">{result.accepted_at ?? "Unknown in this check"}</dd></div>
              </dl>
              <p className="flex flex-wrap gap-x-3 gap-y-1">
                {result.filing_url ? <a className="text-terminal-accent underline" href={result.filing_url} target="_blank" rel="noopener noreferrer">Open SEC filing</a> : null}
                {result.document_url ? <a className="text-terminal-accent underline" href={result.document_url} target="_blank" rel="noopener noreferrer">Open XBRL source</a> : null}
              </p>
              <p className="text-terminal-muted">Checked {new Date(result.retrieved_at).toLocaleString()}. This check does not establish revision order or what investors knew at a past trading time.</p>
            </>
          ) : null}
          {!query.isPending ? (
            <button type="button" className="text-terminal-accent underline disabled:opacity-50" disabled={query.fetchStatus !== "idle"}
              aria-label={`${retry ? "Retry source check" : "Check source again"} for ${identity}`} onClick={() => void query.refetch()}>
              {retry ? "Retry source check" : query.fetchStatus !== "idle" ? "Checking…" : "Check again"}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
