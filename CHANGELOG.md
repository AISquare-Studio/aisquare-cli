# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-29

First release — a portable memory layer for coding agents.

### Added

- **Context store** (local SQLite): `remember` and the `context` group — `add`,
  `list`, `show`, `edit`, `remove`, `search` (FTS5), `promote`, `import`,
  `export`, `preview` — across `user` and `project` pools, with sync-ready
  metadata (soft-delete tombstones, `updated_at`) and time-sortable,
  prefix-addressable ids.
- **`inject` / `why`** — assemble in-scope context for an agent session and
  explain the last injection.
- **Projects** — `init`, the `project` group (`info` / `list` / `switch` /
  `link`), and `onboard`, which packs a Repomix codebase snapshot (full pack +
  skeleton + per-file index).
- **Claude Code integration** — `agents` group with `connect` that installs
  `SessionStart` + `UserPromptSubmit` hooks (injecting context and capturing
  prompts), plus detection (`scan` / `list` / `status`) and `disconnect`.
- **Diagnostics & config** — `status`, `doctor` (dependency + setup health with
  fixes), the `config` group, and `log` (captured prompt history).

[Unreleased]: https://github.com/AISquare-Studio/aisquare-cli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AISquare-Studio/aisquare-cli/releases/tag/v0.1.0
