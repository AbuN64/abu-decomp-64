# Build profile section

`decomp build once [--prove-only]` reads the profile `build` section, which also names
`project_root`, `runtime_root` and `output_root`. Relative paths resolve against the profile file; mutable files remain under `workspace`
(default `runtime_root/build`). Gate working directories must be private.

Required tables:

- `sources`: `[{name, repo, ref, protected_top?}]`. Each ref is pinned once,
  archived into `workspace/sources/<name>`, and checked again before release.
  `protected_top` excludes generated roots such as `build` from archive sync.
  A per-run manifest at `${source.<name>.manifest}` records schema 1, the full
  commit and tree, archive SHA-256, and every committed file SHA-256/mode. Gates
  and promotions fail if that manifest or any archived source file changes, is
  deleted, becomes a symlink, or gains an unowned neighbor outside protected roots.
- `commands`: ordered `[{name, argv, cwd?, env?, expected_exit_codes?,
  expect_all?, forbid?, timeout_seconds?}]`. `argv` is a real argument array.
  Default exit contract is `[0]`; public fatal gates can explicitly require `[1]`.
- `outputs`: `[{name, path, executable?}]`. Each built path must reside inside
  the private workspace and is hash-frozen before release.
- `releases`: `[{name, root, revision, files, activate?, rollback?}]`. A role
  root contains immutable `<revision>/` directories and a sibling `current`
  symlink. `files` is `[{source, target, mode?}]`; source names are `input.<name>`
  or `output.<name>`. Every role uses the same staging, manifest, current-switch,
  readback, and rollback code. `activate` and `rollback` are ordered gate objects
  with user-owned `argv` arrays, usually service restarts.

Optional tables:

- `workspace`: private stable runtime path. The lock, snapshots, build cache,
  run inputs, logs, receipts, and status live here.
- `inputs`: `[{name, path, sha256?, filename?, metadata?}]`. Each input is copied
  into its own per-run directory and hashed. `metadata` is preserved in receipts.
  A configured hash pins canonical assets; later path/hash changes reject a job.
- `checks`: `[{name, kind, when?, ...}]`. Kinds are `file_sha256` (`path`,
  `sha256`), `git_archive` (`repo`, `commit`, `sha256`), `file_contains`
  (`path`, `text`), and `path_absent` (`path`). `when` defaults to `pre,post`;
  `after:<gate-name>` verifies a generated cache immediately after a gate.

String values may contain only known `${...}` variables: `workspace`, `run_dir`,
`runtime_root`, `output_root`, `project_root`, `source.<name>`,
`commit.<name>.full`, `commit.<name>.short7`, `tree.<name>`, `input.<name>`,
`source.<name>.manifest`, `source.<name>.manifest_sha256`,
`input.<name>.dir`, `input.<name>.sha256`, `output.<name>`, and
`output.<name>.sha256`. Release activation also gets `release`,
`release_current`, and `release_revision`. A role revision should bind every
relevant source commit, for example
`${commit.engine.short7}-${commit.adapter.short7}`.

`decomp build status` reads the atomic status/latest receipts. A build invocation
emits one `build/started` and one terminal `build/passed|failed|blocked` event
through the single `logs.events_path` sink when that section is configured.
Failed or blocked `decomp build once` exits nonzero with a named finding and an
immutable receipt path. Source ref changes during gates are coalesced into the
latest committed attempt; no writer worktree or timer is polled.
The receipt records the immutable loaded `Profile.digest`, and a changed profile
file is rejected before any build work and again before promotion.
