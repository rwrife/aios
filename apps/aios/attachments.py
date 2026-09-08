"""Read only files explicitly selected by the user; never follow embedded links."""
from pathlib import Path
import subprocess
import os
from functools import partial
from .voice import child_lifetime

MAX_TEXT = 256 * 1024


def read_attachment(filename):
    path = Path(filename)
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Choose a text/source file or PDF smaller than 10 MiB.")
    if path.suffix.lower() == ".pdf":
        try:
            result = subprocess.run(["pdftotext", "-layout", str(path), "-"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=20, check=True,
                                    preexec_fn=partial(child_lifetime, os.getpid()))
            data = result.stdout
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Could not read this PDF. Use a PDF with selectable text.") from None
    else:
        with path.open("rb") as stream:
            data = stream.read(MAX_TEXT + 1)
    if len(data) > MAX_TEXT:
        raise ValueError("This file contains too much text. Attach a smaller excerpt (256 KiB maximum).")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("This file is not UTF-8 text. Attach a text/source file or PDF.") from None
    if "\x00" in text or not text.strip():
        raise ValueError("No readable text found. Images and scanned PDFs are not supported yet.")
    return path.name, text
