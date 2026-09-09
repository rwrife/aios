"""Sandboxed runner for published single-file AIOS applications."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .applications import (
    DOCTYPE_RE,
    ID_RE,
    MAX_HTML_BYTES,
    MAX_KEYWORDS,
    MAX_REQUEST_LENGTH,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    _is_regular_dir,
    _is_regular_file,
    _load_json,
    _read_regular_file_bytes,
    _sha256_bytes,
)

_APP_PATH = "/app"
_WRAPPER_TITLE = "AIOS application preview"
_CHROMIUM_BIN = "chromium"
_HOST_RESOLVER_RULES = "MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE ::1"
_WRAPPER_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; "
    "base-uri 'none'; object-src 'none'; form-action 'none'"
)
_APP_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; font-src 'none'; media-src 'none'; "
    "object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
)
_GENERIC_LOAD_ERROR = "Choose a published application directory."
_GENERIC_RUN_ERROR = "Application runner failed."


def _valid_published_manifest(folder: Path) -> dict[str, object]:
    manifest_path = folder / "manifest.json"
    data = _load_json(manifest_path)
    if not isinstance(data, dict):
        raise ValueError(_GENERIC_LOAD_ERROR)

    required = {
        "id",
        "title",
        "request",
        "created_at",
        "updated_at",
        "summary",
        "keywords",
        "entrypoint",
        "sha256",
    }
    if set(data) != required:
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["id"], str) or not ID_RE.fullmatch(data["id"]) or data["id"] != folder.name:
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["title"], str) or not (1 <= len(data["title"]) <= MAX_TITLE_LENGTH):
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["request"], str) or not (1 <= len(data["request"]) <= MAX_REQUEST_LENGTH):
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["created_at"], str) or not data["created_at"]:
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["updated_at"], str) or not data["updated_at"]:
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["summary"], str) or not (1 <= len(data["summary"]) <= MAX_SUMMARY_LENGTH):
        raise ValueError(_GENERIC_LOAD_ERROR)
    if data["entrypoint"] != "index.html":
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
        raise ValueError(_GENERIC_LOAD_ERROR)
    if not isinstance(data["keywords"], list) or len(data["keywords"]) > MAX_KEYWORDS:
        raise ValueError(_GENERIC_LOAD_ERROR)

    keywords: list[str] = []
    for keyword in data["keywords"]:
        if not isinstance(keyword, str) or not re.fullmatch(r"[a-z0-9]+", keyword):
            raise ValueError(_GENERIC_LOAD_ERROR)
        keywords.append(keyword)
    if keywords != sorted(set(keywords)):
        raise ValueError(_GENERIC_LOAD_ERROR)
    return data


def _validate_published_folder(folder: Path) -> None:
    allowed = {"index.html", "manifest.json"}
    for entry in folder.iterdir():
        if entry.name not in allowed or entry.is_symlink() or not _is_regular_file(entry):
            raise ValueError(_GENERIC_LOAD_ERROR)


def load_document(folder: Path | str) -> str:
    path = Path(folder).expanduser()
    try:
        if not _is_regular_dir(path):
            raise ValueError(_GENERIC_LOAD_ERROR)
        _validate_published_folder(path)
        manifest = _valid_published_manifest(path)
        index_path = path / str(manifest["entrypoint"])
        html_bytes = _read_regular_file_bytes(index_path, MAX_HTML_BYTES)
        if _sha256_bytes(html_bytes) != manifest["sha256"]:
            raise ValueError(_GENERIC_LOAD_ERROR)
        document = html_bytes.decode("utf-8")
        if not DOCTYPE_RE.match(document):
            raise ValueError(_GENERIC_LOAD_ERROR)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(_GENERIC_LOAD_ERROR) from error
    return document


def handler_for(document: str) -> type[BaseHTTPRequestHandler]:
    wrapper_html = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{_WRAPPER_TITLE}</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>"
        "html,body{margin:0;height:100%;background:#0f172a;color:#e2e8f0;font:16px/1.4 system-ui,sans-serif;}"
        "body{display:grid;grid-template-rows:auto 1fr;}"
        "header{padding:16px 20px;border-bottom:1px solid rgba(148,163,184,.24);}"
        "iframe{width:100%;height:100%;border:0;background:#fff;}"
        "</style></head><body>"
        f"<header><h1>{_WRAPPER_TITLE}</h1></header>"
        '<iframe sandbox="allow-scripts" src="/app" title="Generated application preview"></iframe>'
        "</body></html>"
    )
    wrapper_bytes = wrapper_html.encode("utf-8")
    app_bytes = document.encode("utf-8")

    class ApplicationHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
            return

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve(True)

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve(False)

        def _serve(self, send_body: bool) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self._send_not_found(send_body)
                return
            if parsed.path == "/":
                self._send_document(wrapper_bytes, _WRAPPER_CSP, send_body)
                return
            if parsed.path == _APP_PATH:
                self._send_document(app_bytes, _APP_CSP, send_body)
                return
            self._send_not_found(send_body)

        def _send_not_found(self, send_body: bool) -> None:
            body = b"Not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; base-uri 'none'; object-src 'none'; form-action 'none'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _send_document(self, body: bytes, csp: str, send_body: bool) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

    return ApplicationHandler


def run(folder: Path | str) -> None:
    document = load_document(folder)
    handler = handler_for(document)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = None
    process = None
    try:
        profile = tempfile.TemporaryDirectory(prefix="aios-chromium-")
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        command = [
            _CHROMIUM_BIN,
            f"--app={url}",
            f"--user-data-dir={profile.name}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            f"--host-resolver-rules={_HOST_RESOLVER_RULES}",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        process.wait()
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
        if profile is not None:
            profile.cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: python -m aios.app_runner FOLDER", file=sys.stderr)
        return 2
    try:
        run(Path(args[0]))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        print(_GENERIC_RUN_ERROR, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
