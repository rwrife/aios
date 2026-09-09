"""Persistent application cache for one self-contained HTML document."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
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

APPLICATION_TOOL = {
    "type": "function",
    "function": {
        "name": "application",
        "description": "Create, store, search, publish, read, write, and launch cached single-file HTML applications.",
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_title(value: str) -> str:
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
    def __init__(self, root: Path | str | None = None, launcher: Callable[[Path], Any] | None = None):
        self.root = Path(root) if root is not None else core.data_dir() / "applications"
        self.root = self.root.expanduser()
        _ensure_secure_directory(self.root)
        self.launcher = launcher or self._default_launcher

    def create(self, payload: dict[str, Any]) -> dict[str, str]:
        title = payload.get("title")
        request = payload.get("request")
        if not isinstance(title, str) or not isinstance(request, str):
            raise ValueError("Enter a title and request.")
        title = _normalize_title(title)
        request = _normalize_request(request)

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
            try:
                _atomic_write_json(folder / ".draft.json", {
                    "id": app_id,
                    "title": title,
                    "request": request,
                    "created_at": _utc_now(),
                })
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            return {"id": app_id, "title": title}
        raise RuntimeError("Could not allocate a new application identifier.")

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        folder = self._draft_folder(payload)
        html = payload.get("html")
        if not isinstance(html, str):
            raise ValueError("Enter HTML text.")
        if not DOCTYPE_RE.match(html):
            raise ValueError("HTML must start with <!doctype html>.")
        index = folder / "index.html"
        written = _atomic_write_text(index, html)
        return {"written": True, "bytes": written}

    def read(self, payload: dict[str, Any]) -> str:
        folder = self._existing_folder(payload)
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
        index = folder / "index.html"
        if not _is_regular_file(index):
            raise ValueError("Write the HTML document before publishing.")
        summary_value = payload.get("summary")
        keywords_value = payload.get("keywords")
        if not isinstance(summary_value, str) or not isinstance(keywords_value, list):
            raise ValueError("Enter a summary and keyword list.")
        if len(keywords_value) > MAX_KEYWORDS:
            raise ValueError("Keep the keyword list to 20 items or fewer.")
        summary = _normalize_summary(summary_value)
        keywords = _normalize_keywords(keywords_value)
        try:
            html_bytes = _read_regular_file_bytes(index, MAX_HTML_BYTES)
        except FileNotFoundError as error:
            raise ValueError("Write the HTML document before publishing.") from error
        except ValueError as error:
            if str(error) == "The file is too large.":
                raise ValueError("The HTML document is too large.") from error
            raise ValueError("Write the HTML document before publishing.") from error
        try:
            if not DOCTYPE_RE.match(html_bytes.decode("utf-8")):
                raise ValueError("HTML must start with <!doctype html>.")
        except UnicodeDecodeError as error:
            raise ValueError("The HTML document is not valid UTF-8.") from error
        sha256 = _sha256_bytes(html_bytes)
        manifest = {
            "id": draft["id"],
            "title": draft["title"],
            "request": draft["request"],
            "created_at": draft["created_at"],
            "updated_at": _utc_now(),
            "summary": summary,
            "keywords": keywords,
            "entrypoint": "index.html",
            "sha256": sha256,
        }
        _atomic_write_json(folder / "manifest.json", manifest)
        draft_path = folder / ".draft.json"
        if draft_path.exists():
            draft_path.unlink()
        return {"published": True, "id": draft["id"], "sha256": sha256}

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
                title_tokens = _tokenize(entry["title"])
                keyword_tokens = set(entry["keywords"])
                exact = normalized.casefold() == entry["request"].casefold()
                overlap = len(query_tokens & title_tokens) * 2 + len(query_tokens & keyword_tokens)
                score = (10_000 if exact else 0) + overlap
                if score <= 0:
                    continue
                results.append({
                    "id": entry["id"],
                    "title": entry["title"],
                    "summary": entry["summary"],
                    "exact": exact,
                    "_score": score,
                    "_updated_at": entry["updated_at"],
                })
            except (OSError, ValueError):
                continue
        results.sort(key=lambda item: (-item["_score"], -_timestamp_key(item["_updated_at"]), item["id"]))
        return [{"id": item["id"], "title": item["title"], "summary": item["summary"], "exact": item["exact"]} for item in results[:5]]

    def launch(self, payload: dict[str, Any]) -> dict[str, str]:
        folder = self._published_folder(payload)
        manifest = self._load_manifest(folder)
        if manifest is None:
            raise ValueError("Publish the application before launching it.")
        index = folder / "index.html"
        try:
            html_bytes = _read_regular_file_bytes(index, MAX_HTML_BYTES)
        except FileNotFoundError as error:
            raise ValueError("The application document is missing.") from error
        except ValueError as error:
            if str(error) == "The file is too large.":
                raise ValueError("The HTML document is too large.") from error
            raise ValueError("The application document is missing.") from error
        if _sha256_bytes(html_bytes) != manifest["sha256"]:
            raise ValueError("The application content changed after publication.")
        try:
            if not DOCTYPE_RE.match(html_bytes.decode("utf-8")):
                raise ValueError("HTML must start with <!doctype html>.")
        except UnicodeDecodeError as error:
            raise ValueError("The HTML document is not valid UTF-8.") from error
        self.launcher(folder)
        return {"launched": True, "id": manifest["id"], "title": manifest["title"]}

    def _default_launcher(self, folder: Path) -> subprocess.Popen[Any]:
        return subprocess.Popen(
            [sys.executable, "-m", "aios.app_runner", str(folder)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def _existing_folder(self, payload: dict[str, Any]) -> Path:
        app_id = payload.get("id")
        if not _is_valid_id(app_id):
            raise ValueError("Enter a valid application id.")
        folder = self.root / app_id
        if not _is_regular_dir(folder) or folder.is_symlink():
            raise ValueError("Unknown application id.")
        self._validate_folder_contents(folder, allow_draft=True, allow_manifest=True)
        return folder

    def _draft_folder(self, payload: dict[str, Any]) -> Path:
        folder = self._existing_folder(payload)
        if not _is_regular_file(folder / ".draft.json"):
            raise ValueError("This application is no longer a draft.")
        if _is_regular_file(folder / "manifest.json"):
            raise ValueError("This application is already published.")
        self._validate_folder_contents(folder, allow_draft=True, allow_manifest=False)
        return folder

    def _published_folder(self, payload: dict[str, Any]) -> Path:
        folder = self._existing_folder(payload)
        if not _is_regular_file(folder / "manifest.json"):
            raise ValueError("This application is not published.")
        self._validate_folder_contents(folder, allow_draft=True, allow_manifest=True)
        return folder

    def _load_draft(self, folder: Path) -> dict[str, Any]:
        draft_path = folder / ".draft.json"
        if not draft_path.exists():
            raise ValueError("This application is no longer a draft.")
        try:
            data = _load_json(draft_path)
        except (FileNotFoundError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid draft metadata.") from error
        if not isinstance(data, dict):
            raise ValueError("Invalid draft metadata.")
        required = {"id", "title", "request", "created_at"}
        if set(data) != required:
            raise ValueError("Invalid draft metadata.")
        if not _is_valid_id(data["id"]) or data["id"] != folder.name:
            raise ValueError("Invalid draft metadata.")
        if not isinstance(data["title"], str) or not (1 <= len(data["title"]) <= MAX_TITLE_LENGTH):
            raise ValueError("Invalid draft metadata.")
        if not isinstance(data["request"], str) or not (1 <= len(data["request"]) <= MAX_REQUEST_LENGTH):
            raise ValueError("Invalid draft metadata.")
        if not isinstance(data["created_at"], str) or not data["created_at"]:
            raise ValueError("Invalid draft metadata.")
        _timestamp_key(data["created_at"])
        return data

    def _load_manifest(self, folder: Path) -> dict[str, Any] | None:
        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            return None
        self._validate_folder_contents(folder, allow_draft=True, allow_manifest=True)
        index = folder / "index.html"
        try:
            html_bytes = _read_regular_file_bytes(index, MAX_HTML_BYTES)
        except (FileNotFoundError, ValueError) as error:
            raise ValueError("Invalid manifest metadata.") from error
        try:
            data = _load_json(manifest_path)
        except (FileNotFoundError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid manifest metadata.") from error
        if not isinstance(data, dict):
            raise ValueError("Invalid manifest metadata.")
        required = {"id", "title", "request", "created_at", "updated_at", "summary", "keywords", "entrypoint", "sha256"}
        if set(data) != required:
            raise ValueError("Invalid manifest metadata.")
        if not _is_valid_id(data["id"]) or data["id"] != folder.name:
            raise ValueError("Invalid manifest metadata.")
        if not isinstance(data["title"], str) or not (1 <= len(data["title"]) <= MAX_TITLE_LENGTH):
            raise ValueError("Invalid manifest metadata.")
        if not isinstance(data["request"], str) or not (1 <= len(data["request"]) <= MAX_REQUEST_LENGTH):
            raise ValueError("Invalid manifest metadata.")
        if not isinstance(data["created_at"], str) or not data["created_at"]:
            raise ValueError("Invalid manifest metadata.")
        if not isinstance(data["updated_at"], str) or not data["updated_at"]:
            raise ValueError("Invalid manifest metadata.")
        _timestamp_key(data["created_at"])
        _timestamp_key(data["updated_at"])
        if not isinstance(data["summary"], str) or not (1 <= len(data["summary"]) <= MAX_SUMMARY_LENGTH):
            raise ValueError("Invalid manifest metadata.")
        if data["entrypoint"] != "index.html":
            raise ValueError("Invalid manifest metadata.")
        if not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
            raise ValueError("Invalid manifest metadata.")
        if not isinstance(data["keywords"], list) or len(data["keywords"]) > MAX_KEYWORDS:
            raise ValueError("Invalid manifest metadata.")
        normalized_keywords: list[str] = []
        for keyword in data["keywords"]:
            if not isinstance(keyword, str) or not re.fullmatch(r"[a-z0-9]+", keyword):
                raise ValueError("Invalid manifest metadata.")
            normalized_keywords.append(keyword)
        if normalized_keywords != sorted(set(normalized_keywords)):
            raise ValueError("Invalid manifest metadata.")
        data["keywords"] = normalized_keywords
        if _sha256_bytes(html_bytes) != data["sha256"]:
            raise ValueError("Invalid manifest metadata.")
        try:
            if not DOCTYPE_RE.match(html_bytes.decode("utf-8")):
                raise ValueError("Invalid manifest metadata.")
        except UnicodeDecodeError as error:
            raise ValueError("Invalid manifest metadata.") from error
        draft_path = folder / ".draft.json"
        if draft_path.exists():
            self._load_draft(folder)
            draft_path.unlink()
        return data

    def _validate_folder_contents(self, folder: Path, *, allow_draft: bool, allow_manifest: bool) -> None:
        if folder.is_symlink() or not _is_regular_dir(folder):
            raise ValueError("Unexpected application files found.")
        allowed = {"index.html"}
        if allow_draft:
            allowed.add(".draft.json")
        if allow_manifest:
            allowed.add("manifest.json")
        for entry in folder.iterdir():
            if entry.name not in allowed:
                raise ValueError("Unexpected application files found.")
            if entry.is_symlink():
                raise ValueError("Unexpected application files found.")
            if entry.name in {"index.html", ".draft.json", "manifest.json"} and not _is_regular_file(entry):
                raise ValueError("Unexpected application files found.")


def _load_json(path: Path) -> Any:
    return json.loads(_read_regular_file_bytes(path, MAX_HTML_BYTES).decode("utf-8"))
