"""Loopback-only HTTP service with a dedicated event loop and model tasks."""

import asyncio
import contextlib
import hmac
import http.client
import json
import threading
import time
import uuid


SERVICE_NAME = "pythona-local-llm"
PROTOCOL_VERSION = 3
MAX_BODY = 4 * 1024 * 1024


class ModelBusyError(RuntimeError):
    pass


class ModelRun:
    """Keep one SDK response alive across HTTP/native-tool round trips."""

    def __init__(self, service, request):
        self.service = service
        self.owner = request["owner"]
        self.id = uuid.uuid4().hex
        self.names = {tool["name"] for tool in request["tools"]}
        self.events = asyncio.Queue(maxsize=64)
        self.pending = {}
        self.awaiting = None
        self.attached = True
        self.closed = False
        self.touched = service.loop.time()
        self.response_seconds = service.settings["load_seconds"] if request["backend"] == "mlx_lm" else service.settings["request_seconds"]
        self.task = asyncio.create_task(self._produce(request))

    async def invoke(self, name, arguments):
        if self.closed or name not in self.names:
            raise RuntimeError("The tool is unavailable or its model request has ended")
        call_id = "localtool_" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = (name, future)
        try:
            await self.events.put({"type": "tool_request", "run_id": self.id,
                                   "id": call_id, "name": name, "input": arguments})
            result = await future
            # Preserve native failure metadata without interpreting result text.
            return json.dumps({"content": result["content"], "failed": result["failed"]}, ensure_ascii=False)
        finally:
            self.pending.pop(call_id, None)

    async def _produce(self, request):
        events = self.service.backend(request, self.invoke)
        try:
            finished = False
            async for event in events:
                if finished or event.get("type") not in ("text", "finish"):
                    raise ValueError("Invalid model event")
                finished = event["type"] == "finish"
                await self.events.put(event)
            if not finished:
                raise RuntimeError("The model stream ended without a finish event")
        except Exception as error:
            await self.events.put({"type": "error", "message": f"{type(error).__name__}: {error}"})
        finally:
            await events.aclose()
            for _, future in self.pending.values():
                future.cancel()

    async def cancel(self):
        if self.closed:
            await asyncio.gather(self.task, return_exceptions=True)
            return
        self.closed = True
        for _, future in self.pending.values():
            future.cancel()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # Wake an attached HTTP reader when another request supersedes this run.
        while not self.events.empty():
            self.events.get_nowait()
        self.events.put_nowait({"type": "error", "message": "The model request was cancelled"})


class LocalModelService:
    def __init__(self, settings, build_id, backend=None, status=None):
        self.settings = dict(settings)
        self.build_id = build_id
        self.mlx = MLXWorker()
        self.backend = backend or (lambda request, invoke: model_events(request, invoke, self.mlx))
        self.status = status or (lambda: {backend: model_status({"backend": backend}) for backend in ("apple_fm", "mlx_lm")})
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.error = None
        self.loop = None
        self.stop_event = None
        self.port = settings["port"]
        self.connections = set()
        self.active = 0
        self.run = None
        self.admitting = False

    def start(self):
        self.thread = threading.Thread(target=self._thread_main,
                                       name="Local model HTTP", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.ready.wait(0.05):
            if time.monotonic() >= deadline:
                self.stop()
                raise TimeoutError("Local service startup timed out")
        if self.error:
            raise self.error
        return self

    def stop(self):
        if self.loop is not None and not self.loop.is_closed() and self.stop_event is not None:
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.stop_event.set)

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            self.error = error
        finally:
            self.ready.set()
            self.closed.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        # Avoid global stdout/stderr: another run_python call may be capturing those streams.
        self.loop.set_exception_handler(lambda loop, context: None)
        self.stop_event = asyncio.Event()
        self.last_activity = self.loop.time()
        listener = await asyncio.start_server(self._accept, "127.0.0.1", self.port, limit=16384)
        self.port = listener.sockets[0].getsockname()[1]
        self.ready.set()
        try:
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.25)
                except TimeoutError:
                    now = self.loop.time()
                    run = self.run
                    if run is not None and not run.attached and now - run.touched >= self.settings["request_seconds"]:
                        await self._cancel_run(run)
                    if not self.active and self.run is None and not self.mlx.busy and now - self.last_activity >= self.settings["idle_seconds"]:
                        break
        finally:
            self.mlx.close()
            listener.close()
            await listener.wait_closed()
            tasks = list(self.connections)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.run is not None:
                await self._cancel_run(self.run)
            await self.mlx.wait_closed()

    async def _cancel_run(self, run):
        await run.cancel()
        if self.run is run:
            self.run = None

    async def _new_run(self, request):
        # Reserve admission before awaiting cancellation; new requests never queue here.
        if self.admitting:
            raise ModelBusyError("The local model is busy. Try again shortly.")
        self.admitting = True
        try:
            if self.run is not None:
                if self.run.owner != request["owner"]:
                    raise ModelBusyError("The local model is busy with another conversation. Wait for it to finish and try again.")
                await self._cancel_run(self.run)
            if self.mlx.busy:
                raise ModelBusyError("The previous model request is still stopping. Try again shortly.")
            self.run = ModelRun(self, request)
            return self.run
        finally:
            self.admitting = False

    def health(self):
        return {"service": SERVICE_NAME, "protocol": PROTOCOL_VERSION,
                "build": self.build_id, "port": self.port}

    async def _json_response(self, writer, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: application/json; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body)
        await writer.drain()

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        self.active += 1
        streaming = False
        run = None
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = raw.decode("iso-8859-1").split("\r\n")
            method, path, version = lines[0].split(" ")
            if version not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("Unsupported HTTP version")
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                if key in headers:
                    raise ValueError("Duplicate HTTP header")
                headers[key] = value.strip()
            expected = "Bearer " + self.settings["service_token"]
            if not hmac.compare_digest(headers.get("authorization", ""), expected):
                await self._json_response(writer, "401 Unauthorized", {"error": "Local service credentials do not match"})
                return
            if "transfer-encoding" in headers:
                raise ValueError("Requests require Content-Length")
            length = int(headers.get("content-length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise ValueError("Request body exceeds the limit")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=10)
            self.last_activity = self.loop.time()
            if method == "GET" and path == "/health":
                await self._json_response(writer, "200 OK", self.health())
            elif method == "GET" and path == "/status":
                await self._json_response(writer, "200 OK", self.status())
            elif method == "POST" and path == "/shutdown":
                await self._json_response(writer, "200 OK", {"stopped": True})
                self.stop_event.set()
            elif method == "POST" and path in ("/generate", "/resume"):
                request = json.loads(body)
                if path == "/generate":
                    request = self._validate_request(request)
                    run = await self._new_run(request)
                else:
                    run = self._resume(request)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson; charset=utf-8\r\n"
                             b"Cache-Control: no-store\r\nConnection: close\r\n\r\n")
                await writer.drain()
                streaming = True
                await self._stream_run(reader, writer, run)
            else:
                await self._json_response(writer, "404 Not Found", {"error": "Unknown endpoint"})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except ModelBusyError as error:
            await self._json_response(writer, "409 Conflict", {"error": str(error)})
        except Exception as error:
            if not streaming:
                with contextlib.suppress(ConnectionError):
                    await self._json_response(writer, "400 Bad Request", {"error": str(error)})
        finally:
            if run is not None:
                # Only a fully written tool handoff may survive this connection.
                if run.awaiting is None:
                    await self._cancel_run(run)
                run.attached = False
                run.touched = self.loop.time()
            self.active -= 1
            self.last_activity = self.loop.time()
            self.connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=1)

    def _validate_request(self, request):
        if not isinstance(request, dict) or request.get("backend") not in ("apple_fm", "mlx_lm"):
            raise ValueError("Unknown model backend")
        if request["backend"] == "mlx_lm" and (not isinstance(request.get("model_id"), str) or not request["model_id"].strip()):
            raise ValueError("Enter a Hugging Face model ID")
        if not isinstance(request, dict) or not isinstance(request.get("instructions"), str):
            raise ValueError("instructions must be a string")
        if not isinstance(request.get("owner"), str) or not 1 <= len(request["owner"]) <= 512:
            raise ValueError("Invalid conversation owner")
        if not isinstance(request.get("messages"), list) or not request["messages"]:
            raise ValueError("messages must be a nonempty array")
        if not isinstance(request.get("tools"), list):
            raise ValueError("tools must be an array")
        names = set()
        for tool in request["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("Invalid tool definition")
            if not tool["name"] or tool["name"] in names:
                raise ValueError("Tool names must be nonempty and unique")
            if not isinstance(tool.get("description", ""), str):
                raise ValueError("Tool descriptions must be strings")
            if not isinstance(tool.get("inputSchema"), dict):
                raise ValueError("Tool schemas must be objects")
            names.add(tool["name"])
        tokens = request.get("maximum_response_tokens", 800)
        if type(tokens) is not int or not 1 <= tokens <= 8192:
            raise ValueError("Invalid maximum_response_tokens")
        request["maximum_response_tokens"] = tokens
        return request

    def _resume(self, request):
        if not isinstance(request, dict):
            raise ValueError("Invalid tool result request")
        run = self.run
        if run is None or run.id != request.get("run_id") or run.closed or run.owner != request.get("owner"):
            raise ValueError("The model request has expired; send a new message to continue")
        if run.attached:
            raise ValueError("The model request already has an active connection")
        result = request.get("result")
        if not isinstance(result, dict) or result.get("id") != run.awaiting:
            raise ValueError("Tool result does not match the pending call")
        name, future = run.pending.get(run.awaiting, (None, None))
        if future is None or future.done() or result.get("name") != name:
            raise ValueError("The tool call has ended or its result was already received")
        if not isinstance(result.get("content"), str) or type(result.get("failed")) is not bool:
            raise ValueError("Invalid tool result content or failure flag")
        run.attached = True
        run.response_seconds = self.settings["request_seconds"]
        run.awaiting = None
        future.set_result(result)
        return run

    async def _stream_run(self, reader, writer, run):
        async def emit(event):
            writer.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()

        async def pump():
            while True:
                event = await run.events.get()
                await emit(event)
                if event["type"] in ("tool_request", "finish", "error"):
                    return event

        output = asyncio.create_task(pump())
        disconnected = asyncio.create_task(reader.read(1))
        try:
            done, _ = await asyncio.wait((output, disconnected),
                                         timeout=run.response_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                return
            if output not in done:
                await emit({"type": "error", "message": "The model request timed out and was cancelled"})
                return
            event = await output
            if event["type"] == "tool_request":
                # The next JS runtime returns the result to this same SDK response.
                run.awaiting = event["id"]
        except Exception as error:
            with contextlib.suppress(ConnectionError):
                await emit({"type": "error", "message": str(error)})
        finally:
            output.cancel()
            disconnected.cancel()
            await asyncio.gather(output, disconnected, return_exceptions=True)


def service_json(settings, path, method="GET", timeout=1):
    connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=timeout)
    try:
        connection.request(method, path, headers={"Authorization": "Bearer " + settings["service_token"]})
        response = connection.getresponse()
        content = response.read(65536)
        if response.status != 200:
            raise RuntimeError(f"Local service returned HTTP {response.status}: {content.decode('utf-8', 'replace')}")
        return json.loads(content)
    finally:
        connection.close()


def start_service(settings, build_id):
    """Return after startup; the worker retains the backend without occupying pythonThread."""
    try:
        existing = service_json(settings, "/health")
    except (ConnectionError, OSError, http.client.HTTPException):
        existing = None
    if existing is not None:
        if existing.get("service") != SERVICE_NAME or existing.get("protocol") != PROTOCOL_VERSION:
            raise RuntimeError("The endpoint is not this project's model service")
        if existing.get("build") == build_id:
            return existing
        service_json(settings, "/shutdown", method="POST")
    deadline = time.monotonic() + 5
    while True:
        try:
            return LocalModelService(settings, build_id).start().health()
        except OSError:
            # Resolve concurrent startup through the listener, not run_python globals.
            try:
                existing = service_json(settings, "/health")
                if existing.get("service") == SERVICE_NAME and existing.get("build") == build_id:
                    return existing
            except (ConnectionError, OSError, http.client.HTTPException):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("The port is busy or the previous service has not exited; try again")
            time.sleep(0.05)
