"""Real loopback protocols only; no Internet notification traffic."""
import json
import socket
import socketserver
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from resetnotice.core import FetchFailure, PublicClient
from resetnotice.notify import SendFailure, Transport
from resetnotice.vault import Secrets
from tests.test_release import MemoryVault, NOW, configured, feed


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost simulated SMTP\r\n")
        while True:
            command = self.rfile.readline().strip().split(b" ", 1)[0].upper()
            if not command:
                return
            if command == b"EHLO":
                self.wfile.write(b"250-localhost\r\n250 SIZE 1000000\r\n")
            elif command in (b"MAIL", b"RCPT"):
                self.wfile.write(b"250 OK\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 End with dot\r\n")
                lines = []
                while True:
                    line = self.rfile.readline()
                    if line in (b".\r\n", b""):
                        break
                    lines.append(line)
                self.server.messages.append(b"".join(lines))
                if self.server.drop_ack:
                    return
                self.wfile.write(b"250 accepted by mock server\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"502 unsupported\r\n")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LoopbackTests(unittest.TestCase):
    def smtp_server(self, drop_ack=False):
        server = SMTPServer(("127.0.0.1", 0), SMTPHandler)
        server.messages, server.drop_ack = [], drop_ack
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = configured()
        config.update(smtp_host="127.0.0.1", smtp_port=server.server_address[1], smtp_tls="无 TLS", smtp_user="")
        return server, config

    def test_real_smtp_exchange_only_to_mock_server(self):
        server, config = self.smtp_server()
        result = Transport(Secrets(MemoryVault())).smtp(config, "one@example.invalid", "Synthetic test", "Data: codex-reset.com\nhttps://codex-reset.com/")
        self.assertEqual(len(server.messages), 1)
        self.assertIn(b"one@example.invalid", server.messages[0])
        self.assertIn("未确认", result)

    def test_real_smtp_accept_then_drop_confirmation_is_uncertain(self):
        server, config = self.smtp_server(drop_ack=True)
        with self.assertRaises(SendFailure) as caught:
            Transport(Secrets(MemoryVault())).smtp(config, "one@example.invalid", "Synthetic test", "body")
        self.assertEqual(len(server.messages), 1)
        self.assertTrue(caught.exception.uncertain)

    def test_real_http429_and_recovery_keep_identifying_header(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                calls.append(inner.headers.get("User-Agent"))
                if len(calls) == 1:
                    inner.send_response(429)
                    inner.send_header("Retry-After", "600")
                    inner.end_headers()
                else:
                    data = json.dumps(feed()).encode()
                    inner.send_response(200)
                    inner.send_header("Content-Length", str(len(data)))
                    inner.end_headers()
                    inner.wfile.write(data)
            def log_message(inner, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def local_opener(request, timeout):
            local = urllib.request.Request("http://127.0.0.1:" + str(server.server_address[1]) + "/mock", headers=dict(request.header_items()))
            return urllib.request.urlopen(local, timeout=timeout)
        client = PublicClient("mailto:author@example.invalid", local_opener)
        with self.assertRaises(FetchFailure) as caught:
            client.get("feed", NOW)
        self.assertEqual(caught.exception.delay, 600)
        result, _ = client.get("feed", NOW+600)
        self.assertFalse(result["stale"])
        self.assertTrue(all("CodexResetNotification/1.0" in header for header in calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
