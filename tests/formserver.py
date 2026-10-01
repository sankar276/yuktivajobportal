"""A tiny local web server that plays the part of an applicant-tracking system."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

FORMS = Path(__file__).parent / "fixtures" / "forms"
CONFIRMATION = (
    "<!doctype html><html><head><title>Application received</title></head><body>"
    "<h1>Thank you for applying</h1><p>Your application has been received.</p></body></html>"
)


@dataclass
class Submission:
    path: str
    fields: dict[str, list[str]] = field(default_factory=dict)
    #: name -> (filename, size in bytes)
    files: dict[str, tuple[str, int]] = field(default_factory=dict)

    def first(self, name: str) -> str | None:
        values = self.fields.get(name)
        return values[0] if values else None


class FormServer:
    def __init__(self) -> None:
        self.submissions: list[Submission] = []
        self.requests: list[tuple[str, str]] = []
        self.robots: str | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # keep test output quiet
                return

            def _send(
                self, status: int, body: str, content_type: str = "text/html; charset=utf-8"
            ) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                owner.requests.append(("GET", path))
                if path == "/robots.txt":
                    if owner.robots is None:
                        self._send(404, "not found", "text/plain")
                    else:
                        self._send(200, owner.robots, "text/plain")
                    return
                if path.endswith(".js"):
                    self._send(200, "/* stub */", "application/javascript")
                    return
                target = FORMS / path.lstrip("/")
                if target.is_file() and target.parent == FORMS:
                    self._send(200, target.read_text(encoding="utf-8"))
                else:
                    self._send(404, "not found", "text/plain")

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                owner.requests.append(("POST", path))
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                submission = Submission(path=path)
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("multipart/form-data"):
                    message = BytesParser(policy=HTTP).parsebytes(
                        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
                    )
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        filename = part.get_filename()
                        payload = part.get_payload(decode=True) or b""
                        if filename is not None:
                            if filename:
                                submission.files[str(name)] = (filename, len(payload))
                        else:
                            submission.fields.setdefault(str(name), []).append(
                                payload.decode("utf-8")
                            )
                else:
                    for name, values in parse_qs(
                        body.decode("utf-8"), keep_blank_values=True
                    ).items():
                        submission.fields[name] = values
                owner.submissions.append(submission)
                if path == "/submit":
                    self._send(200, CONFIRMATION)
                elif path == "/silent":
                    self._send(200, (FORMS / "silent.html").read_text(encoding="utf-8"))
                else:
                    self._send(200, "<html><body><form><input name='step2'></form></body></html>")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def posts(self) -> list[Submission]:
        return list(self.submissions)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
