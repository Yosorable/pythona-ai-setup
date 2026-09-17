# Agent guide

Read [README.md](README.md) for setup, architecture, and runtime limits.

## Runtime and providers

- `ai_setup/bundle.py` combines `model.py`, `mlx_dependencies.py`, `mlx_model.py`,
  and `server.py` into one namespace and embeds them in JavaScript. Installed
  providers must work without this repository's files; keep model SDK and
  dependency resolver imports lazy.
- Prepare MLX dependencies on its worker before importing model SDKs. Resolve
  against actual bundled versions and submit the complete pinned set through
  `pythona.packages.install_many`; retain installed-package and restart checks.
- Each provider invocation gets a fresh JavaScript context. Preserve tool handoffs
  through provider records and resume the existing model run without replaying tools.
- HTTP serving, downloads, and inference run on background threads. Starting the
  service through `run_python` must return promptly so later tool calls can run.
- Allow only one generating conversation or pending native-tool run across backends.
  Keep at most one MLX model loaded, and finish releasing it before loading another,
  including when replacing the service.
- Keep blank-line heartbeats working in both the provider and setup probe. They
  must not extend request deadlines or interfere with cancellation and cleanup.

## Configuration and UI

- Save installed profiles and returned provider IDs in the Git-ignored
  `settings.local.json` beside `main.py`. Check known IDs with `pythona.ai`;
  only a confirmed missing ID invalidates a record. Preserve IDs on lookup errors.
- Drafts stay in memory. Add or Save Changes commits the provider and local record;
  Back, Cancel, closing, and model tests must not implicitly save form edits.
  Model availability and successful downloads must not be prerequisites for adding.
- UIKit owns the WebKit container, fixed native title, and close button. Page titles,
  navigation, and forms belong in `web/`; avoid synchronizing native navigation
  with page state. Keep UI strings in `ai_setup/strings.json` following app language.

## Validation

- Use the commands in [README's Development section](README.md#development).
  Run relevant Python and provider JS tests for backend, persistence, or transport
  changes; use browser tests for web UI changes.
