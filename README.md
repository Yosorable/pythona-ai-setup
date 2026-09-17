# Pythona AI Setup

Configure AI providers for Pythona AI Assistant. The current backend uses Apple’s
on-device model. Clone this repository in Pythona and run **`main.py`** from the
project root.

The local Web settings page lets you test the model, choose tool categories, and
add or update a JavaScript provider. Installation works even when the model is
unavailable. Apple Foundation Models is the current backend; MLX is not implemented.

## Requirements

- Pythona with the `pythona.ai` Custom Provider API v2.
- Pythona's bundled rubicon-objc for the WebKit container.
- A supported device with Apple Intelligence and `apple-fm-sdk` for actual inference.
  Missing model assets or SDK support do not prevent provider installation.

The page follows Pythona's App language, with English as its source and fallback.
It includes English, Simplified and Traditional Chinese, German, Spanish, French,
Japanese, Korean, and Russian. Light and dark appearances are supported.

## Use

1. Run `main.py` in Pythona.
2. Choose the connection name and tools. File tools are enabled by default;
   browser and Python tools are initially disabled.
3. Optionally run a test in the separate **Test Conversation** section.
4. Choose **Add to AI Assistant**, then close the page and select the connection
   in Pythona's AI Assistant settings.

Run the project again to update the same provider. The page saves configuration and
the provider ID in `settings.local.json`, next to `main.py`. This file is ignored
by Git. If the provider was deleted in Pythona, installation creates a new one.
Updating replaces that provider's JavaScript and its runtime configuration while
preserving unrelated localStorage keys.

## How it works

The page is plain HTML, CSS, and JavaScript, loaded into a small WKWebView container.
WebKit handles native keyboard scrolling; the page has one document scroll area
and no fixed-height chat output. The page communicates with Python using JSON
messages. Model checks and tests run in background threads, so the install action
remains available while a test runs. Closing the page removes the message handler
and requests cancellation of any active test.

Each enabled native tool becomes a separate `fm.Tool`, with its own name,
description, and typed parameter schema. The adapter supports the App's current
strings, integers, numbers, booleans, optional fields, string enums, and numeric
bounds. Unsupported schemas fail explicitly.

The generated JavaScript embeds the complete compressed Python backend. After
installation it does not depend on this repository's files. If the authenticated
service at `127.0.0.1:8768` is absent, JavaScript uses its embedded code through
`run_python` to start a daemon thread with an asyncio loop. The startup call returns
and subsequent `run_python` calls remain available.

During a user turn, a model tool callback waits while HTTP hands the complete call
to JavaScript. The provider records the run and call IDs, and Pythona executes the
native tool. The next provider invocation returns the result and failure flag to
`/resume`; the same SDK response continues. Text is streamed before and after calls.

Only tools actually present in `ctx.nativeTools` can be selected. The provider does
not additionally inspect `ctx.preferences.browserTools`; execution still depends
on what the App exposes. `ctx.instructions` is passed unchanged, and history is
not automatically truncated. Startup calls are excluded from model history.

## Service lifecycle

A streaming HTTP disconnect cancels that request. During native tool execution,
there is no active provider HTTP connection, so Stop cannot immediately notify the
service during that gap. Abandoned tool waits expire after `request_seconds`
(default 120 seconds), or are cancelled when that conversation starts a new request.
Active generation also times out after `request_seconds` per HTTP round. Expired
runs report an error without replaying tools.

The service exits after five minutes without active requests or pending tool waits.
Defaults are in `ai_setup/settings.py`. This runs inside Pythona's interpreter and
is not a persistent iOS background service.

## Development

The project runs directly from its clone. There is no frontend build step.

Preview the actual page in a desktop browser with simulated responses and
installation. Preview changes stay in memory and do not modify a provider:

```sh
python3 main.py --preview --language en
# Open http://127.0.0.1:8879
```

The code is organized as follows:

```text
main.py                Pythona entry point and desktop preview
ai_setup/app.py        Settings actions and background tests
ai_setup/ui.py         WebKit presentation and JSON bridge
ai_setup/settings.py   Configuration and provider installation
ai_setup/model.py      Apple Foundation Models adapter
ai_setup/server.py     HTTP lifecycle and tool result handoffs
ai_setup/provider.js   Provider protocol and embedded service startup
web/                   HTML, CSS, and browser code
tests/                 Python, JavaScript, browser, and iOS checks
```

Run host checks with Python 3.11+ and Node.js. Browser tests use an installed
Google Chrome through Playwright; npm dependencies are only needed for these tests:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
npm ci
npm test
```

For integration testing with a Pythona source checkout and an arm64 iOS simulator:

```sh
python3 scripts/test_ios.py /path/to/Pythona --device SIMULATOR_UDID
```

The runner temporarily links the Swift test into Pythona's test target, and removes
that link afterward. The test loads this repository directly. It exercises the real
WebKit bridge, provider creation and updates, all current App tool schemas, and a
mocked model exchange through JavaScriptCore. No project sources are kept in the
Pythona repository.

Generation quality, context limits, and actual software keyboard interaction still
need verification on a supported device.
