"""Run setup probes through the same HTTP backend used by the provider."""

import asyncio
import contextlib
import json
import time
import uuid

from .bundle import backend_bundle, load_backend


async def generate_probe(settings, prompt):
    reader, writer = await asyncio.open_connection("127.0.0.1", settings["port"])
    try:
        body = json.dumps({"owner": "probe_" + uuid.uuid4().hex,
                           "backend": settings["backend"], "model_id": settings["model_id"],
                           "instructions": "Answer briefly in the user's language.",
                           "messages": [{"role": "user", "content": prompt}], "tools": [],
                           "maximum_response_tokens": 256}, ensure_ascii=False).encode("utf-8")
        headers = (f"POST /generate HTTP/1.1\r\nHost: 127.0.0.1:{settings['port']}\r\n"
                   f"Authorization: Bearer {settings['service_token']}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                   "Connection: close\r\n\r\n")
        writer.write(headers.encode("ascii") + body)
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        if response.split(b" ", 2)[1] != b"200":
            raise RuntimeError((await reader.read()).decode("utf-8", "replace"))
        text = ""
        finished = False
        memory = None
        while line := await reader.readline():
            if not line.strip():
                continue
            event = json.loads(line)
            if event["type"] == "error":
                raise RuntimeError(event["message"])
            if event["type"] == "text":
                text += event["delta"]
            if event["type"] == "finish":
                finished = True
                memory = event.get("memory")
        if not finished or not text.strip():
            raise RuntimeError("No complete model response was received")
        return {"text": text, "memory": memory}
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def test_model(settings, prompt, cancelled):
    namespace = load_backend()
    _, build_id = backend_bundle()
    namespace["start_service"](settings, build_id)

    async def run():
        task = asyncio.create_task(generate_probe(settings, prompt))
        limit = settings["load_seconds"] if settings["backend"] == "mlx_lm" else settings["request_seconds"]
        deadline = asyncio.get_running_loop().time() + limit + 5
        try:
            while not task.done():
                if cancelled.is_set():
                    raise asyncio.CancelledError
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("The model test timed out")
                await asyncio.wait((task,), timeout=0.1)
            return await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    started = time.monotonic()
    answer = asyncio.run(run())
    return {**answer, "seconds": time.monotonic() - started}
