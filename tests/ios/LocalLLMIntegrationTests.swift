import XCTest
import UIKit
import WebKit

@testable import Pythona

@MainActor
final class LocalLLMIntegrationTests: XCTestCase {
    func testWebSettingsInstallUpdateAndModelTransport() async throws {
        let engine = PythonEngine.shared
        await engine.ensureInitialized()
        let pro = AppDefaults.isProUser
        let active = AIConfigStore.activeId
        let originalIDs = Set(AIConfigStore.all().map(\.id))
        AppDefaults.isProUser = true
        defer {
            for config in AIConfigStore.all() where !originalIDs.contains(config.id) && config.name.hasPrefix("Local LLM Test") {
                AIConfigStore.delete(config.id)
            }
            AIConfigStore.activeId = active
            AppDefaults.isProUser = pro
        }
        // The runner links this file into PythonaTests only for the duration of the test.
        let root = URL(fileURLWithPath: #filePath).resolvingSymlinksInPath()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        let rootLiteral = String(decoding: try encoder.encode(root), as: UTF8.self)
        do {
            let opened = await engine.runCodeForAI(code: """
            import sys, tempfile, types, threading, socket
            from pathlib import Path
            sys.path.insert(0, \(rootLiteral))
            from ai_setup.app import SetupApp
            from ai_setup.ui import WebSettings
            from ai_setup.settings import SettingsStore
            state = types.ModuleType('_local_llm_integration')
            sys.modules[state.__name__] = state
            state.root = \(rootLiteral)
            state.directory = tempfile.TemporaryDirectory(prefix='pythona_local_llm_test_')
            state.store = SettingsStore(Path(state.directory.name) / 'settings.local.json')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                state.store.service['port'] = sock.getsockname()[1]
            state.app = SetupApp(state.store, 'en', status=lambda settings: {'available': False,
                'reason': 'DEVICE_NOT_ELIGIBLE' if settings['backend'] == 'apple_fm' else 'MLX_UNAVAILABLE'})
            state.host = WebSettings(state.app)
            run_on_ui(state.host.open).wait()
            def pump():
                while not state.app.closed.is_set():
                    state.host.process_next()
            state.thread = threading.Thread(target=pump, daemon=True)
            state.thread.start()
            state.app.refresh_installations()
            """)
            XCTAssertTrue(opened.succeeded, opened.output)
            guard opened.succeeded else { await cleanup(engine); return }
            let window = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
                .flatMap(\.windows).first(where: \.isKeyWindow))
            var presented = try XCTUnwrap(window.rootViewController)
            while let next = presented.presentedViewController { presented = next }
            let navigation = try XCTUnwrap(presented as? UINavigationController)
            let controller = try XCTUnwrap(navigation.topViewController)
            let close = try XCTUnwrap(controller.navigationItem.rightBarButtonItem)
            let closeAction = try XCTUnwrap(close.action)
            XCTAssertEqual(controller.title, "Pythona AI Setup")
            XCTAssertNil(controller.navigationItem.leftBarButtonItem)
            XCTAssertEqual(close.style, .plain)
            XCTAssertEqual(close.tintColor, UIColor.label)
            XCTAssertTrue(close.image?.isSymbolImage == true)
            let web = try XCTUnwrap(controller.view.subviews.compactMap { $0 as? WKWebView }.first)
            func capture(_ name: String) {
                let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
                    window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
                }
                let attachment = XCTAttachment(image: image)
                attachment.name = name
                attachment.lifetime = .keepAlways
                add(attachment)
            }
            try await waitPage(web, "home")
            controller.view.layoutIfNeeded()
            XCTAssertGreaterThan(web.scrollView.adjustedContentInset.top, 0)
            try await Task.sleep(for: .milliseconds(300))
            capture("Provider home · Empty list")
            _ = try await web.evaluateJavaScript("document.getElementById('new').click()")
            try await waitPage(web, "new")
            XCTAssertEqual(controller.title, "Pythona AI Setup")
            XCTAssertTrue(controller.navigationItem.rightBarButtonItem === close)
            XCTAssertNil(controller.navigationItem.leftBarButtonItem)
            try await wait(web, "document.getElementById('detail-title').textContent === 'Add Provider' && document.getElementById('back').textContent === 'Cancel' && document.getElementById('back').getBoundingClientRect().width > 0")
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = 'Discarded draft'")
            _ = try await web.evaluateJavaScript("document.getElementById('back').click()")
            try await waitPage(web, "home")
            let count = try await web.evaluateJavaScript("document.querySelectorAll('.profile').length")
            XCTAssertEqual(count as? Int, 0)
            _ = try await web.evaluateJavaScript("document.getElementById('new').click()")
            try await waitPage(web, "new")
            try await wait(web, "document.getElementById('availability').dataset.kind === 'unavailable'")
            let filesEnabled = try await web.evaluateJavaScript("document.getElementById('files').checked")
            XCTAssertEqual(filesEnabled as? Bool, true)
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = 'Local LLM Test'; document.getElementById('install').click()")
            try await waitPage(web, "home")
            let saved = await engine.runCodeForAI(code: """
            import sys
            from ai_setup.settings import SettingsStore
            state = sys.modules['_local_llm_integration']
            assert SettingsStore(state.store.path).data['profiles'][0]['provider_id'] == state.store.data['profiles'][0]['provider_id']
            print(state.store.data['profiles'][0]['provider_id'])
            """)
            XCTAssertTrue(saved.succeeded, saved.output)
            let id = saved.output.trimmingCharacters(in: .whitespacesAndNewlines)
            let config = try XCTUnwrap(AIConfigStore.config(id: id))
            XCTAssertFalse(config.jsCode.contains(root))
            let browser = AppDefaults.aiBrowserToolsEnabled
            AppDefaults.aiBrowserToolsEnabled = true
            let appTools = AIToolbox.all(engine: engine, workspace: AIWorkspace(storage: .local, relativePath: "."))
            AppDefaults.aiBrowserToolsEnabled = browser
            let definitions: [[String: Any]] = try appTools.map { tool in
                let function = try XCTUnwrap(tool.schema["function"] as? [String: Any])
                return ["name": tool.name, "description": function["description"] ?? "", "inputSchema": function["parameters"] ?? [:]]
            }
            let tools = definitions.filter { ["run_python", "read_file"].contains($0["name"] as? String ?? "") }
            let transcript: [AIChatStore.Record] = [.message(AIChatMessage(role: "user", content: "Read demo.py"))]
            let boot = try await events(config, tools: tools, records: transcript)
            let startup = try XCTUnwrap(boot.compactMap { event -> AIToolCall? in
                if case .toolCall(let call) = event { return call }; return nil
            }.first)
            let arguments = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(startup.function.arguments.utf8)) as? [String: String])
            let started = await engine.runCodeForAI(code: try XCTUnwrap(arguments["code"]))
            XCTAssertTrue(started.succeeded, started.output)
            let definitionsJSON = String(decoding: try JSONSerialization.data(withJSONObject: definitions), as: UTF8.self)
            let definitionsLiteral = String(decoding: try encoder.encode(definitionsJSON), as: UTF8.self)
            let mocked = await engine.runCodeForAI(code: """
            import asyncio, json, sys
            import apple_fm_sdk as fm
            from ai_setup.bundle import backend_bundle, load_backend
            from ai_setup.settings import provider_settings
            state = sys.modules['_local_llm_integration']
            runtime = load_backend()
            runtime['service_json'](provider_settings(state.store.data['profiles'][0], state.store.service), '/shutdown', method='POST')
            async def schemas():
                async def unused(name, arguments):
                    raise AssertionError('Unexpected tool execution')
                definitions = json.loads(\(definitionsLiteral))
                registered = runtime['native_tools'](fm, definitions, unused)
                assert len(registered) == 20
                for tool in registered:
                    assert isinstance(tool, fm.Tool) and tool.arguments_schema.to_dict()
            asyncio.run(schemas())
            async def backend(request, invoke):
                if not request['tools']:
                    yield {'type': 'text', 'delta': 'Model test received 😀'}
                else:
                    registered = runtime['native_tools'](fm, request['tools'], invoke)
                    result = await asyncio.to_thread(lambda: asyncio.run(registered[0].call(fm.GeneratedContent({'path': 'demo.py'}))))
                    assert json.loads(result) == {'content': 'print(1)', 'failed': False}
                    yield {'type': 'text', 'delta': 'File read 😀'}
                yield {'type': 'finish', 'reason': 'stop'}
            settings = provider_settings(state.store.data['profiles'][0], state.store.service)
            settings['port'] = 0
            state.service = runtime['LocalModelService'](settings, backend_bundle()[1], backend=backend,
                status=lambda: {'available': False, 'reason': 'DEVICE_NOT_ELIGIBLE'}).start()
            with state.app.lock:
                state.store.service['port'] = state.service.port
            """)
            XCTAssertTrue(mocked.succeeded, mocked.output)
            _ = try await web.evaluateJavaScript("document.querySelector('.profile').click()")
            try await waitPage(web, "edit")
            XCTAssertEqual(controller.title, "Pythona AI Setup")
            XCTAssertTrue(controller.navigationItem.rightBarButtonItem === close)
            XCTAssertNil(controller.navigationItem.leftBarButtonItem)
            try await wait(web, "document.getElementById('detail-title').textContent === 'Edit Provider' && document.getElementById('back').textContent === 'Back'")
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = 'Local LLM Test Updated'; document.getElementById('install').click()")
            try await waitPage(web, "home")
            let updated = try XCTUnwrap(AIConfigStore.config(id: id))
            XCTAssertEqual(updated.name, "Local LLM Test Updated")
            _ = try await web.evaluateJavaScript("document.querySelector('.profile').click()")
            try await waitPage(web, "edit")
            _ = try await web.evaluateJavaScript("document.getElementById('test').click()")
            try await wait(web, "document.getElementById('reply').textContent.includes('Model test received')")
            let first = try await events(updated, tools: tools, records: transcript)
            let call = try XCTUnwrap(first.compactMap { event -> AIToolCall? in
                if case .toolCall(let call) = event { return call }; return nil
            }.first)
            XCTAssertEqual(call.function.name, "read_file")
            let checkpoints = first.compactMap { event -> AIChatStore.Record? in
                if case .providerRecord(let namespace, let data) = event {
                    return .providerRecord(AIProviderRecord(providerId: id, namespace: namespace, data: data))
                }; return nil
            }
            var assistant = AIChatMessage(role: "assistant", content: nil)
            assistant.toolCalls = [call]
            let second = try await events(updated, tools: tools, records: transcript + checkpoints + [
                .message(assistant), .message(AIChatMessage(role: "tool", content: "print(1)", toolCallId: call.id, name: call.function.name)),
            ])
            XCTAssertEqual(second.compactMap { event -> String? in
                if case .text(let delta) = event { return delta }; return nil
            }.joined(), "File read 😀")
            XCTAssertEqual(web.frame, controller.view.bounds)
            _ = try await web.evaluateJavaScript("window.scrollTo(0, document.documentElement.scrollHeight)")
            try await wait(web, "window.scrollY > 0 && document.getElementById('reply').getBoundingClientRect().bottom <= window.visualViewport.height + window.visualViewport.offsetTop")
            try await Task.sleep(for: .milliseconds(150))
            capture("Web settings · Test conversation")
            // Add and edit another backend, then verify real deletion from the App.
            _ = try await web.evaluateJavaScript("document.getElementById('back').click()")
            try await waitPage(web, "home")
            _ = try await web.evaluateJavaScript("document.getElementById('new').click()")
            try await waitPage(web, "new")
            _ = try await web.evaluateJavaScript("document.getElementById('backend').value = 'mlx_lm'; document.getElementById('backend').dispatchEvent(new Event('change'))")
            try await wait(web, "!document.getElementById('mlx-settings').hidden && !document.getElementById('install').disabled")
            let modelID = try await web.evaluateJavaScript("document.getElementById('model-id').value")
            XCTAssertEqual(modelID as? String, "mlx-community/Qwen3-1.7B-4bit")
            try await Task.sleep(for: .milliseconds(200))
            capture("Add provider · MLX")
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = 'Local LLM Test MLX'; document.getElementById('install').click()")
            try await waitPage(web, "home")
            let mlx = try XCTUnwrap(AIConfigStore.all().first(where: { $0.name == "Local LLM Test MLX" }))
            XCTAssertNotEqual(mlx.id, id)
            XCTAssertTrue(JSFileLocalStorage.snapshot(namespace: mlx.id)["local_model_settings"]?.contains("mlx-community/Qwen3-1.7B-4bit") == true)
            try await Task.sleep(for: .milliseconds(200))
            capture("Provider home · Installed models")
            _ = try await web.evaluateJavaScript("document.querySelectorAll('.profile')[1].click()")
            try await waitPage(web, "edit")
            _ = try await web.evaluateJavaScript("document.getElementById('model-id').value = 'example/another-model'; document.getElementById('install').click()")
            try await waitPage(web, "home")
            XCTAssertTrue(JSFileLocalStorage.snapshot(namespace: mlx.id)["local_model_settings"]?.contains("example/another-model") == true)
            XCTAssertEqual(AIConfigStore.config(id: id)?.name, "Local LLM Test Updated")
            _ = try await web.evaluateJavaScript("document.querySelectorAll('.profile')[1].click()")
            try await waitPage(web, "edit")
            AIConfigStore.delete(mlx.id)
            _ = try await web.evaluateJavaScript("document.getElementById('install').click()")
            try await waitPage(web, "home")
            let replacement = try XCTUnwrap(AIConfigStore.all().first(where: { $0.name == "Local LLM Test MLX" }))
            XCTAssertNotEqual(replacement.id, mlx.id)
            AIConfigStore.delete(replacement.id)
            _ = try await web.evaluateJavaScript("document.getElementById('refresh').click()")
            try await wait(web, "document.querySelectorAll('.profile').length === 1 && !document.getElementById('refresh').disabled")
            _ = try await web.evaluateJavaScript("document.querySelector('.profile').click()")
            try await waitPage(web, "edit")
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = 'Discarded edit'")
            _ = try await web.evaluateJavaScript("document.getElementById('back').click()")
            try await waitPage(web, "home")
            XCTAssertEqual(AIConfigStore.config(id: id)?.name, "Local LLM Test Updated")
            _ = try await web.evaluateJavaScript("document.querySelector('.profile').click()")
            try await waitPage(web, "edit")
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = ''; document.getElementById('install').click()")
            try await wait(web, "!document.getElementById('error').hidden")
            XCTAssertEqual(controller.title, "Pythona AI Setup")
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = 'Local LLM Test Final'; document.getElementById('install').click()")
            try await waitPage(web, "home")
            XCTAssertEqual(AIConfigStore.config(id: id)?.name, "Local LLM Test Final")
            _ = try await web.evaluateJavaScript("document.querySelector('.profile').click()")
            try await waitPage(web, "edit")
            // Closing discards the form and works even if the JavaScript bridge is unavailable.
            _ = try await web.evaluateJavaScript("document.getElementById('name').value = ''; delete window.setupBridge")
            XCTAssertTrue(controller.navigationItem.rightBarButtonItem === close)
            XCTAssertTrue(UIApplication.shared.sendAction(closeAction, to: close.target, from: close, for: nil))
            for _ in 0..<30 where navigation.presentingViewController != nil {
                try await Task.sleep(for: .milliseconds(100))
                let result = await engine.runCodeForAI(code: """
                import sys
                state = sys.modules['_local_llm_integration']
                if state.app.closed.is_set():
                    run_on_ui(state.host.close).wait()
                """)
                XCTAssertTrue(result.succeeded, result.output)
            }
            XCTAssertNil(navigation.presentingViewController)
            let closed = await engine.runCodeForAI(code: """
            import sys
            from ai_setup.settings import SettingsStore
            state = sys.modules['_local_llm_integration']
            saved = SettingsStore(state.store.path).data['profiles'][0]
            assert saved['name'] == 'Local LLM Test Final'
            assert saved['provider_id'] == state.store.data['profiles'][0]['provider_id']
            assert state.app.closed.is_set() and all(job['cancelled'].is_set() for job in state.app.jobs.values())
            """)
            XCTAssertTrue(closed.succeeded, closed.output)
        } catch {
            await cleanup(engine)
            throw error
        }
        await cleanup(engine)
    }

    private func waitPage(_ web: WKWebView, _ page: String) async throws {
        try await wait(web, "document.documentElement.dataset.page === '\(page)' && !document.getElementById('new').disabled")
    }

    private func wait(_ web: WKWebView, _ expression: String) async throws {
        for _ in 0..<100 {
            if (try? await web.evaluateJavaScript(expression)) as? Bool == true { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        let body = try? await web.evaluateJavaScript("document.body.innerText")
        XCTFail("Web condition timed out: \(expression)\n\(body ?? "No page text")")
        throw NSError(domain: "LocalLLMTest", code: 1)
    }

    private func events(_ config: AIConfig, tools: [[String: Any]], records: [AIChatStore.Record]) async throws -> [AIStreamEvent] {
        var output: [AIStreamEvent] = []
        for try await event in AICustomJSClient(config: config).stream(instructions: "Test instructions", nativeTools: tools,
            conversationId: "local-llm-integration", transcript: records) { output.append(event) }
        return output
    }

    private func cleanup(_ engine: PythonEngine) async {
        let result = await engine.runCodeForAI(code: """
        import contextlib, sys
        state = sys.modules.pop('_local_llm_integration', None)
        if state is not None:
            if hasattr(state, 'host'):
                run_on_ui(state.host.close).wait()
            if hasattr(state, 'thread'):
                state.thread.join(timeout=3)
            if hasattr(state, 'service'):
                state.service.stop()
                assert state.service.closed.wait(3)
            with contextlib.suppress(Exception):
                from ai_setup.bundle import load_backend
                from ai_setup.settings import provider_settings
                load_backend()['service_json'](state.store.service, '/shutdown', method='POST')
            state.directory.cleanup()
            sys.path[:] = [path for path in sys.path if path != state.root]
        """)
        XCTAssertTrue(result.succeeded, result.output)
    }
}
