"""Real socket checks for quiet streams, cancellation, and absolute deadlines."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import patch

from ai_setup.responses_probe import read_test_response, request_test_response


def event(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


class ResponseProbeTests(unittest.TestCase):
    def server(self, respond, tls=None):
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    respond(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    errors.append(error)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls is not None:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(1)
            self.assertEqual(errors, [])
        self.addCleanup(close)
        return Request(("https" if tls else "http") + "://127.0.0.1:" + str(server.server_address[1]) + "/responses", data=b"{}")

    def run_request(self, request, cancelled=None, seconds=30):
        deadline = time.monotonic() + seconds

        def check():
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError
            if time.monotonic() >= deadline:
                raise TimeoutError("Test deadline")
        return request_test_response(request, check, deadline)

    @staticmethod
    def headers(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()

    def test_quiet_reasoning_longer_than_fifteen_seconds_can_complete(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def respond(handler):
            self.headers(handler)
            handler.wfile.write(event({"type": "response.created"}))
            handler.wfile.flush()
            release.wait(16)
            handler.wfile.write(event({"type": "response.output_text.delta", "delta": "Ready"}))
            handler.wfile.write(event({"type": "response.completed"}))
            handler.wfile.flush()

        self.assertEqual(self.run_request(self.server(respond)), "Ready")

    def test_cancel_interrupts_waiting_for_headers_and_body(self):
        for send_headers in (False, True):
            with self.subTest(send_headers=send_headers):
                cancelled, release, entered = threading.Event(), threading.Event(), threading.Event()
                self.addCleanup(release.set)

                def respond(handler):
                    if send_headers:
                        self.headers(handler)
                        handler.wfile.write(event({"type": "response.created"}))
                        handler.wfile.flush()
                    entered.set()
                    timer = threading.Timer(0.1, cancelled.set)
                    timer.start()
                    release.wait(3)
                    timer.cancel()

                request = self.server(respond)
                started = time.monotonic()
                try:
                    with self.assertRaises(asyncio.CancelledError):
                        self.run_request(request, cancelled)
                    self.assertTrue(entered.is_set())
                    self.assertLess(time.monotonic() - started, 1)
                finally:
                    release.set()

    def test_heartbeats_do_not_extend_the_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def respond(handler):
            self.headers(handler)
            while not release.wait(0.02):
                handler.wfile.write(b"\n")
                handler.wfile.flush()

        request = self.server(respond)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "Test deadline"):
            self.run_request(request, seconds=0.2)
        release.set()
        self.assertLess(time.monotonic() - started, 1)

    def test_http_error_retains_status_and_bounded_body_for_auth_recovery(self):
        def respond(handler):
            handler.send_response(401)
            handler.end_headers()
            handler.wfile.write(b'{"error":{"code":"invalid_token"}}')

        with self.assertRaises(HTTPError) as caught:
            self.run_request(self.server(respond))
        with caught.exception as error:
            self.assertEqual(error.code, 401)
            self.assertEqual(json.loads(error.read())["error"]["code"], "invalid_token")

    def test_https_cancellation_closes_the_connection(self):
        from datetime import datetime, timedelta, timezone
        from ipaddress import ip_address
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
                       .sign(key, hashes.SHA256()))
        cert_path, key_path = Path(folder.name) / "cert.pem", Path(folder.name) / "key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(cert_path, key_path)
        client_tls = ssl.create_default_context(cafile=str(cert_path))
        cancelled, disconnected = threading.Event(), threading.Event()

        def respond(handler):
            self.headers(handler)
            handler.wfile.write(event({"type": "response.created"}))
            handler.wfile.flush()
            timer = threading.Timer(0.1, cancelled.set)
            timer.start()
            handler.connection.settimeout(2)
            try:
                if not handler.connection.recv(1):
                    disconnected.set()
            finally:
                timer.cancel()

        request = self.server(respond, server_tls)
        with patch("ssl._create_default_https_context", return_value=client_tls):
            with self.assertRaises(asyncio.CancelledError):
                self.run_request(request, cancelled)
        self.assertTrue(disconnected.wait(1))

    def test_refusal_is_a_visible_completed_response_with_or_without_deltas(self):
        text = "I cannot help with that request."
        terminal = {"type": "response.completed", "response": {"output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": text}]}]}}
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                values = ([{"type": "response.refusal.delta", "delta": text}] if streamed else []) + [terminal]
                stream = io.BytesIO(b"".join(event(value) for value in values))
                self.assertEqual(read_test_response(stream, lambda: None), text)
