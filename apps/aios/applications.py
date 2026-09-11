"""Persistent cache for sandboxed web and trusted native applications."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import core

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{8}$")
TOKEN_RE = re.compile(r"[a-z0-9]+")
DOCTYPE_RE = re.compile(r"^\s*<!doctype html\b", re.IGNORECASE)
MAX_HTML_BYTES = 16 * 1024
MAX_TITLE_LENGTH = 100
MAX_REQUEST_LENGTH = 2000
MAX_SUMMARY_LENGTH = 300
MAX_KEYWORDS = 20
NATIVE_TEMPLATES = ("calculator",)
RUNTIMES = ("web", "native")
LAUNCH_FAILURE_REASON = "Application window could not open."

APPLICATION_TOOL = {
    "type": "function",
    "function": {
        "name": "application",
        "description": (
            "Create and launch desktop applications for this chat, with sandboxed single-file HTML fallback. "
            "For an app creation request, create then launch (write HTML first for web apps) in the same turn. "
            "Drafts can launch without publishing. Publish only when the user explicitly requests publication "
            "to the reusable cache. Launched apps close when this chat stops or closes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "create", "read", "write", "publish", "launch"],
                },
                "query": {
                    "type": "string",
                    "description": "Search query for finding existing applications.",
                },
                "id": {
                    "type": "string",
                    "description": "Application identifier.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title for a new application.",
                },
                "request": {
                    "type": "string",
                    "description": "Normalized source request for a new application.",
                },
                "html": {
                    "type": "string",
                    "description": "Complete HTML document for the cached application.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short published summary.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_KEYWORDS,
                    "description": "Keyword list used for search and publication metadata.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def application_tool(native_templates: Iterable[str] = ()) -> dict[str, Any]:
    definition = copy.deepcopy(APPLICATION_TOOL)
    templates = tuple(native_templates)
    if templates:
        properties = definition["function"]["parameters"]["properties"]
        properties["runtime"] = {
            "type": "string",
            "enum": list(RUNTIMES),
            "description": "Application runtime. Use native only with an advertised native template.",
        }
        properties["template"] = {
            "type": "string",
            "enum": list(templates),
            "description": "Trusted native application template.",
        }
    return definition


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_title(value: str) -> str:
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x1F or codepoint == 0x7F:
            raise ValueError("Enter a title without control characters.")
    title = _normalize_text(value)
    if not (1 <= len(title) <= MAX_TITLE_LENGTH):
        raise ValueError("Enter a title between 1 and 100 characters.")
    return title


def _normalize_request(value: str) -> str:
    request = _normalize_text(value)
    if not request:
        raise ValueError("Enter a non-empty request.")
    if len(request) > MAX_REQUEST_LENGTH:
        raise ValueError("Keep the request under 2000 characters.")
    return request


def _normalize_summary(value: str) -> str:
    summary = _normalize_text(value)
    if not summary or len(summary) > MAX_SUMMARY_LENGTH:
        raise ValueError("Enter a summary between 1 and 300 characters.")
    return summary


def _slugify(value: str) -> str:
    tokens = TOKEN_RE.findall(value.lower())
    slug = "-".join(tokens).strip("-")
    if not slug:
        raise ValueError("Enter a title with at least one alphanumeric character.")
    return slug


def _application_id(title: str) -> str:
    return f"{_slugify(title)}-{secrets.token_hex(4)}"


def _normalize_keywords(values: Iterable[Any]) -> list[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Keywords must be strings.")
        tokens = TOKEN_RE.findall(_normalize_text(value).lower())
        if not tokens:
            raise ValueError("Keywords must contain alphanumeric text.")
        for token in tokens:
            normalized.add(token)
    ordered = sorted(normalized)
    if len(ordered) > MAX_KEYWORDS:
        raise ValueError("Keep the keyword list to 20 tokens or fewer.")
    return ordered


def _tokenize(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def _is_valid_id(value: str) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def _is_regular_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def _is_regular_dir(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp_key(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _atomic_write_bytes(path: Path, data: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        temp.replace(path)
        os.chmod(path, 0o600)
        return len(data)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, text: str) -> int:
    data = text.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        raise ValueError("Keep the HTML document at 16 KiB or less.")
    return _atomic_write_bytes(path, data)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _atomic_write_bytes(path, data)


def _ensure_secure_directory_fallback(path: Path) -> None:
    absolute_path = Path(os.path.abspath(path))
    parts = absolute_path.parts
    if not absolute_path.is_absolute() or not parts:
        raise ValueError("Choose a real application root directory.")

    def ensure_component(directory: Path, final: bool) -> None:
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                try:
                    mode = directory.lstat().st_mode
                except FileNotFoundError as error:
                    raise ValueError("Choose a real application root directory.") from error
            except OSError as error:
                raise ValueError("Choose a real application root directory.") from error
            else:
                directory.chmod(0o700)
                return
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("Choose a real application root directory.")
        if final:
            directory.chmod(0o700)

    current = Path(parts[0])
    for component in parts[1:]:
        ensure_component(current, current == absolute_path)
        current = current / component
    ensure_component(current, True)


def _supports_descriptor_safe_directories() -> bool:
    return (
        sys.platform.startswith("linux")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
        and hasattr(os, "supports_dir_fd")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    )


def _ensure_secure_directory_linux(path: Path) -> None:
    absolute_path = Path(os.path.abspath(path))
    parts = absolute_path.parts
    if not absolute_path.is_absolute() or not parts:
        raise ValueError("Choose a real application root directory.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd: int | None = None
    child_fd: int | None = None
    try:
        try:
            current_fd = os.open(parts[0], flags)
        except OSError as error:
            raise ValueError("Choose a real application root directory.") from error
        remaining = parts[1:]
        if not remaining:
            if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
                raise ValueError("Choose a real application root directory.")
            os.fchmod(current_fd, 0o700)
            return
        for index, component in enumerate(remaining):
            created = False
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ValueError("Choose a real application root directory.") from error
                try:
                    child_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as error:
                    raise ValueError("Choose a real application root directory.") from error
            except OSError as error:
                raise ValueError("Choose a real application root directory.") from error
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    raise ValueError("Choose a real application root directory.")
                if created or index == len(remaining) - 1:
                    os.fchmod(child_fd, 0o700)
            except Exception:
                os.close(child_fd)
                child_fd = None
                raise
            os.close(current_fd)
            current_fd = child_fd
            child_fd = None
    finally:
        if child_fd is not None:
            os.close(child_fd)
        if current_fd is not None:
            os.close(current_fd)


def _ensure_secure_directory(path: Path) -> None:
    if _supports_descriptor_safe_directories():
        _ensure_secure_directory_linux(path)
        return
    _ensure_secure_directory_fallback(path)


def _read_regular_file_bytes(path: Path, max_bytes: int) -> bytes:
    if hasattr(os, "O_NOFOLLOW"):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise ValueError("Unexpected application files found.") from error
        with os.fdopen(fd, "rb", closefd=True) as stream:
            stat_result = os.fstat(stream.fileno())
            if not stat.S_ISREG(stat_result.st_mode):
                raise ValueError("Unexpected application files found.")
            if stat_result.st_size > max_bytes:
                raise ValueError("The file is too large.")
            data = stream.read(stat_result.st_size)
        if len(data) != stat_result.st_size:
            raise ValueError("Unexpected application files found.")
        return data
    if path.is_symlink() or not path.is_file():
        raise ValueError("Unexpected application files found.")
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError("The file is too large.")
    return data


class ApplicationStore:
    def __init__(
        self,
        root: Path | str | None = None,
        launcher: Callable[[Path], Any] | None = None,
        native_host: Path | str | None = None,
        native_templates: Iterable[str] | None = None,
        launch_timeout: float = 30,
    ):
        self.root = Path(root) if root is not None else core.data_dir() / "applications"
        self.root = self.root.expanduser()
        _ensure_secure_directory(self.root)
        self.launcher = launcher
        if native_host is None:
            discovered_host = os.environ.get("AIOS_APP_HOST") or "/usr/local/bin/aios-app-host"
        else:
            discovered_host = os.fspath(native_host)
        self.native_host = Path(discovered_host).expanduser() if discovered_host else None
        host_available = self.native_host is not None and _is_regular_file(self.native_host) and os.access(self.native_host, os.X_OK)
        if native_templates is None:
            configured_templates = NATIVE_TEMPLATES if host_available else ()
        else:
            if isinstance(native_templates, (str, bytes)):
                raise ValueError("Choose supported native application templates.")
            requested = tuple(native_templates)
            if any(template not in NATIVE_TEMPLATES for template in requested):
                raise ValueError("Choose supported native application templates.")
            if requested and not host_available:
                raise ValueError("Native application templates require an executable native host.")
            configured_templates = tuple(template for template in NATIVE_TEMPLATES if template in requested)
        self.native_templates = configured_templates
        try:
            self.launch_timeout = float(launch_timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Choose a valid application launch timeout.") from None
        if self.launch_timeout < 0 or not math.isfinite(self.launch_timeout):
            raise ValueError("Choose a valid application launch timeout.")
        self._lifecycle_lock = threading.RLock()
        self._closed = threading.Event()
        self._processes: dict[subprocess.Popen[Any], tempfile.TemporaryDirectory | None] = {}

    def definition(self) -> dict[str, Any]:
        return application_tool(self.native_templates)

    def create(self, payload: dict[str, Any]) -> dict[str, str]:
        title = payload.get("title")
        request = payload.get("request")
        if not isinstance(title, str) or not isinstance(request, str):
            raise ValueError("Enter a title and request.")
        title = _normalize_title(title)
        request = _normalize_request(request)
        runtime = payload.get("runtime", "web")
        template_value = payload.get("template")
        if runtime not in RUNTIMES:
            raise ValueError("Choose a supported application runtime.")
        if runtime == "web":
            if template_value not in (None, ""):
                raise ValueError("Web applications do not use a native template.")
            template = None
        else:
            if not self.native_templates:
                raise ValueError("Native applications are not available.")
            if template_value not in self.native_templates:
                raise ValueError("Choose an advertised native application template.")
            template = str(template_value)

        for _ in range(1000):
            app_id = _application_id(title)
            folder = self.root / app_id
            if folder.exists():
                continue
            try:
                folder.mkdir(mode=0o700)
            except FileExistsError:
                continue
            folder.chmod(0o700)
            draft = {
                "id": app_id,
                "title": title,
                "request": request,
                "created_at": _utc_now(),
                "runtime": runtime,
            }
            if template is not None:
                draft["template"] = template
            try:
                _atomic_write_json(folder / ".draft.json", draft)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            result = {"id": app_id, "title": title, "runtime": runtime}
            if template is not None:
                result["template"] = template
            return result
        raise RuntimeError("Could not allocate a new application identifier.")

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        folder = self._draft_folder(payload)
        draft = self._load_draft(folder)
        if draft["runtime"] == "native":
            raise ValueError("Native applications use a trusted template and cannot accept HTML.")
        html = payload.get("html")
        if not isinstance(html, str):
            raise ValueError("Enter HTML text.")
        if not DOCTYPE_RE.match(html):
            raise ValueError("HTML must start with <!doctype html>.")
        written = _atomic_write_text(folder / "index.html", html)
        return {"written": True, "bytes": written}

    def read(self, payload: dict[str, Any]) -> str:
        folder = self._existing_folder(payload)
        manifest = self._load_manifest(folder)
        if manifest is not None:
            metadata = manifest
        else:
            metadata = self._load_draft(folder)
            allowed = {".draft.json", "index.html"} if metadata["runtime"] == "web" else {".draft.json"}
            self._validate_folder_contents(folder, allowed)
        if metadata["runtime"] == "native":
            raise ValueError("Native applications do not have an HTML document.")
        index = folder / "index.html"
        try:
            html_bytes = _read_regular_file_bytes(index, MAX_HTML_BYTES)
        except FileNotFoundError as error:
            raise ValueError("The application has no readable HTML document.") from error
        except ValueError as error:
            if str(error) == "The file is too large.":
                raise ValueError("The HTML document is too large.") from error
            raise ValueError("The application has no readable HTML document.") from error
        try:
            return html_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("The HTML document is not valid UTF-8.") from error

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        folder = self._draft_folder(payload)
        draft = self._load_draft(folder)
        summary_value = payload.get("summary")
        keywords_value = payload.get("keywords")
        if not isinstance(summary_value, str) or not isinstance(keywords_value, list):
            raise ValueError("Enter a summary and keyword list.")
        if len(keywords_value) > MAX_KEYWORDS:
            raise ValueError("Keep the keyword list to 20 items or fewer.")
        summary = _normalize_summary(summary_value)
        keywords = _normalize_keywords(keywords_value)
        manifest, _ = self._build_manifest(folder, draft, summary, keywords)
        _atomic_write_json(folder / "manifest.json", manifest)
        (folder / ".draft.json").unlink(missing_ok=True)
        result: dict[str, Any] = {"published": True, "id": draft["id"], "runtime": draft["runtime"]}
        if draft["runtime"] == "native":
            result["template"] = manifest["template"]
        else:
            result["sha256"] = manifest["sha256"]
        return result

    def _build_manifest(
        self, folder: Path, draft: dict[str, Any], summary: str, keywords: list[str],
    ) -> tuple[dict[str, Any], bytes | None]:
        manifest = {
            "id": draft["id"],
            "title": draft["title"],
            "request": draft["request"],
            "created_at": draft["created_at"],
            "updated_at": _utc_now(),
            "summary": summary,
            "keywords": keywords,
            "runtime": draft["runtime"],
        }
        html_bytes = None
        if draft["runtime"] == "native":
            manifest["template"] = draft["template"]
        else:
            index = folder / "index.html"
            if not _is_regular_file(index):
                raise ValueError("Write the HTML document before launching or publishing.")
            try:
                html_bytes = _read_regular_file_bytes(index, MAX_HTML_BYTES)
            except FileNotFoundError as error:
                raise ValueError("Write the HTML document before launching or publishing.") from error
            except ValueError as error:
                if str(error) == "The file is too large.":
                    raise ValueError("The HTML document is too large.") from error
                raise ValueError("Write the HTML document before launching or publishing.") from error
            try:
                if not DOCTYPE_RE.match(html_bytes.decode("utf-8")):
                    raise ValueError("HTML must start with <!doctype html>.")
            except UnicodeDecodeError as error:
                raise ValueError("The HTML document is not valid UTF-8.") from error
            sha256 = _sha256_bytes(html_bytes)
            manifest.update({"entrypoint": "index.html", "sha256": sha256})
        return manifest, html_bytes

    def search(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        query = payload.get("query")
        if not isinstance(query, str):
            raise ValueError("Enter a search query.")
        normalized = _normalize_text(query)
        if not normalized:
            raise ValueError("Enter a search query.")
        query_tokens = _tokenize(normalized)
        results: list[dict[str, Any]] = []
        for folder in self.root.iterdir() if self.root.exists() else []:
            if folder.is_symlink() or not _is_regular_dir(folder):
                continue
            try:
                entry = self._load_manifest(folder)
                if entry is None:
                    continue
                exact = normalized.casefold() == entry["request"].casefold()
                overlap = len(query_tokens & _tokenize(entry["title"])) * 2
                overlap += len(query_tokens & set(entry["keywords"]))
                score = (10_000 if exact else 0) + overlap
                if score <= 0:
                    continue
                results.append({
                    "id": entry["id"],
                    "title": entry["title"],
                    "summary": entry["summary"],
                    "exact": exact,
                    "runtime": entry["runtime"],
                    "template": entry["template"],
                    "_score": score,
                    "_updated_at": entry["updated_at"],
                })
            except (OSError, ValueError):
                continue
        results.sort(key=lambda item: (
            -item["_score"],
            0 if item["runtime"] == "native" else 1,
            -_timestamp_key(item["_updated_at"]),
            item["id"],
        ))
        return [
            {key: item[key] for key in ("id", "title", "summary", "exact", "runtime", "template")}
            for item in results[:5]
        ]

    def launch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lifecycle_lock:
            if self._closed.is_set():
                raise RuntimeError("This chat's application service is closed.")
            return self._launch(payload)

    def _launch(self, payload: dict[str, Any]) -> dict[str, Any]:
        folder = self._existing_folder(payload)
        manifest = self._load_manifest(folder)
        html_bytes = None
        is_draft = manifest is None
        if manifest is None:
            folder = self._draft_folder(payload)
            draft = self._load_draft(folder)
            manifest, html_bytes = self._build_manifest(folder, draft, draft["title"], [])
        if manifest["runtime"] == "native":
            if manifest["template"] not in self.native_templates or self.native_host is None:
                raise ValueError("This native application template is not available.")
        success = False
        snapshot = None
        try:
            if is_draft:
                # Keep runner integrity checks unchanged and leave the editable draft unpublished.
                snapshot = tempfile.TemporaryDirectory(prefix="aios-application-")
                folder = Path(snapshot.name) / manifest["id"]
                folder.mkdir(mode=0o700)
                if html_bytes is not None:
                    _atomic_write_bytes(folder / "index.html", html_bytes)
                _atomic_write_json(folder / "manifest.json", manifest)
            if self.launcher is not None:
                success = self.launcher(folder) is not False
            else:
                success = self._default_launcher(folder, manifest, snapshot)
                if success:
                    snapshot = None
        except Exception:
            success = False
        finally:
            if snapshot is not None:
                snapshot.cleanup()
        result: dict[str, Any] = {
            "launched": success,
            "id": manifest["id"],
            "title": manifest["title"],
            "runtime": manifest["runtime"],
        }
        if manifest["runtime"] == "native":
            result["template"] = manifest["template"]
        if not success:
            result["reason"] = LAUNCH_FAILURE_REASON
        return result

    def _default_launcher(
        self, folder: Path, manifest: dict[str, Any], snapshot: tempfile.TemporaryDirectory | None = None,
    ) -> bool:
        if os.name != "posix":
            return False
        if manifest["runtime"] == "native":
            template = manifest["template"]
            if template not in self.native_templates or template not in NATIVE_TEMPLATES or self.native_host is None:
                return False
        read_fd, write_fd = os.pipe()
        process = None
        owns_process = False
        try:
            if self._closed.is_set():
                return False
            if manifest["runtime"] == "native":
                command = [os.fspath(self.native_host)]
                environment = os.environ.copy()
                environment.update({
                    "AIOS_APP_TEMPLATE": template,
                    "AIOS_APP_TITLE": manifest["title"],
                    "AIOS_APP_READY_FD": str(write_fd),
                })
            else:
                command = [
                    sys.executable,
                    "-m",
                    "aios.app_runner",
                    str(folder),
                    "--ready-fd",
                    str(write_fd),
                ]
                environment = None
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=(write_fd,),
                env=environment,
            )
            owns_process = True
            os.close(write_fd)
            write_fd = -1

            ready = False
            try:
                selector = selectors.DefaultSelector()
                try:
                    selector.register(read_fd, selectors.EVENT_READ)
                    deadline = time.monotonic() + self.launch_timeout
                    received = b""
                    while time.monotonic() < deadline and not self._closed.is_set():
                        events = selector.select(min(0.1, max(0, deadline - time.monotonic())))
                        if not events:
                            if process.poll() is not None:
                                break
                            continue
                        chunk = os.read(read_fd, 64)
                        if not chunk:
                            break
                        received += chunk
                        if received == b"ready\n":
                            ready = True
                            break
                        if not b"ready\n".startswith(received):
                            break
                finally:
                    selector.close()
            finally:
                os.close(read_fd)
                read_fd = -1
            if not ready or self._closed.is_set() or process.poll() is not None:
                return False
            self._processes[process] = snapshot
            threading.Thread(target=self._reap_child, args=(process,), name="aios-app-reaper", daemon=True).start()
            owns_process = False
            return True
        except Exception:
            return False
        finally:
            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
            if read_fd >= 0:
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            if owns_process and process is not None:
                self._processes.pop(process, None)
                self._terminate_child(process)

    def _reap_child(self, process: subprocess.Popen[Any]) -> None:
        process.wait()
        with self._lifecycle_lock:
            if process not in self._processes:
                return
            snapshot = self._processes.pop(process)
            self._close_child(process, snapshot)

    def _close_child(self, process: subprocess.Popen[Any], snapshot: tempfile.TemporaryDirectory | None) -> None:
        try:
            self._terminate_child(process)
        finally:
            if snapshot is not None:
                snapshot.cleanup()

    def close(self) -> None:
        # Interrupt readiness before taking the lock held by launch, including during Popen.
        self._closed.set()
        with self._lifecycle_lock:
            processes = self._processes
            self._processes = {}
            threads = []
            for process, snapshot in processes.items():
                thread = threading.Thread(
                    target=self._close_child, args=(process, snapshot), name="aios-app-close",
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()

    @staticmethod
    def _terminate_child(process: subprocess.Popen[Any]) -> None:
        group_signaled = False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            group_signaled = True
        except (AttributeError, OSError, ProcessLookupError):
            pass
        if not group_signaled:
            if process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        if group_signaled:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (AttributeError, OSError, ProcessLookupError):
                pass
        elif process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=1)
        except Exception:
            pass

    def _existing_folder(self, payload: dict[str, Any]) -> Path:
        app_id = payload.get("id")
        if not _is_valid_id(app_id):
            raise ValueError("Enter a valid application id.")
        folder = self.root / app_id
        if not _is_regular_dir(folder) or folder.is_symlink():
            raise ValueError("Unknown application id.")
        return folder

    def _draft_folder(self, payload: dict[str, Any]) -> Path:
        folder = self._existing_folder(payload)
        if not _is_regular_file(folder / ".draft.json"):
            raise ValueError("This application is no longer a draft.")
        if (folder / "manifest.json").exists():
            raise ValueError("This application is already published.")
        draft = self._load_draft(folder)
        allowed = {".draft.json", "index.html"} if draft["runtime"] == "web" else {".draft.json"}
        self._validate_folder_contents(folder, allowed)
        return folder

    def _load_draft(self, folder: Path) -> dict[str, Any]:
        try:
            data = _load_json(folder / ".draft.json")
        except (FileNotFoundError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid draft metadata.") from error
        if not isinstance(data, dict):
            raise ValueError("Invalid draft metadata.")
        legacy = {"id", "title", "request", "created_at"}
        web = legacy | {"runtime"}
        native = web | {"template"}
        if set(data) == legacy:
            data["runtime"] = "web"
            data["template"] = None
        elif set(data) == web and data.get("runtime") == "web":
            data["template"] = None
        elif set(data) == native and data.get("runtime") == "native" and data.get("template") in NATIVE_TEMPLATES:
            pass
        else:
            raise ValueError("Invalid draft metadata.")
        self._validate_common_metadata(data, folder, published=False)
        return data

    def _load_manifest(self, folder: Path) -> dict[str, Any] | None:
        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            data = _load_json(manifest_path)
        except (FileNotFoundError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid manifest metadata.") from error
        if not isinstance(data, dict):
            raise ValueError("Invalid manifest metadata.")
        base = {"id", "title", "request", "created_at", "updated_at", "summary", "keywords"}
        legacy_web = base | {"entrypoint", "sha256"}
        new_web = legacy_web | {"runtime"}
        native = base | {"runtime", "template"}
        if set(data) == legacy_web:
            data["runtime"] = "web"
            data["template"] = None
        elif set(data) == new_web and data.get("runtime") == "web":
            data["template"] = None
        elif set(data) == native and data.get("runtime") == "native" and data.get("template") in NATIVE_TEMPLATES:
            pass
        else:
            raise ValueError("Invalid manifest metadata.")
        self._validate_common_metadata(data, folder, published=True)
        draft_path = folder / ".draft.json"
        if data["runtime"] == "native":
            if draft_path.exists():
                if not _is_regular_file(draft_path):
                    raise ValueError("Invalid manifest metadata.")
                draft = self._load_draft(folder)
                if any(draft[key] != data[key] for key in ("id", "title", "request", "created_at", "runtime", "template")):
                    raise ValueError("Invalid manifest metadata.")
                draft_path.unlink()
            self._validate_folder_contents(folder, {"manifest.json"})
            if (folder / "index.html").exists():
                raise ValueError("Invalid manifest metadata.")
        else:
            self._validate_folder_contents(folder, {"index.html", "manifest.json", ".draft.json"})
            try:
                html_bytes = _read_regular_file_bytes(folder / "index.html", MAX_HTML_BYTES)
            except (FileNotFoundError, ValueError) as error:
                raise ValueError("Invalid manifest metadata.") from error
            if data.get("entrypoint") != "index.html":
                raise ValueError("Invalid manifest metadata.")
            if not isinstance(data.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
                raise ValueError("Invalid manifest metadata.")
            if _sha256_bytes(html_bytes) != data["sha256"]:
                raise ValueError("Invalid manifest metadata.")
            try:
                if not DOCTYPE_RE.match(html_bytes.decode("utf-8")):
                    raise ValueError("Invalid manifest metadata.")
            except UnicodeDecodeError as error:
                raise ValueError("Invalid manifest metadata.") from error
            if draft_path.exists():
                draft = self._load_draft(folder)
                if any(draft[key] != data[key] for key in ("id", "title", "request", "created_at", "runtime", "template")):
                    raise ValueError("Invalid manifest metadata.")
                draft_path.unlink()
        return data

    @staticmethod
    def _validate_common_metadata(data: dict[str, Any], folder: Path, *, published: bool) -> None:
        message = "Invalid manifest metadata." if published else "Invalid draft metadata."
        if not _is_valid_id(data.get("id")) or data["id"] != folder.name:
            raise ValueError(message)
        if not isinstance(data.get("title"), str) or not (1 <= len(data["title"]) <= MAX_TITLE_LENGTH):
            raise ValueError(message)
        if not isinstance(data.get("request"), str) or not (1 <= len(data["request"]) <= MAX_REQUEST_LENGTH):
            raise ValueError(message)
        if not isinstance(data.get("created_at"), str) or not data["created_at"]:
            raise ValueError(message)
        _timestamp_key(data["created_at"])
        if not published:
            return
        if not isinstance(data.get("updated_at"), str) or not data["updated_at"]:
            raise ValueError(message)
        _timestamp_key(data["updated_at"])
        if not isinstance(data.get("summary"), str) or not (1 <= len(data["summary"]) <= MAX_SUMMARY_LENGTH):
            raise ValueError(message)
        if not isinstance(data.get("keywords"), list) or len(data["keywords"]) > MAX_KEYWORDS:
            raise ValueError(message)
        keywords = data["keywords"]
        if any(not isinstance(keyword, str) or not re.fullmatch(r"[a-z0-9]+", keyword) for keyword in keywords):
            raise ValueError(message)
        if keywords != sorted(set(keywords)):
            raise ValueError(message)

    @staticmethod
    def _validate_folder_contents(folder: Path, allowed: set[str]) -> None:
        if folder.is_symlink() or not _is_regular_dir(folder):
            raise ValueError("Unexpected application files found.")
        for entry in folder.iterdir():
            if entry.name not in allowed or entry.is_symlink() or not _is_regular_file(entry):
                raise ValueError("Unexpected application files found.")


def _load_json(path: Path) -> Any:
    return json.loads(_read_regular_file_bytes(path, MAX_HTML_BYTES).decode("utf-8"))
