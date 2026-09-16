"""Offline aggregate release checklist. Does not grant runtime approval."""
import argparse
import hashlib
import json
import math

GATES = ('consent_and_retention', 'disjoint_cohorts', 'calibration_frozen',
         'unprivileged_alpine_capture', 'adversarial_matrix', 'no_authority_effects',
         'no_media_retention', 'lifecycle_invalidation', 'keyboard_pin_fallback',
         'ocean_and_generated_palette', 'accessibility_and_reduced_motion', 'long_soak')
METRICS = ('false_match_rate', 'false_reject_rate', 'ambiguity_rate',
           'quality_rejection_rate', 'cold_suggestion_p95_seconds',
           'warm_suggestion_p95_seconds', 'camera_open_p95_seconds',
           'camera_reopen_p95_seconds', 'cpu_one_core_percent', 'rss_mib',
           'rss_growth_mib_per_hour', 'camera_duty_fraction', 'recovery_p95_seconds')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def assess(plan, report):
    """Missing, unsigned, changed, malformed or failed evidence never passes.

    These are reviewer attestations, not independently verifiable consent or
    accuracy. Runtime manifest approval remains a separate reviewed action.
    """
    reasons = []
    if plan.get('status') != 'approved' or not plan.get('review_reference'):
        reasons.append('acceptance plan has not been reviewed and approved')
    if report.get('plan_sha256') != digest(plan):
        reasons.append('report does not bind the frozen acceptance plan')
    for field in ('model_lock_sha256', 'calibration_sha256', 'image_sha256'):
        value = plan.get(field)
        if (type(value) is not str or len(value) != 64 or
                any(c not in '0123456789abcdef' for c in value) or report.get(field) != value):
            reasons.append(field + ' is missing or mismatched')
    frozen, started = plan.get('frozen_at'), report.get('evaluation_started_at')
    if not finite(frozen) or not finite(started) or frozen >= started:
        reasons.append('evaluation must start after plan freeze')
    for gate in GATES:
        value = report.get('gates', {}).get(gate, {})
        if (not isinstance(value, dict) or value.get('status') != 'pass' or
                type(value.get('evidence')) is not str or not value['evidence'].strip()):
            reasons.append(gate + ' is unmeasured or failed')
    for metric in METRICS:
        limit = plan.get('maximums', {}).get(metric)
        measured = report.get('metrics', {}).get(metric)
        if not finite(limit) or not finite(measured) or measured > limit:
            reasons.append(metric + ' has no passing approved measurement')
        if metric.endswith('_rate') or metric == 'camera_duty_fraction':
            if finite(limit) and limit > 1 or finite(measured) and measured > 1:
                reasons.append(metric + ' must be a fraction')
    # A zero observed error rate with too few attempts is not adequate evidence.
    for count in ('participants', 'genuine_attempts', 'impostor_attempts', 'soak_hours'):
        minimum = plan.get('minimums', {}).get(count)
        actual = report.get('counts', {}).get(count)
        if not finite(minimum) or minimum <= 0 or not finite(actual) or actual < minimum:
            reasons.append(count + ' has insufficient evidence')
        if count != 'soak_hours' and (type(minimum) is not int or type(actual) is not int):
            reasons.append(count + ' must be an integer count')
    return {'status': 'blocked' if reasons else 'review-ready', 'reasons': reasons,
            'runtime_approval_created': False, 'recognition_default': 'off'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('report')
    args = parser.parse_args()
    try:
        with open(args.plan) as stream:
            plan = json.load(stream)
        with open(args.report) as stream:
            report = json.load(stream)
        result = assess(plan, report)
    except (ValueError, TypeError, AttributeError, OSError):
        result = {'status': 'blocked', 'reasons': ['invalid evaluation input'],
                  'runtime_approval_created': False, 'recognition_default': 'off'}
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['status'] == 'review-ready' else 2


if __name__ == '__main__':
    raise SystemExit(main())
