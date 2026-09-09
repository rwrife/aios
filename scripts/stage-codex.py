#!/usr/bin/env python3
"""Stage a checksum-pinned, static musl Codex binary for the image architecture."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import urllib.request

VERSION = '0.153.4'
CHECKSUMS = {
    'x86_64': 'f479424eca092484dc40d87ae28c44f4cc40234a60045d6131e493800d814a30',
    'aarch64': '5cda6182bd94c3a30f2eb63a495489ebf7f691fddb14d70f48c6c1a5071b6cde',
}


def main():
    build, dest = map(Path, sys.argv[1:3])
    arch = os.environ.get('ARCH', platform.machine())
    if arch not in CHECKSUMS:
        raise SystemExit('ChatGPT runtime supports x86_64 and aarch64 only.')
    name = f'codex-{arch}-unknown-linux-musl'
    archive = build / f'{name}-{VERSION}.tar.gz'
    build.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        partial = archive.with_suffix('.part')
        with urllib.request.urlopen(f'https://github.com/openai/codex/releases/download/rust-v{VERSION}/{name}.tar.gz', timeout=120) as response, partial.open('wb') as stream:
            shutil.copyfileobj(response, stream)
        partial.replace(archive)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != CHECKSUMS[arch]:
        raise SystemExit('Codex archive checksum mismatch: ' + str(archive))
    target = dest / 'usr/local/bin/codex'
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        member = tar.getmember(name)
        if not member.isfile():
            raise SystemExit('Codex archive does not contain a regular binary.')
        with tar.extractfile(member) as source, target.open('wb') as output:
            shutil.copyfileobj(source, output)
    target.chmod(0o755)
    share = dest / 'usr/local/share/aios'
    share.mkdir(parents=True, exist_ok=True)
    (share / 'codex-version').write_text(VERSION + '\n')
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'apps/aios/licenses/Codex-LICENSE', share / 'CODEX-LICENSE')
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'apps/aios/licenses/Codex-NOTICE', share / 'CODEX-NOTICE')


if __name__ == '__main__':
    main()
