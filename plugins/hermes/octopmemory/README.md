# OctopMemory for Hermes

Hermes `MemoryProvider` backed by the Python `octop-memory` runtime. Requires Hermes v0.10.0+
and Python 3.12+ with SQLite FTS5.

Install it as a native plugin straight from this monorepo — the Hermes installer accepts
`owner/repo/path/to/plugin` subdirectory specs, so no separate plugin repository is needed:

```bash
hermes plugins install TencentCloud/octop-memory/plugins/hermes/octopmemory \
  --ref <full-40-character-commit-sha>
hermes memory setup     # select octopmemory; the host installs pip_dependencies from plugin.yaml
hermes gateway restart
hermes memory status
```

`--ref` requires the full 40-character commit SHA (short SHAs are rejected); the pinned revision is
recorded in `~/.hermes/plugins/.install-metadata.json`. This artifact does not contain
`octopmemory.installer`, and the `octop-memory-hermes` installer package is not published to PyPI.

Installation, configuration, troubleshooting and adapter development share one
[OpenClaw / Hermes guide](https://github.com/TencentCloud/octop-memory/blob/main/docs/integrations.md#hermes).
