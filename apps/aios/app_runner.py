"""Sandboxed runner for published single-file AIOS applications."""

from __future__ import annotations

import json
import os
import re
import signal
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
    _timestamp_key,
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
_CLEANUP_WAIT_SECONDS = 5


class _TerminationSignal(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.exit_code = 128 + signum


def _raise_termination(signum: int, _frame: object) -> None:
    raise _TerminationSignal(signum)


def _termination_signals() -> tuple[int, ...]:
    signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    return tuple(signals)


def _install_termination_handlers() -> dict[int, object]:
    previous_handlers: dict[int, object] = {}
    for signum in _termination_signals():
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _raise_termination)
    return previous_handlers


def _restore_termination_handlers(previous_handlers: dict[int, object]) -> None:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def _wait_for_process_exit(process: subprocess.Popen[bytes] | subprocess.Popen[str] | object) -> bool:
    try:
        process.wait(timeout=_CLEANUP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    except (OSError, ProcessLookupError):
        return True
    return True


def _terminate_process(process: subprocess.Popen[bytes] | subprocess.Popen[str] | object) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        if _wait_for_process_exit(process):
            return
        process.kill()
        _wait_for_process_exit(process)
    except Exception:
        pass


def _valid_published_manifest(folder: Path) -> dict[str, object]:
    manifest_path = folder / "manifest.json"
    data = _load_json(manifest_path)
    if not isinstance(data, dict):
        raise ValueError(_GENERIC_LOAD_ERROR)

    legacy_required = {
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
    new_required = legacy_required | {"runtime"}
    if set(data) not in (legacy_required, new_required):
        raise ValueError(_GENERIC_LOAD_ERROR)
    if "runtime" in data and data["runtime"] != "web":
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
    _timestamp_key(data["created_at"])
    _timestamp_key(data["updated_at"])
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


def handler_for(document: str, on_app_loaded=None) -> type[BaseHTTPRequestHandler]:
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
    callback_lock = threading.Lock()
    callback_invoked = False

    def notify_app_loaded() -> None:
        nonlocal callback_invoked
        if on_app_loaded is None:
            return
        with callback_lock:
            if callback_invoked:
                return
            callback_invoked = True
        on_app_loaded()

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
                self._send_document(app_bytes, _APP_CSP, send_body, notify=send_body)
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

        def _send_document(self, body: bytes, csp: str, send_body: bool, notify: bool = False) -> None:
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
                if notify:
                    self.wfile.flush()
                    notify_app_loaded()

    return ApplicationHandler


def run(folder: Path | str, ready_fd: int | None = None) -> None:
    ready_lock = threading.Lock()
    open_ready_fd = ready_fd

    def close_ready_fd() -> None:
        nonlocal open_ready_fd
        with ready_lock:
            fd = open_ready_fd
            open_ready_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def signal_ready() -> None:
        nonlocal open_ready_fd
        with ready_lock:
            fd = open_ready_fd
            open_ready_fd = None
        if fd is None:
            return
        try:
            os.write(fd, b"ready\n")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    server = None
    thread = None
    profile = None
    process = None
    try:
        document = load_document(folder)
        handler = handler_for(document, on_app_loaded=signal_ready if ready_fd is not None else None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
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
            start_new_session=False,
            close_fds=True,
        )
        process.wait()
    finally:
        if process is not None:
            _terminate_process(process)
        if profile is not None:
            profile.cleanup()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        close_ready_fd()


def _ready_fd_from_args(args: list[str]) -> int | None:
    if "--ready-fd" not in args:
        return None
    index = args.index("--ready-fd")
    if index + 1 >= len(args):
        return None
    try:
        value = int(args[index + 1])
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_args(args: list[str]) -> tuple[Path, int | None]:
    if len(args) == 1:
        return Path(args[0]), None
    if len(args) == 3 and args[1] == "--ready-fd":
        try:
            ready_fd = int(args[2])
        except ValueError:
            raise ValueError("invalid arguments") from None
        if ready_fd < 0:
            raise ValueError("invalid arguments")
        return Path(args[0]), ready_fd
    raise ValueError("invalid arguments")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cleanup_fd = _ready_fd_from_args(args)
    try:
        folder, ready_fd = _parse_args(args)
    except ValueError:
        if cleanup_fd is not None:
            try:
                os.close(cleanup_fd)
            except OSError:
                pass
        print("Usage: python -m aios.app_runner FOLDER [--ready-fd FD]", file=sys.stderr)
        return 2
    try:
        previous_handlers = _install_termination_handlers()
    except BaseException:
        if ready_fd is not None:
            try:
                os.close(ready_fd)
            except OSError:
                pass
        raise
    try:
        try:
            run(folder, ready_fd=ready_fd)
        except _TerminationSignal as interrupted:
            return interrupted.exit_code
        except KeyboardInterrupt:
            return 130
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            print(_GENERIC_RUN_ERROR, file=sys.stderr)
            return 1
        return 0
    finally:
        _restore_termination_handlers(previous_handlers)


if __name__ == "__main__":
    raise SystemExit(main())
