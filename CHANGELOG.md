# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.0] - 2026-11-25

`anaf-mcp` is a read-only MCP server for Romanian public data, written with the Python standard
library only: ANAF company, VAT and balance-sheet data by CUI, BNR exchange rates, the CAEN
nomenclature, and company search and rankings over a prebuilt Trade Register / financial-statement
index. It runs locally over stdio or as a stateless Streamable HTTP endpoint (Vercel). The server
already declared 1.3.0; this is the first versioned release (the existing `latest` GitHub release
is the data index, not the code).

### Added
- Offline test suite on top of the existing protocol tests: input normalization, CAEN lookup, BNR
  parsing and conversion, `top_firme` / `search_company` over a synthetic index (including SQL
  metacharacters in the input), the compact remote index and its decompression size limit, the
  HTTP transport on a loopback port (health, batch, SSE, malformed and oversized requests), and the
  stdio server as a subprocess.
- CI (tests on Python 3.10 and 3.13, ruff, `pip-audit` on the CI toolchain, `zizmor`), CodeQL,
  OpenSSF Scorecard and a tag-triggered release workflow (`v*` tags, version read from
  `SERVER_VERSION`) with build provenance; Dependabot for actions.
- `SECURITY.md`, `CONTRIBUTING.md` and this changelog.

### Changed
- `SERVER_VERSION` is now 1.4.0 and is the single version source for releases.
- `build-index.yml` (manual only) hardened without changing what it does: actions pinned to commit
  SHAs, `persist-credentials: false`, read-only default permissions, and the `ani` input passed
  through an environment variable instead of being expanded inside the shell script.

[Unreleased]: https://github.com/sergiudanstan/anaf-mcp/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/sergiudanstan/anaf-mcp/releases/tag/v1.4.0
