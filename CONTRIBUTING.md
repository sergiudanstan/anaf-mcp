# Contributing

Contributions are welcome. This is a single-maintainer project, so please open an issue
before starting anything substantial.

**Security problems do not belong in issues.** Follow [SECURITY.md](SECURITY.md).

## Branches, pull requests and releases

- `main` is the only long-lived branch and is protected: changes land through a pull
  request with the required checks green, commits on `main` are signed, and force-pushes
  are refused. Pull requests are squash-merged; GitHub deletes the branch on merge.
- Branch names: `<type>/<topic>` (`feat/`, `fix/`, `docs/`, `chore/`, `release/`);
  agents use `agent/<name>/<topic>`.
- A release is a pull request that bumps `SERVER_VERSION` in `anaf_mcp.py` and moves
  `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) to a dated version. After it merges, a
  signed `vX.Y.Z` tag on that commit triggers `release.yml`, which refuses to run if the
  tag, the declared version and the changelog disagree. Tags are never moved or deleted.
- The GitHub releases `latest` and `data-YYYY-MM` hold the prebuilt data index and are managed by
  `build-index.yml` (or by hand, see the README). Code releases use `v*` tags only, are created
  with `--latest=false`, and never touch those two.

## Running it locally

```bash
python3 -m unittest discover -s tests -v    # standard library only; no network needed
python3 anaf_mcp.py --test                  # live check against BNR (needs network)
python3 anaf_mcp.py                         # run the server over stdio
python3 remote_mcp.py --port 8765           # HTTP endpoint at http://127.0.0.1:8765/mcp
```

No dependencies to install. The tests never reach the network.
