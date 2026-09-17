# Pythona AI Setup

Configure local model providers for Pythona AI Assistant. Clone this repository in
Pythona and run **`main.py`** from the project root.

Create independent configurations for **Apple Foundation Models** and **MLX-LM**.
Each configuration has its own name, tool choices, model selection, and installed
provider ID. Installation works even when the model is unavailable.

## Requirements

- Pythona with the `pythona.ai` Custom Provider API v2.
- Pythona's bundled rubicon-objc for the WebKit container.
- Apple Foundation Models: a supported device with Apple Intelligence and
  `apple-fm-sdk` for inference.
- MLX-LM: Pythona's bundled MLX. On the first test or assistant request, the backend
  installs `mlx-lm==0.31.3` through `pythona.packages` if it is missing, and downloads
  the selected model from Hugging Face. Choose an MLX-compatible model repository.

The page follows Pythona's App language, with English as its source and fallback.
It includes English, Simplified and Traditional Chinese, German, Spanish, French,
Japanese, Korean, and Russian. Light and dark appearances are supported.

## Use

1. Run `main.py` in Pythona. The home page lists installed providers managed by this
   project. Choose **Add** to open a new provider form.
2. Choose the backend, connection name, and tools. File tools are enabled by default;
   browser and Python tools are initially disabled.
3. For MLX-LM, enter a Hugging Face model ID. The default is
   `mlx-community/Qwen3-1.7B-4bit`, matching Pythona's `llm_demo.py` example.
   Its first download is about 1 GB.
4. Optionally run a test in the separate **Test Conversation** section.
5. Choose **Add to AI Assistant**. This installs the provider, saves its local record,
   and returns to the list. Select the connection in Pythona's AI Assistant settings
   to start chatting.

Tap a list entry to edit its model, name, or tools, then choose **Save Changes** to
update the same provider ID. **Cancel** or **Back** discards unsubmitted edits.
Model tests use the current form without saving it; testing never creates a provider.
Installation and saving remain available even when the model is unavailable or a
test fails.

Opening the page, opening an editor, and **Refresh** check saved provider IDs against
Pythona. Confirmed missing IDs are removed from the list and the local record.
A lookup failure preserves the record and displays an unverified status. Saving an
open editor also checks its ID, recreating the provider if it was deleted meanwhile.
Only IDs saved by this project are queried; Pythona's other providers are not enumerated.

To delete an installed provider, remove it in Pythona's AI Assistant settings.
Refresh this page to remove the corresponding list entry and setup record.

## Storage and privacy

`settings.local.json`, next to `main.py`, contains an installed `profiles` array and
shared `service` settings. Every profile has its own local `id`
and a separate `provider_id` returned by Pythona. This file is ignored by Git.
Drafts and navigation state stay in memory. There is no migration from previous
storage formats; delete the old JSON file before using this version. Deleting this
file forgets its provider IDs but does not delete installed providers from Pythona.

Each installed provider contains its runtime configuration under
`local_model_settings` in that provider's localStorage. Updating replaces its
JavaScript and this configuration while preserving unrelated localStorage keys.
The setup page itself uses a nonpersistent WebKit data store.

MLX dependencies and model downloads contact PyPI and Hugging Face respectively.
Model files use Hugging Face's normal local cache. Prompts and model responses are
processed on the device. Enabled tools can make their own network requests.

## Model adapters

Apple Foundation Models represents each enabled native tool as a separate `fm.Tool`,
with its own name, description, and typed parameter schema. The adapter supports
the App's current strings, integers, numbers, booleans, optional fields, string
enums, and numeric bounds. Unsupported schemas fail explicitly.

MLX-LM uses the selected model's chat template and built-in tool parser. It passes
individual function schemas to the template, validates complete tool arguments,
and supplies tool results before generating the next response. Models without
tool support can be used with all tool categories disabled. Thinking is disabled
through the chat template where the model supports that option.

Only tools present in `ctx.nativeTools` can be selected. The provider does not
additionally inspect `ctx.preferences.browserTools`. `ctx.instructions` is passed
unchanged, and history is not automatically truncated. Startup calls are excluded
from model history.

## Service and memory lifecycle

The generated JavaScript embeds the complete compressed Python backend. After
installation it does not depend on this repository's files. All configurations
share one authenticated service at `127.0.0.1:8768`. Each request identifies its
backend and model, so changing an MLX profile does not change another profile.

If the service is absent, JavaScript uses its embedded code through `run_python`
to start a daemon thread with an asyncio loop. Startup returns promptly and
subsequent `run_python` calls remain available. MLX downloads and inference run on
a separate worker, keeping the HTTP loop responsive.

Only one conversation can generate or wait for tools at a time, across both
backends. Another conversation receives HTTP 409 with a busy message, without
interrupting the current one or joining a queue. After the current turn finishes,
any conversation can use the service. A new turn in the same conversation can
cancel its previous unfinished request; native work still stopping must finish
before another request is accepted.

MLX uses one persistent background worker. It retains only the most recently used
model and tokenizer, reusing them across turns and configurations with the same
model ID. Switching models releases the previous weights before loading the next;
switching to Apple Foundation Models also releases the MLX model. Service shutdown
and idle timeout release the retained model. A lease shared by embedded backend
copies prevents a replacement service from loading another model before the old
one finishes cleanup.

Prompts, conversation history, and KV caches are rebuilt for each generation and
are not retained with the weights. Unused MLX allocations are cleared after each
request. Keeping the model loaded reduces repeated loading work but continues to
use memory while waiting for the next message.

MLX test results show the MLX process memory peak, active allocations including the
retained model, and remaining cache. These are MLX allocator measurements, not total App memory;
the peak includes earlier MLX use in the same App process. Actual memory use and
model quality still need testing on a supported device.
The MLX configuration page also explains that older devices may report MLX
compatibility errors, and longer conversations can exceed iOS memory limits and
cause Pythona to close unexpectedly.

During tool execution, HTTP hands the complete call to JavaScript. The provider
records the run and call IDs, and Pythona executes the native tool. The next provider
invocation returns the result and failure flag to `/resume`; the same backend
request continues without replaying the tool.

A streaming HTTP disconnect requests cancellation. MLX cancellation is cooperative:
a native kernel, package installation, or download already in progress must return
before the worker can release its resources. Requests arriving during that cleanup
receive a busy response. Cancelled or failed generations discard the retained model.
During native tool execution there is no active provider HTTP connection, so Stop
cannot immediately notify the service in that gap. Abandoned tool waits and normal
HTTP rounds expire after `request_seconds` (default 120 seconds). An initial MLX
request uses `load_seconds` (default 900 seconds) to allow model downloads. Both can
be adjusted in the shared service settings.

While an HTTP response is waiting for model output, the service sends a blank-line
heartbeat every 15 seconds. Both clients ignore these keepalives. This prevents the
App's network idle timeout during downloads, model loading, or prompt prefill;
heartbeats do not extend the model request deadline or prevent cancellation.

The service exits after five minutes without requests or pending tool waits.
This runs inside Pythona's interpreter and is not a persistent iOS background service.

## UI

The page is plain HTML, CSS, and JavaScript inside a small WKWebView container. Its
native navigation bar always shows Pythona AI Setup and a neutral close icon.
Page titles and localized Cancel and Back buttons live in HTML; page changes do not
update the native bar. Closing works independently of JavaScript and discards
unsubmitted edits. A validation or installation error keeps the form open.
Only the form's primary button saves changes. WebKit handles
keyboard scrolling. The page has one document scroll area and no fixed-height
chat output. Model checks and tests run in background threads. Closing the page
removes the message handler and requests cancellation of active tests.

## Development

The project runs directly from its clone. There is no frontend build step.

Preview the actual page in a desktop browser with simulated responses and
installation. Changes stay in memory and do not modify a provider:

```sh
python3 main.py --preview --language en
# Open http://127.0.0.1:8879
```

```text
main.py                Pythona entry point and desktop preview
ai_setup/app.py        Profile actions and background tests
ai_setup/ui.py         WebKit presentation and JSON bridge
ai_setup/settings.py   Configuration records and provider installation
ai_setup/model.py      Apple Foundation Models adapter and backend selection
ai_setup/mlx_model.py  MLX-LM generation, tool parsing, and cleanup
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

With MLX and MLX-LM installed on a Mac, the optional smoke check runs the actual
adapter twice with temporary tiny Qwen3 weights, checking model reuse and memory
release on shutdown. It does not download model weights or measure the full default model:

```sh
python3 scripts/test_mlx.py
```

For integration testing with a Pythona source checkout and an arm64 iOS simulator:

```sh
python3 scripts/test_ios.py /path/to/Pythona --device SIMULATOR_UDID
```

The runner temporarily links the Swift test into Pythona's test target and removes
that link afterward. It exercises WebKit, multiple provider installations, updates,
manual deletion and recreation, current App tool schemas, and a mocked model
exchange through JavaScriptCore. No project sources remain in the Pythona repository.
Real software keyboard interaction and full-model inference need device verification.
