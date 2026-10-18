# OpenTerminalUI Wiki

OpenTerminalUI is a self-hosted research terminal for US/EU equities and crypto,
with intentional provider-gated NSE/BSE F&O support. The current release is
[v2.1.0](https://github.com/laanito/OpenTerminalUI/releases/tag/v2.1.0), published
2026-10-18: selected US fundamentals can be checked against official SEC filing
evidence. No database migration is required for this release.

> **North star.** An open, private terminal that helps an individual invest *without being fooled* — by markets, by hype, or by themselves — through AI-native research you can grow privately. Bloomberg parity is pursued only "just enough to be credible"; the differentiation is being AI-native, private, open, and multi-asset. See the [Roadmap](Roadmap) for the full direction.

## Current Product Focus

- Terminal Noir shell (dark-first, dense UI, keyboard-first)
- GO Command Bar + ticker tape + market status bar
- Launchpad multi-panel workspace
- Security Hub (DES-style) for ticker-centric research
- DenseTable component for high-throughput market tables
- Chart workstation with crosshair sync, overlays, and volume profile
- Portfolio manager + multi-market screener + alerts/news sentiment
- Crypto fundamentals: tokenomics, on-chain TVL & fees, valuation ratios (CoinGecko + DefiLlama)
- Local, provider-agnostic LLM (Ollama default) for AI insights, news sentiment & emotion
- User-selected cross-market comparisons with dated source, currency and coverage disclosures
- On-demand SEC source-filing checks for exact selected US facts
- Time-aware private Notes/Second Brain and saved Brain Log research memos
- Base-currency portfolio accounting with explicit partial results when FX is missing

## Quick Links

- [Getting Started](Getting-Started)
- [Features](Features)
- [Research Workflows](Research-Workflows) — comparisons/SEC checks, dated notes, saved answers and external ingestion
- [Architecture](Architecture)
- [Data Providers](Data-Providers)
- [Limitations](Limitations) — what works keyless vs. needs keys, and honest caveats
- [Contributing](Contributing)
- [Roadmap](Roadmap) — released v2.1 scope and later, unscheduled generation work
- [Releasing](Releasing) — how we cut a tagged release

## Smoke Routes

- `/equity/stocks`
- `/equity/security/:ticker`
- `/equity/launchpad`
- `/equity/chart-workstation`
- `/equity/compare`
- `/equity/market-context` (contextual comparison, distinct from chart Compare)
- `/equity/notes`
- `/equity/brain` (Second Brain and Brain Log)
- `/equity/journal`
- `/equity/screener`
- `/equity/portfolio?view=manager`
