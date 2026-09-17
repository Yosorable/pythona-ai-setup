"""Host regressions for SDK-independent installation, persistence, bundling, and HTTP."""

import asyncio
import copy
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_setup.bundle import backend_bundle, bootstrap_source, build_provider, load_backend
from ai_setup.probe import generate_probe
from ai_setup.settings import SettingsStore, defaults, service_defaults, install, provider_settings, STORAGE_KEY, STATE_PATH


class FakeAI:
    def __init__(self):
        self.providers = {}
        self.created = 0

    def create_custom_provider(self, **values):
        self.created += 1
        provider_id = f"provider-{self.created}"
        self.providers[provider_id] = {"id": provider_id, **copy.deepcopy(values)}
        return provider_id

    def get_custom_provider(self, provider_id):
        return copy.deepcopy(self.providers[provider_id])

    def update_custom_provider(self, provider_id, **values):
        self.providers[provider_id].update(copy.deepcopy(values))


class InstallTests(unittest.TestCase):
    def test_state_file_belongs_to_the_project_and_is_ignored(self):
        project = Path(__file__).resolve().parents[1]
        self.assertEqual(STATE_PATH, project / "settings.local.json")
        self.assertIn(STATE_PATH.name, (project / ".gitignore").read_text().splitlines())

    def test_install_without_sdk_then_reload_and_update_same_id(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"apple_fm_sdk": None}):
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            ai = FakeAI()
            provider_id = install(store, defaults(), ai)
            ai.providers[provider_id]["local_storage"]["user_owned"] = "preserve"
            store = SettingsStore(path)
            self.assertEqual(store.data["profiles"][0]["provider_id"], provider_id)
            changed = copy.deepcopy(store.data["profiles"][0])
            changed["name"] = "测试连接 😀"
            changed["groups"]["files"] = True
            self.assertEqual(install(store, changed, ai), provider_id)
            self.assertEqual(ai.created, 1)
            snapshot = ai.get_custom_provider(provider_id)
            self.assertEqual(snapshot["name"], changed["name"])
            self.assertEqual(snapshot["local_storage"]["user_owned"], "preserve")
            saved = json.loads(snapshot["local_storage"][STORAGE_KEY])
            self.assertEqual(saved["groups"], {"files": True, "browser": False, "python": False})
            self.assertNotIn(str(Path(__file__).resolve().parents[1]), snapshot["js_code"])
            self.assertIn("BACKEND_PAYLOAD", snapshot["js_code"])

    def test_deleted_provider_is_recreated_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            ai = FakeAI()
            original = install(store, defaults(), ai)
            del ai.providers[original]
            replacement = install(store, store.data["profiles"][0], ai)
            self.assertNotEqual(original, replacement)
            self.assertEqual(SettingsStore(store.path).data["profiles"][0]["provider_id"], replacement)

    def test_failed_final_save_keeps_id_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            ai = FakeAI()
            draft = defaults()
            with patch('ai_setup.settings.os.replace', side_effect=OSError('Disk full')):
                with self.assertRaisesRegex(RuntimeError, "provider-1"):
                    install(store, draft, ai)
            self.assertEqual(install(store, draft, ai), "provider-1")
            self.assertEqual(ai.created, 1)

    def test_corrupt_state_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                SettingsStore(path)
            self.assertEqual(path.read_text(), "{broken")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_backend()
        self.settings = provider_settings(defaults(), service_defaults())
        self.settings["port"] = 0
        self.settings["idle_seconds"] = 30
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.stop()
            self.assertTrue(service.closed.wait(3), "服务线程未退出")

    def start(self, backend, **settings):
        config = dict(self.settings, **settings)
        service = self.namespace["LocalModelService"](
            config, "test-build", backend=backend,
            status=lambda: {"available": False, "reason": "TEST_UNAVAILABLE"}).start()
        self.services.append(service)
        return service

    def request(self, service, path, payload=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=3)
        conn.request("GET" if payload is None else "POST", path,
                     body=None if payload is None else json.dumps(payload),
                     headers={"Authorization": "Bearer " + (token or self.settings["service_token"]),
                              "Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def payload(self):
        return {"backend": "apple_fm", "model_id": "", "owner": "test-conversation", "instructions": "照原样保留", "tools": [],
                "messages": [{"role": "user", "content": "你好"}]}

    def test_health_works_during_generation_and_disconnect_cancels(self):
        started, cancelled = threading.Event(), threading.Event()

        async def backend(request, invoke):
            self.assertEqual(request["instructions"], "照原样保留")
            started.set()
            try:
                yield {"type": "text", "delta": "开始 😀"}
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        service = self.start(backend)
        conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=3)
        conn.request("POST", "/generate", body=json.dumps(self.payload()),
                     headers={"Authorization": "Bearer " + self.settings["service_token"]})
        response = conn.getresponse()
        self.assertIn("开始", json.loads(response.readline())["delta"])
        self.assertTrue(started.is_set())
        status, body = self.request(service, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["service"], "pythona-local-llm")
        other = {**self.payload(), "owner": "another-conversation"}
        self.assertEqual(self.request(service, "/generate", other)[0], 409)
        response.close()
        conn.close()
        self.assertTrue(cancelled.wait(2), "断开连接没有取消推理")

    def test_error_status_auth_and_idle_shutdown(self):
        async def backend(request, invoke):
            raise RuntimeError("模型不可用")
            yield

        service = self.start(backend, idle_seconds=0.3)
        status, body = self.request(service, "/status")
        self.assertFalse(json.loads(body)["available"])
        self.assertEqual(self.request(service, "/health", token="wrong")[0], 401)
        status, body = self.request(service, "/generate", self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["type"], "error")
        self.assertTrue(service.closed.wait(2))

    def test_timeout_cancels_model_and_request_never_counts_as_idle(self):
        self.namespace["HEARTBEAT_SECONDS"] = 0.05
        cancelled = threading.Event()

        async def backend(request, invoke):
            try:
                await asyncio.sleep(30)
                yield
            finally:
                cancelled.set()

        service = self.start(backend, idle_seconds=0.1, request_seconds=0.4)
        status, body = self.request(service, "/generate", self.payload())
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\n"))
        self.assertIn("timed out", json.loads(body)["message"])
        self.assertTrue(cancelled.wait(1))

    def test_heartbeat_only_connection_disconnect_cancels_model(self):
        self.namespace["HEARTBEAT_SECONDS"] = 0.02
        cancelled = threading.Event()

        async def backend(request, invoke):
            try:
                await asyncio.sleep(30)
                yield {"type": "finish", "reason": "stop"}
            finally:
                cancelled.set()

        service = self.start(backend)
        connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=3)
        try:
            connection.request("POST", "/generate", body=json.dumps(self.payload()),
                               headers={"Authorization": "Bearer " + self.settings["service_token"]})
            response = connection.getresponse()
            try:
                self.assertEqual(response.readline(), b"\n")
                self.assertFalse(cancelled.is_set())
            finally:
                response.close()
        finally:
            connection.close()
        self.assertTrue(cancelled.wait(1))

    def test_setup_probe_ignores_heartbeats_while_loading(self):
        self.namespace["HEARTBEAT_SECONDS"] = 0.02

        async def backend(request, invoke):
            await asyncio.sleep(0.12)
            yield {"type": "text", "delta": "Ready 😀"}
            yield {"type": "finish", "reason": "stop", "memory": {"active": 100}}

        service = self.start(backend)
        settings = {**self.settings, "port": service.port}
        result = asyncio.run(generate_probe(settings, "Hello"))
        self.assertEqual(result, {"text": "Ready 😀", "memory": {"active": 100}})

    def test_embedded_bootstrap_needs_no_project_files_or_sdk(self):
        config = dict(self.settings)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            config["port"] = sock.getsockname()[1]
        payload, build_id = backend_bundle()
        code = bootstrap_source(payload, build_id, config)
        with patch.dict(sys.modules, {"apple_fm_sdk": None}):
            scope = {}
            try:
                exec(compile(code, "<test-bootstrap>", "exec"), scope)
                first = self.namespace["service_json"](config, "/health")
                self.assertEqual(first["build"], build_id)
                second = self.namespace["start_service"](config, build_id)
                self.assertEqual(first, second)
                result = self.namespace["service_json"](config, "/status")
                self.assertFalse(result["apple_fm"]["available"])
            finally:
                self.namespace["service_json"](config, "/shutdown", method="POST")

    def tool_payload(self):
        payload = self.payload()
        payload["tools"] = [{"name": "read_file", "inputSchema": {"type": "object"}}]
        return payload

    def resume_payload(self, call, **result):
        return {"backend": "apple_fm", "model_id": "", "owner": "test-conversation", "run_id": call["run_id"],
                "result": {"id": call["id"], "name": call["name"], "content": "print(1)", "failed": False, **result}}

    def test_heartbeats_preserve_generate_and_resume_handoffs(self):
        self.namespace["HEARTBEAT_SECONDS"] = 0.02

        async def backend(request, invoke):
            await asyncio.sleep(0.12)
            await invoke("read_file", {"path": "demo.py"})
            await asyncio.sleep(0.12)
            yield {"type": "text", "delta": "Read successfully"}
            yield {"type": "finish", "reason": "stop"}

        service = self.start(backend)
        status, body = self.request(service, "/generate", self.tool_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\n"))
        call = json.loads(body)
        self.assertEqual(call["type"], "tool_request")
        status, body = self.request(service, "/resume", self.resume_payload(call))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\n"))
        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual(events, [{"type": "text", "delta": "Read successfully"}, {"type": "finish", "reason": "stop"}])

    def test_tool_result_resumes_same_generation_and_preserves_failure(self):
        invocations = []
        resumed = []

        async def backend(request, invoke):
            invocations.append(request)
            for index in range(2):
                result = await invoke("read_file", {"path": f"file{index}.py"})
                resumed.append(json.loads(result))
            yield {"type": "text", "delta": "Done 😀"}
            yield {"type": "finish", "reason": "stop"}

        service = self.start(backend)
        status, body = self.request(service, "/generate", self.tool_payload())
        call = json.loads(body)
        self.assertEqual(call["type"], "tool_request")
        self.assertEqual(len(invocations), 1)
        self.assertEqual(resumed, [])
        # Invalid results must neither release the SDK callback nor consume the call.
        wrong = self.resume_payload(call)
        wrong["owner"] = "different-conversation"
        self.assertEqual(self.request(service, "/resume", wrong)[0], 400)
        self.assertEqual(self.request(service, "/resume", self.resume_payload(call, id="wrong"))[0], 400)
        status, body = self.request(service, "/resume", self.resume_payload(call, content="Permission denied", failed=True))
        second = json.loads(body)
        self.assertEqual(second["run_id"], call["run_id"])
        self.assertNotEqual(second["id"], call["id"])
        self.assertEqual(self.request(service, "/resume", self.resume_payload(call))[0], 400)
        status, body = self.request(service, "/resume", self.resume_payload(second))
        self.assertEqual(status, 200)
        self.assertEqual([json.loads(line)["type"] for line in body.splitlines()], ["text", "finish"])
        self.assertEqual(len(invocations), 1)
        self.assertEqual(resumed, [{"content": "Permission denied", "failed": True}, {"content": "print(1)", "failed": False}])
        self.assertEqual(self.request(service, "/resume", self.resume_payload(second))[0], 400)

    def test_parallel_sdk_calls_are_handed_off_without_deadlock(self):
        async def backend(request, invoke):
            results = await asyncio.gather(invoke("read_file", {"path": "a"}), invoke("read_file", {"path": "b"}))
            yield {"type": "text", "delta": str(len(results))}
            yield {"type": "finish", "reason": "stop"}

        service = self.start(backend)
        _, body = self.request(service, "/generate", self.tool_payload())
        first = json.loads(body)
        _, body = self.request(service, "/resume", self.resume_payload(first))
        second = json.loads(body)
        self.assertEqual(second["type"], "tool_request")
        _, body = self.request(service, "/resume", self.resume_payload(second))
        self.assertEqual(json.loads(body.splitlines()[0])["delta"], "2")

    def test_another_conversation_is_rejected_during_a_tool_wait(self):
        owners = []

        async def backend(request, invoke):
            owners.append(request["owner"])
            if request["tools"]:
                await invoke("read_file", {})
            yield {"type": "finish", "reason": "stop"}

        service = self.start(backend)
        _, body = self.request(service, "/generate", self.tool_payload())
        call = json.loads(body)
        other = {**self.payload(), "owner": "another-conversation"}
        status, error = self.request(service, "/generate", other)
        self.assertEqual(status, 409)
        self.assertIn("busy", json.loads(error)["error"])
        self.assertEqual(owners, ["test-conversation"])
        self.assertEqual(self.request(service, "/health")[0], 200)
        self.assertEqual(self.request(service, "/resume", self.resume_payload(call))[0], 200)
        self.assertEqual(self.request(service, "/generate", other)[0], 200)
        self.assertEqual(owners, ["test-conversation", "another-conversation"])

    def test_admission_stays_reserved_while_a_superseded_run_is_cancelling(self):
        from concurrent.futures import ThreadPoolExecutor
        cancelling, release = threading.Event(), threading.Event()
        runs = []

        async def backend(request, invoke):
            runs.append(request)
            if len(runs) == 1:
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelling.set()
                    while not release.is_set():
                        await asyncio.sleep(0.01)
            yield {"type": "finish", "reason": "stop"}

        service = self.start(backend)
        connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=3)
        connection.request("POST", "/generate", body=json.dumps(self.payload()),
                           headers={"Authorization": "Bearer " + self.settings["service_token"]})
        response = connection.getresponse()
        with ThreadPoolExecutor(max_workers=1) as pool:
            replacement = pool.submit(self.request, service, "/generate", self.payload())
            try:
                self.assertTrue(cancelling.wait(1))
                for owner in ("another-conversation", "test-conversation"):
                    self.assertEqual(self.request(service, "/generate", {**self.payload(), "owner": owner})[0], 409)
                self.assertEqual(len(runs), 1)
            finally:
                release.set()
                response.close()
                connection.close()
            self.assertEqual(replacement.result(timeout=2)[0], 200)
        self.assertEqual(len(runs), 2)

    def test_waiting_tool_expires_and_new_message_cancels_superseded_run(self):
        cancelled = threading.Event()

        async def backend(request, invoke):
            try:
                await invoke("read_file", {})
                yield {"type": "finish", "reason": "stop"}
            finally:
                cancelled.set()

        service = self.start(backend, request_seconds=0.4, idle_seconds=0.1)
        _, body = self.request(service, "/generate", self.tool_payload())
        old = json.loads(body)
        self.assertFalse(service.closed.wait(0.15), "A pending tool must keep the service alive")
        _, body = self.request(service, "/generate", self.tool_payload())
        current = json.loads(body)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.request(service, "/resume", self.resume_payload(old))[0], 400)
        cancelled.clear()
        self.assertTrue(cancelled.wait(2), "An abandoned SDK callback must be cancelled")
        self.assertTrue(service.closed.wait(2))

    def test_shutdown_releases_pending_tools(self):
        cancelled = threading.Event()

        async def backend(request, invoke):
            try:
                await invoke("read_file", {})
                yield {"type": "finish", "reason": "stop"}
            finally:
                cancelled.set()

        service = self.start(backend)
        self.request(service, "/generate", self.tool_payload())
        service.stop()
        self.assertTrue(service.closed.wait(2))
        self.assertTrue(cancelled.is_set())

    def test_superseding_generation_closes_its_active_http_stream(self):
        started, cancelled = threading.Event(), threading.Event()

        async def backend(request, invoke):
            if not started.is_set():
                started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.set()
            yield {"type": "finish", "reason": "stop"}

        service = self.start(backend)
        connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=2)
        try:
            connection.request("POST", "/generate", body=json.dumps(self.payload()),
                               headers={"Authorization": "Bearer " + self.settings["service_token"]})
            response = connection.getresponse()
            self.assertTrue(started.wait(1))
            status, _ = self.request(service, "/generate", self.payload())
            self.assertEqual(status, 200)
            self.assertTrue(cancelled.is_set())
            self.assertIn("cancelled", json.loads(response.read())["message"])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
