# Pythona AI Setup

Configure model providers for Pythona AI Assistant. Clone this repository in
Pythona and run **`main.py`** from the project root.

Create independent configurations for **Apple Foundation Models**, **MLX-LM**, **Codex**,
and **ChatGPT** (Sign in with ChatGPT).
Each configuration has its own name, model selection, and installed provider ID.
Local models also have tool choices. Codex and ChatGPT require completed account
authorization before adding or saving a provider. Model availability is not an
installation prerequisite.

## Requirements

- Pythona with the `pythona.ai` Custom Provider API v2.
- Pythona's bundled rubicon-objc for the WebKit container.
- Codex: a ChatGPT account with Codex access and an internet connection.
- ChatGPT: an eligible ChatGPT Plus or Pro account with permission to use its plan
  in this app, an internet connection, and Pythona's bundled `cryptography` package.
- Apple Foundation Models: a supported device with Apple Intelligence and
  `apple-fm-sdk` for inference.
- MLX-LM: Pythona's bundled MLX. On the first test or assistant request, the backend
  selects MLX-LM and its dependencies for the current bundled libraries, then
  downloads the selected model from Hugging Face. Dependency installation requires
  Pythona's `pythona.packages.install_many` API. Choose an MLX-compatible model repository.

The page follows Pythona's App language, with English as its source and fallback.
It includes English, Simplified and Traditional Chinese, German, Spanish, French,
Japanese, Korean, Brazilian Portuguese, and Russian. Light and dark appearances
are supported.

## Use

1. Run `main.py` in Pythona. The home page lists installed providers managed by this
   project. Choose **Add**, then select a provider to open its configuration form.
   Cancelling the selection does not create a draft or install anything.
2. Enter a connection name. For local models, choose the tools:
   file tools are enabled by default; browser and Python tools are initially disabled.
   Codex and ChatGPT use all native tools supplied by Pythona, without a separate tool selector.
3. For MLX-LM, enter a Hugging Face model ID. The default is
   `mlx-community/Qwen3-1.7B-4bit`.
   Its first download is about 1 GB.
   For Codex, choose **Sign in with ChatGPT**, complete authorization in the
   in-app Safari window, and select a model and reasoning effort available to your
   account. The initial model is `gpt-6.1-sol`; it can be edited without updating Pythona.
   For ChatGPT, choose **Continue with ChatGPT** and authorize ChatGPT plan usage.
   Available models are suggested after sign-in; you can also enter a model ID.
4. Optionally run a test in the separate **Test Conversation** section.
5. Choose **Add to AI Assistant**. This installs the provider, saves its local record,
   and returns to the list. Select the connection in Pythona's AI Assistant settings
   to start chatting.

Tap a list entry to edit its model, name, or tools, then choose **Save Changes** to
update the same provider ID. **Cancel** or **Back** discards unsubmitted edits.
Model tests use the current form without saving it; testing never creates a provider.
Successful model tests are optional. Local models can be added even when unavailable;
Codex and ChatGPT require account authorization, including ChatGPT plan permission
for the ChatGPT provider. An unrelated inference failure does not block saving.

After pulling project updates, open each existing provider and choose **Save Changes**
to replace its embedded backend with the updated code.
When upgrading from a backend without the process-local service registry, restart
Pythona once after updating your providers to clear any workers left by the old code.

Opening the page, opening an editor, and **Refresh** check saved provider IDs against
Pythona. Confirmed missing IDs are removed from the list and the local record.
A lookup failure preserves the record and displays an unverified status. Saving an
open editor also checks its ID, recreating the provider if it was deleted meanwhile.
Only IDs saved by this project are queried; Pythona's other providers are not enumerated.

To delete an installed provider, remove it in Pythona's AI Assistant settings.
Refresh this page to remove the corresponding list entry and setup record.

## Codex sign-in

Sign-in uses `SFSafariViewController` and OAuth authorization code flow with PKCE.
A temporary Python HTTP listener receives the callback at
`http://127.0.0.1:1455/auth/callback`, with its socket bound only to the loopback
interface. The listener starts before the browser
opens. Another service occupying this port reports an error without being stopped.

The listener checks actual HTTP responses while waiting for authorization.
Native foreground notifications also wake this check, including while Safari
covers the setup page. If iOS has invalidated the sockets, the old listener exits
before the same login rebinds the same port. Its state, PKCE verifier, browser URL,
and ten-minute deadline are preserved. If the browser already displayed a failed
callback before recovery, reload that page. An expired login must be started again.
Process-local ownership also closes an older login before another setup run starts
one. Closing Safari, cancelling, leaving the form, or closing setup discards the
pending attempt and prevents late authorization results from being saved.

Successful sign-in remains a draft until **Add to AI Assistant** or **Save Changes**.
**Sign in again** replaces the account only when saved. Testing uses the current
form without installing a provider or saving its model, name, or reasoning effort.
A test may rotate credentials: draft credentials remain in memory; installed
credentials are updated in provider storage so the next request can use the new
refresh token. Wait for a test to finish, including cancellation, before saving or
signing in again. Reopening the same profile keeps its pending test visible and
does not start a second test. Setup model discovery and tests for one profile run
sequentially and read the latest saved credentials before making requests.
Adding or saving a Codex provider requires a completed sign-in with account credentials.
The button stays disabled until authorization succeeds, and installation also
checks credentials before creating or updating a provider. A model test is optional.

`ai_setup/codex_provider.js` contains the complete Codex provider. Setup embeds
this JavaScript with the selected model and reasoning effort. All tools in `ctx.nativeTools`
are passed through; hosted web search follows Pythona's web-search preference. The installed
provider contacts Codex directly, refreshes its own tokens, and preserves Responses
checkpoints across native tool rounds. It does not start the local model server.
Update this repository and save an existing Codex provider to update its code.

Cloud model tests allow up to 120 seconds, including quiet reasoning periods.
Cancellation interrupts an active HTTP response, and blank-line heartbeats do not
extend that deadline. Completed refusal responses appear as text in both the test
and installed provider.

The provider and setup test advertise Codex client version `0.160.0`, with matching
`Version`, `User-Agent`, and Responses beta headers. This follows the
[official stable release](https://github.com/openai/codex/releases/tag/rust-v0.160.0);
the header layout was also checked against
[OpenMinis's Codex requests](https://github.com/OpenMinis/OpenMinis/blob/b4c0661d5631ebab4d1a2e6f3fd4c805d4030a6c/src/ios/Providers/OpenAI/OpenAIProvider.swift#L924).
The callback host follows the
[official login implementation](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/login/src/server.rs#L183).

## Storage and privacy

`settings.local.json`, next to `main.py`, contains an installed `profiles` array and
shared `service` settings. Its optional `chatgpt_host_id` stores the random
installation UUID used for public ChatGPT sign-in. Every profile has its own local `id`
and a separate `provider_id` returned by Pythona. This file is ignored by Git.
Drafts and navigation state stay in memory. There is no migration from previous
storage formats; delete the old JSON file before using this version. Deleting this
file forgets its provider IDs but does not delete installed providers from Pythona.

Each installed local-model provider contains its runtime configuration under
`local_model_settings` in that provider's localStorage. Codex uses `codex_settings`
and stores account credentials in that same provider's localStorage; credentials
are not included in `settings.local.json` or sent to the setup web page.
ChatGPT uses `chatgpt_settings` and stores its complete credential record under
`chatgpt_auth`. These are Pythona's native per-provider storage values, not browser
localStorage. A refresh replaces the ChatGPT credential record in one write.
Refresh recovery reuses newer credentials already saved by another request.
ChatGPT also identifies each login locally so a delayed successful refresh can
recover that login after a concurrent failure, without replacing a later login.
This does not serialize refreshes across installed JavaScript contexts.
The installation UUID is generated and saved in `settings.local.json` before the
first ChatGPT sign-in and reused across accounts and launches. Saving this identifier
does not save form drafts or account credentials.
Updating replaces its
JavaScript and this configuration while preserving unrelated localStorage keys.
The setup page itself uses a nonpersistent WebKit data store.

MLX dependencies and model downloads contact PyPI and Hugging Face respectively.
Model files use Hugging Face's normal local cache. Apple Foundation Models and
MLX-LM process prompts and responses on the device. Codex sign-in and token refresh
contact `auth.openai.com`; Codex requests send prompts, conversation history, enabled
tool schemas, and tool results to `chatgpt.com`. ChatGPT sign-in contacts
`auth.openai.com`, and its model discovery and inference contact `api.openai.com`.
Enabled tools can make their own
network requests.

## Image input

Turn on **Image Understanding** in Pythona and choose a vision-capable Codex or
ChatGPT model to send image attachments or inspect workspace files with
`view_image`. Both providers send these images to their respective cloud endpoints.

Pythona supplies user attachments and tool results in the optional `message.images`
array as JPEG or PNG data URLs. The caption or tool result text stays in
`message.content`. The cloud providers send each image as an `input_image` block,
followed by an `input_text` block when text is present. Images from `view_image`
travel in the `function_call_output` for the original call ID. Each request uses
the images and text currently supplied by Pythona, including when replaying history.

The Apple Foundation Models and MLX-LM adapters in this project accept text only
and exclude `view_image` from their tools. If any message or tool result in the
current transcript contains an image, the provider reports an error before
inference. Switch to a provider with image support to continue that conversation,
or start a new text-only conversation.

## Sign in with ChatGPT provider

The **ChatGPT** entry is independent of **Codex**. Both can be installed for
comparison; they keep separate provider IDs, settings, and credentials.
This entry follows OpenAI's public
[local-client registration flow](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
and [ChatGPT-plan Responses interface](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).
It needs no developer API key, client secret, or application backend.

The first sign-in starts with `dynamic_agent_client`; the callback's issued client
ID is retained for code exchange and later sign-ins to that account and workspace.
An explicit retry after failed code exchange reuses the issued ID while the form
remains open. Sign-in uses PKCE and an OIDC nonce, then verifies the ID token's
RS256 signature against OpenAI's published JWKS, issuer, audience, expiry, nonce,
and returning account identity. The `cryptography` import and network work run on
the OAuth worker. Tokens and granted scopes are kept together; identity-only
sign-in does not enable inference or allow adding or saving a provider. Signing in again can request the missing plan
permission. The first successful grant explains plan usage and links to ChatGPT
usage settings.

The recoverable loopback listener and in-app Safari presentation are shared with
Codex. All draft, cancellation, foreground recovery, and explicit-save behavior
described above also applies to ChatGPT. Account model discovery is a background
request; failures do not prevent entering a model ID or adding the provider.

`chatgpt_provider.js` supplies public-route authentication, token refresh, errors,
and namespaced tool definitions. Installation bundles it with the Responses
streaming and history engine in `codex_provider.js`, without depending on either
file at runtime. Requests use `api.openai.com/v1/responses`, `store: false`, and
`stream: true`. All Pythona native tools are functions in the `pythona` namespace.
Tool calls, outputs, and encrypted reasoning are retained between invocations;
checkpoints are scoped to the registered account and selected model.

The route's [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
apply. Hosted web search depends on model/account availability. Subscription or
stream failures do not complete a turn or execute pending native tools. Repeated
authorization failures stop after one token refresh; quota and policy errors do
not trigger an OAuth retry loop. Setup tests use the same public route and preserve
rotated credentials even when inference fails or is cancelled.

## Model adapters

Apple Foundation Models represents each enabled native tool as a separate `fm.Tool`,
with its own name, description, and typed parameter schema. The adapter supports
the App's current strings, integers, numbers, booleans, optional fields, string
enums, and numeric bounds. Unsupported schemas fail explicitly.

Each new Apple Foundation Models request passes the latest user message directly
to the SDK. Earlier user messages, assistant responses, tool calls, and tool
results are restored through the SDK's native `Transcript` format, rather than
included as JSON text in a prompt. Current instructions and enabled tools come
from a fresh SDK session; historical tool calls do not enable tools or execute
again. Tool results retain their success or failure flag. Incomplete or mismatched
tool history reports an error instead of silently dropping records; start a new
conversation if an older conversation has no recorded result for a tool call.

MLX-LM uses the selected model's chat template and built-in tool parser. It passes
individual function schemas to the template, validates complete tool arguments,
and supplies tool results before generating the next response. Models without
tool support can be used with all tool categories disabled. Thinking is disabled
through the chat template where the model supports that option.

Only tools present in `ctx.nativeTools` can be selected. The provider does not
additionally inspect `ctx.preferences.browserTools`. `ctx.instructions` is passed
unchanged, and history is not automatically truncated. Startup calls are excluded
from model history.

## MLX dependency setup

Before importing MLX-LM or reusing a loaded model, the MLX worker checks the shared
Python package environment. It reads the actual bundled distributions, including
MLX and tokenizers, and uses `resolvelib` with PyPI metadata to select compatible
stable versions of MLX-LM and its complete dependency chain. The backend requires
`mlx-lm>=0.31.3`; compatible installed packages are preferred, including on later
offline requests. The small `resolvelib` helper is installed through Pythona if needed.
Installed packages are also checked against their `Requires-Python` metadata,
so an App upgrade that changes Python can trigger selection of a compatible replacement.

When packages need to change, the complete version selection, including unchanged
packages, bundled versions, and requested extras, is submitted in one
`pythona.packages.install_many` call. Pythona still checks conflicts with other
installed packages. Upgrading Pythona's bundled libraries or changing user packages
causes the next MLX request to check the current environment again. No App-version
table or persistent dependency lock file is used.

If no compatible combination is found, dependency setup reports the conflicting
requirements. A required version change to an already imported package reports
that Pythona must restart; the backend does not replace a loaded package and
continue with stale modules. A user package overriding a required bundled version
must be removed before retrying. Model availability checks and adding or updating
a provider never run dependency installation, and remain available when setup fails.

The setup test and installed JavaScript provider embed and use the same dependency
code. Desktop MLX smoke checks use the host's existing Python environment; automatic
dependency installation runs only inside Pythona on iOS.

## Service and memory lifecycle

For local models, the generated JavaScript embeds the complete compressed Python
backend. After installation it does not depend on this repository's files. All
local-model configurations share one authenticated service at `127.0.0.1:8768`. Each request identifies its
backend and model, so changing an MLX profile does not change another profile.

Backend copies also share a process-local service registry. If the HTTP listener
is lost while a cached model remains alive, startup closes the previous service
and waits for its worker to finish releasing resources before creating a replacement.
Healthy services keep reusing their cached model. A failed health check does not
interrupt an active request, and cleanup timeouts leave the previous owner tracked
so a later retry can complete the handoff.

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
main.py                      Pythona entry point and desktop preview
ai_setup/app.py              Profile actions and background tests
ai_setup/ui.py               WebKit presentation and JSON bridge
ai_setup/settings.py         Configuration records and provider installation
ai_setup/model.py            Apple Foundation Models adapter and backend selection
ai_setup/mlx_dependencies.py MLX dependency selection and coordinated installation
ai_setup/mlx_model.py        MLX-LM generation, tool parsing, and cleanup
ai_setup/server.py           HTTP lifecycle and tool result handoffs
ai_setup/provider.js         Provider protocol and embedded service startup
ai_setup/codex_provider.js   Repository-owned Codex provider and token refresh
ai_setup/codex_auth.py       OAuth PKCE login and recoverable loopback listener
ai_setup/codex_probe.py      Codex test requests and credential rotation
ai_setup/chatgpt_provider.js Public ChatGPT transport bundled with the Responses engine
ai_setup/chatgpt_auth.py     Dynamic registration, verified identity, and token refresh
ai_setup/chatgpt_probe.py    Account model discovery and public Responses tests
ai_setup/responses_probe.py Shared bounded SSE reader for cloud model tests
web/                         HTML, CSS, and browser code
tests/                       Python, JavaScript, and browser checks
```

Run host checks with Python 3.11+ and Node.js. Browser tests use an installed
Google Chrome through Playwright; npm dependencies are only needed for these tests:

```sh
python3 -m pip install -r requirements-dev.txt
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
