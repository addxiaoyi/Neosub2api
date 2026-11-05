"""测试用假 S3：只实现远端备份会用到的 PUT/HEAD/GET(list)/DELETE。

放在独立模块里，供上传与调度两份测试共用；不支持的功能一律 405，避免悄悄放过。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

BUCKET = "test-bucket"


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class FakeS3:
    def __init__(self, bucket: str = BUCKET) -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []
        self.fail_puts = 0
        self.forced_error: tuple[int, str, str] | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _respond(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD" and body:
                    self.wfile.write(body)

            def _error(self, status: int, code: str, message: str) -> None:
                body = (
                    f'<?xml version="1.0"?><Error><Code>{_xml_escape(code)}</Code>'
                    f"<Message>{_xml_escape(message)}</Message></Error>"
                ).encode()
                self._respond(status, body, {"Content-Type": "application/xml"})

            def _parts(self) -> tuple[str, str, dict[str, list[str]]]:
                parsed = urlsplit(self.path)
                path = unquote(parsed.path).lstrip("/")
                bucket, _, key = path.partition("/")
                return bucket, key, parse_qs(parsed.query, keep_blank_values=True)

            def _body(self) -> bytes:
                size = int(self.headers.get("Content-Length", "0") or 0)
                return self.rfile.read(size) if size else b""

            def _dispatch(self) -> None:
                fake.requests.append((self.command, self.path))
                bucket, key, query = self._parts()
                # 先把请求体读干净：出错时不读体会让 keep-alive 连接里的下一个请求错位
                body = self._body() if self.command == "PUT" else b""
                if fake.forced_error is not None:
                    self._error(*fake.forced_error)
                    return
                if bucket != fake.bucket:
                    self._error(404, "NoSuchBucket", "bucket not found")
                    return
                if self.command == "PUT":
                    if fake.fail_puts > 0:
                        fake.fail_puts -= 1
                        self._error(503, "SlowDown", "please retry")
                        return
                    fake.bodies.append(body)
                    meta = {
                        name.lower()[len("x-amz-meta-") :]: value
                        for name, value in self.headers.items()
                        if name.lower().startswith("x-amz-meta-")
                    }
                    fake.objects[key] = (body, meta)
                    self._respond(200, b"", {"ETag": '"fake-etag"'})
                    return
                if self.command == "HEAD":
                    if key not in fake.objects:
                        self._error(404, "NoSuchKey", "not found")
                        return
                    body, meta = fake.objects[key]
                    headers = {
                        "Content-Length": str(len(body)),
                        "Last-Modified": "Mon, 05 Nov 2026 00:00:00 GMT",
                    }
                    headers.update({f"x-amz-meta-{name}": value for name, value in meta.items()})
                    self.send_response(200)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    return
                if self.command == "DELETE":
                    fake.objects.pop(key, None)
                    self._respond(204)
                    return
                if self.command == "GET" and query.get("list-type") == ["2"]:
                    prefix = query.get("prefix", [""])[0]
                    rows = [
                        f"<Contents><Key>{_xml_escape(name)}</Key><Size>{len(body)}</Size>"
                        f"<LastModified>2026-11-05T00:00:00Z</LastModified></Contents>"
                        for name, (body, _meta) in sorted(fake.objects.items())
                        if name.startswith(prefix)
                    ]
                    body = (
                        '<?xml version="1.0"?><ListBucketResult '
                        'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        "<IsTruncated>false</IsTruncated>" + "".join(rows) + "</ListBucketResult>"
                    ).encode()
                    self._respond(200, body, {"Content-Type": "application/xml"})
                    return
                if self.command == "GET":
                    if key not in fake.objects:
                        self._error(404, "NoSuchKey", "not found")
                        return
                    self._respond(200, fake.objects[key][0])
                    return
                self._error(405, "MethodNotAllowed", "unsupported")

            do_GET = _dispatch
            do_PUT = _dispatch
            do_HEAD = _dispatch
            do_DELETE = _dispatch

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "FakeS3":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
