"""Bind functional test results to a validated project bundle and its examples.

#164 evidence layer: a build, a launched window or a readiness handshake is not
proof of functionality. This module accepts results observed elsewhere (the
managed-runtime sandbox of #162, or a trusted test harness), checks that the
results were produced for exactly this source and exactly these expectations,
and classifies the outcome as tested / partially_verified / failed.

It never executes code, never stores diagnostics beyond a bounded status, and
echoes neither inputs nor observations back in its report.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import project_bundle

MAX_EXAMPLES = 16
MAX_FIELD_BYTES = 4096

_NAME_RE = re.compile(r'^[\x20-\x7e]{1,64}$')
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')

_EXAMPLE_KEYS = {'name', 'input', 'expected'}
_RESULT_KEYS = {'name', 'status', 'observed'}
_EVIDENCE_KEYS = {'source_sha256', 'examples_sha256', 'results'}
_RESULT_STATUSES = {'completed', 'failed', 'not_tested'}

STATUS_TESTED = 'tested'
STATUS_PARTIAL = 'partially_verified'
STATUS_FAILED = 'failed'


def _encode(value: str, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError(f'{label} is not valid UTF-8: {exc}') from exc
    if len(encoded) > MAX_FIELD_BYTES:
        raise ValueError(f'{label} exceeds {MAX_FIELD_BYTES} bytes')
    return encoded


def _canonical_example(example: Any) -> dict[str, str]:
    if not isinstance(example, dict) or set(example) != _EXAMPLE_KEYS:
        raise ValueError('Acceptance example must have exactly name, input, expected')
    name = example['name']
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError('Acceptance example name must be printable, non-empty and bounded')
    _encode(example['input'], f'input for {name!r}')
    expected = example['expected']
    expected_bytes = _encode(expected, f'expected for {name!r}')
    if not expected_bytes:
        raise ValueError(f'Expected result for {name!r} must not be empty')
    return {'name': name, 'input': example['input'], 'expected': expected}


def examples_digest(examples: Any) -> str:
    """Canonical digest of the acceptance examples, independent of their order."""
    if not isinstance(examples, list) or not examples:
        raise ValueError('Acceptance examples must be a non-empty list')
    if len(examples) > MAX_EXAMPLES:
        raise ValueError(f'Acceptance examples exceed maximum of {MAX_EXAMPLES}')
    canonical = [_canonical_example(example) for example in examples]
    lines = [json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in canonical]
    names = [entry['name'] for entry in canonical]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate acceptance example names are forbidden')
    hasher = hashlib.sha256()
    for line in sorted(lines):
        data = line.encode('utf-8')
        hasher.update(f'{len(data)}:'.encode('ascii'))
        hasher.update(data)
        hasher.update(b'\n')
    return hasher.hexdigest()


def _canonical_results(results: Any, expected_names: set[str]) -> dict[str, dict[str, str]]:
    if not isinstance(results, list):
        raise ValueError('Observed results must be a list')
    if len(results) > MAX_EXAMPLES:
        raise ValueError(f'Observed results exceed maximum of {MAX_EXAMPLES}')
    by_name: dict[str, dict[str, str]] = {}
    for result in results:
        if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
            raise ValueError('Observed result must have exactly name, status, observed')
        name = result['name']
        if not isinstance(name, str) or name not in expected_names:
            raise ValueError('Observed result names an unknown acceptance example')
        if name in by_name:
            raise ValueError(f'Duplicate observations for acceptance example {name!r}')
        status = result['status']
        if not isinstance(status, str) or status not in _RESULT_STATUSES:
            raise ValueError(f'Invalid observation status: {status!r}')
        observed = result['observed']
        _encode(observed, f'observed output for {name!r}')
        by_name[name] = {'name': name, 'status': status, 'observed': observed}
    return by_name


def verify(source: Any, examples: Any, evidence: Any) -> dict[str, Any]:
    """Grade observed functional results against the source and its examples.

    ``source`` is a raw project source accepted by :func:`project_bundle.validate`.
    ``examples`` are the separately declared acceptance examples. ``evidence``
    carries the digests the runner attests to plus the observed results; a
    digest mismatch means the evidence predates the current source or
    expectations and is rejected outright.
    """
    bundle = project_bundle.validate(source)
    examples_sha256 = examples_digest(examples)

    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        raise ValueError('Evidence must have exactly source_sha256, examples_sha256, results')
    for key in ('source_sha256', 'examples_sha256'):
        value = evidence[key]
        if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
            raise ValueError(f'Evidence {key} must be a lowercase sha256 hex digest')
    if evidence['source_sha256'] != bundle['sha256']:
        raise ValueError('Evidence was produced for a different source revision')
    if evidence['examples_sha256'] != examples_sha256:
        raise ValueError('Evidence was produced for different acceptance examples')

    canonical = sorted(
        (_canonical_example(example) for example in examples),
        key=lambda entry: entry['name'])
    observations = _canonical_results(evidence['results'],
                                      {entry['name'] for entry in canonical})

    graded: list[dict[str, str]] = []
    for entry in canonical:
        observation = observations.get(entry['name'])
        if observation is None or observation['status'] == 'not_tested':
            status = 'not_tested'
        elif observation['status'] == 'failed':
            status = 'failed'
        else:
            status = 'passed' if observation['observed'] == entry['expected'] else 'failed'
        graded.append({'name': entry['name'], 'status': status})

    statuses = {row['status'] for row in graded}
    if 'failed' in statuses:
        overall = STATUS_FAILED
    elif 'not_tested' in statuses:
        overall = STATUS_PARTIAL
    else:
        overall = STATUS_TESTED

    return {
        'status': overall,
        'source_sha256': bundle['sha256'],
        'examples_sha256': examples_sha256,
        'examples': graded,
    }
