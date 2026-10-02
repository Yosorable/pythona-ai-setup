"""Bounded SSE consumption shared by the two remote setup tests."""

import asyncio
from http.client import HTTPConnection, HTTPSConnection
import io
import json
import queue
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPHandler, HTTPSHandler, build_opener


def request_test_response(request, check, deadline, error_message=None):
    """Read one test on a short-lived worker so quiet streams remain cancellable."""
    check()
    stopped = threading.Event()
    outcome = queue.Queue(maxsize=1)
    active_socket = []

    def connection(connection_type, host, **kwargs):
        value = connection_type(host, **kwargs)
        connect = value.connect

        def tracked_connect():
            connect()
            active_socket[:] = [value.sock]
            if stopped.is_set():
                value.close()
                raise asyncio.CancelledError
        value.connect = tracked_connect
        return value

    class TestHTTPHandler(HTTPHandler):
        def http_open(self, request):
            return self.do_open(lambda host, **kwargs: connection(HTTPConnection, host, **kwargs), request)

    class TestHTTPSHandler(HTTPSHandler):
        def https_open(self, request):
            return self.do_open(lambda host, **kwargs: connection(HTTPSConnection, host, **kwargs), request, context=self._context)

    def read():
        try:
            # Keep urllib's proxy and TLS handling, adding only socket ownership
            # for cancellation of this request.
            opener = build_opener(TestHTTPHandler(), TestHTTPSHandler())
            try:
                response = opener.open(request, timeout=max(0.01, deadline - time.monotonic()))
            except HTTPError as error:
                with error:
                    body = error.read(131072)
                raise HTTPError(error.url, error.code, error.reason, error.headers, io.BytesIO(body)) from None
            value = read_test_response(response, check, error_message)
        except BaseException as error:
            outcome.put((None, error))
        else:
            outcome.put((value, None))

    worker = threading.Thread(target=read, name="Cloud model test response", daemon=True)
    worker.start()
    try:
        while True:
            check()
            try:
                value, error = outcome.get(timeout=0.1)
            except queue.Empty:
                continue
            check()
            if error is not None:
                raise error
            return value
    finally:
        stopped.set()
        # Shutdown wakes a blocked header/body read without waiting on its buffer
        # lock. The worker owns closing the response and connection afterwards.
        if active_socket:
            try:
                active_socket[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        worker.join(timeout=0.1)


def read_test_response(response, check, error_message=None):
    text, complete, pending, size = "", False, [], 0
    with response:
        while True:
            check()
            line = response.readline(1048577)
            size += len(line)
            if size > 4 * 1024 * 1024:
                raise RuntimeError("The model test response is too large")
            if not line:
                break
            line = line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                pending.append(line[5:].lstrip())
            elif not line and pending:
                data, pending = "\n".join(pending), []
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if event.get("type") in ("response.output_text.delta", "response.refusal.delta"):
                    text += event.get("delta", "")
                elif event.get("type") == "response.completed":
                    complete = True
                    if not text:
                        text = "".join(part.get("refusal", "") if part.get("type") == "refusal" else part.get("text", "")
                                       for item in event.get("response", {}).get("output", [])
                                       for part in item.get("content", []) if part.get("type") in ("output_text", "refusal"))
                    break
                elif event.get("type") in ("error", "response.failed", "response.incomplete"):
                    if error_message is not None:
                        raise error_message(event)
                    raise RuntimeError("Codex did not complete the model test")
    check()
    if not complete or not text.strip():
        raise RuntimeError("No complete model response was received")
    return text
