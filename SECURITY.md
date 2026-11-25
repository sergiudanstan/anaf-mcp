# Security Policy

`anaf-mcp` is a **read-only** MCP server for Romanian public data (ANAF, BNR, the CAEN
nomenclature, and a prebuilt Trade Register / financial-statement index). It uses only the Python
standard library. It runs locally over stdio, or as a stateless Streamable HTTP endpoint
(`remote_mcp.py`, deployed on Vercel through `api/index.py`) that is **public and has no
authentication by design**: it serves only public data.

## Supported versions

| Version | Supported |
|---|---|
| 1.4.x | Current |

Single maintainer; only the latest release receives fixes.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately:
**[Report a vulnerability](https://github.com/sergiudanstan/anaf-mcp/security/advisories/new)**
(Security -> Advisories -> Report a vulnerability).

| Stage | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Triage | within 10 business days |
| Fix, or a documented decision not to fix | within 90 days |

These are best-effort targets from a single unpaid maintainer, not a contractual SLA.
There is no bug bounty; credit is offered in the advisory and changelog.

## Threat model

Tool arguments are untrusted: they are chosen by an LLM that may itself be steered by
prompt injection in content it reads. What the tools do with them:

- **No code execution, no file writes chosen by the caller.** There is no subprocess, `eval`
  or file path taken from a tool argument. The only files written are the on-disk response
  cache (`~/.cache/anaf-mcp/`, or `ANAF_MCP_CACHE_DIR`; file names are a SHA-256 of the cache
  key) and, for `--sync`, the downloaded index in that directory.
- **Input handling.** CUIs are normalised to integers, dates are parsed strictly, the financial
  year is converted to an integer before it is placed in a URL, `anaf_firme` accepts at most
  100 CUIs, result limits are clamped (`limita` at most 500, never SQLite's "no limit"), and the
  index is queried with bound parameters (the only interpolated SQL is a column name chosen from
  a fixed allow-list). The tests cover malformed CUIs and dates, the clamping, and a
  SQL-metacharacter search string.
- **Network.** Outgoing requests go to fixed hosts: `webservicesp.anaf.ro`, `curs.bnr.ro`, and,
  only for the manual `--sync` and `--sync-onrc` commands, GitHub Releases of this repository
  and `data.gov.ro`. Tool arguments become values in the request body or query string, never the
  host. Known limits: the downloaded index (`--sync`) is not checked against a published
  checksum (only against `Content-Length`) and is decompressed without a size cap on the local
  path; the CSV URL used by `--sync-onrc` comes from the `data.gov.ro` API response and its
  scheme is not restricted. Both are manual CLI commands, not reachable from a tool call.
- **Public HTTP endpoint.** `/mcp` accepts any caller (CORS `*`, no token). It rejects bodies
  over 1 MiB, but has no per-client rate limit; ANAF calls are serialised at one per second per
  instance, so a flood of lookups can exhaust that budget or get the deployment's address
  rate-limited by ANAF. Responses carry `nosniff` and `no-store`.
- **Third-party content.** Everything returned comes from ANAF, BNR, ONRC or data.gov.ro and is
  passed to the model as text. Treat company names and similar fields as untrusted data, not
  instructions.

## Scope

In scope:

- Any way for a tool argument to reach the file system, a subprocess, SQL, or a request host or
  path other than the documented ones.
- Memory or CPU exhaustion from a single small request to the HTTP endpoint (beyond the
  documented lack of rate limiting).
- Anything in this repository's workflows that could publish a release or the data index not
  built from the documented process.

Out of scope:

- Lack of authentication or rate limiting on the public endpoint (documented; the data is
  public). Deploy your own copy if you need a quota.
- The accuracy, freshness or availability of ANAF, BNR or data.gov.ro data.
- The two manual-download limits listed above, unless you can trigger them from a tool call.
- Vulnerabilities in the Vercel platform or the client (report upstream).

## Supply chain

The project has no third-party dependencies. CI runs the test suite on Python 3.10 and 3.13 (all
offline), `ruff` (syntax and undefined-name checks only), `pip-audit` on the hash-pinned CI
toolchain, CodeQL, and `zizmor` on the workflows. Actions are pinned to full commit SHAs and
kept current by Dependabot. Releases are triggered by `v*` tags and attach a source archive with
Sigstore build provenance:

```
gh attestation verify anaf-mcp-v1.4.0.tar.gz --repo sergiudanstan/anaf-mcp
```

The prebuilt data index is published separately as the GitHub releases `latest` and
`data-YYYY-MM` by `build-index.yml` (manual) or by hand; those assets are not covered by the
provenance attestation.
