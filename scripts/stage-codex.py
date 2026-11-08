#!/usr/bin/env python3
"""Stage a checksum-pinned, static musl Codex binary for the image architecture."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request

VERSION = '0.153.4'
CHECKSUMS = {
    'x86_64': 'f479424eca092484dc40d87ae28c44f4cc40234a60045d6131e493800d814a30',
    'aarch64': '5cda6182bd94c3a30f2eb63a495489ebf7f691fddb14d70f48c6c1a5071b6cde',
}


def _download_archive(url: str, archive: Path):
    partial = archive.with_suffix('.part')
    headers = {'User-Agent': 'aios-stage-codex/1.0'}
    github_token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    api_request = urllib.request.Request(
        f'https://api.github.com/repos/openai/codex/releases/tags/rust-v{VERSION}',
        headers={**headers, **({'Authorization': f'Bearer {github_token}'} if github_token else {})},
    )
    fallback_urls = [url]
    try:
        with urllib.request.urlopen(api_request, timeout=30) as meta_response:
            release_data = json.loads(meta_response.read().decode('utf-8'))
            asset_filename = archive.name.replace(f'-{VERSION}', '')
            for asset in release_data.get('assets', []):
                if asset.get('name') == asset_filename and asset.get('url'):
                    fallback_urls.insert(0, asset['url'])
                    break
    except Exception:
        pass

    last_error = None
    for attempt in range(4):
        target_url = fallback_urls[attempt % len(fallback_urls)]
        req_headers = dict(headers)
        if 'api.github.com' in target_url:
            req_headers['Accept'] = 'application/octet-stream'
            if github_token:
                req_headers['Authorization'] = f'Bearer {github_token}'
        request = urllib.request.Request(target_url, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as stream:
                shutil.copyfileobj(response, stream)
            partial.replace(archive)
            return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if partial.exists():
                partial.unlink()
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise SystemExit(f'Failed downloading Codex archive after 4 attempts: {last_error}')


def main():
    build, dest = map(Path, sys.argv[1:3])
    arch = os.environ.get('ARCH', platform.machine())
    if arch not in CHECKSUMS:
        raise SystemExit('ChatGPT runtime supports x86_64 and aarch64 only.')
    name = f'codex-{arch}-unknown-linux-musl'
    archive = build / f'{name}-{VERSION}.tar.gz'
    build.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        _download_archive(f'https://github.com/openai/codex/releases/download/rust-v{VERSION}/{name}.tar.gz', archive)
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
