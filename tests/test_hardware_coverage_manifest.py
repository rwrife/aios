"""Structural/evidence-contract tests for docs/qa/hardware-coverage.json.

`jsonschema` is not a dependency of this repository, so the schema's
conditionals are also asserted directly against the data here. When
`jsonschema` happens to be installed, the manifest is additionally validated
against the Draft 2020-12 schema.
"""
import json
import re
import unittest
from pathlib import Path

try:  # optional: never added as a dependency just for tests
    import jsonschema
except ImportError:  # pragma: no cover - depends on the local environment
    jsonschema = None

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / 'docs' / 'qa' / 'hardware-coverage.json'
SCHEMA_PATH = ROOT / 'docs' / 'schemas' / 'hardware-coverage.schema.json'
BUILD_ENV_PATH = ROOT / 'distro' / 'alpine' / 'build.env'

STATUSES = {'untested', 'verified', 'degraded', 'unsupported'}
FAMILIES = {
    'wifi', 'gpu', 'storage', 'usb', 'hid-input', 'ethernet',
    'acpi', 'audio', 'network-stack', 'display-stack',
}
BUSES = {'pci', 'usb', 'sdio', 'platform', 'virtual', 'i2c'}
ID_PATTERN = re.compile(r'^[0-9a-f]{4}:[0-9a-f]{4}$')
DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ID_SLUG_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]*$')

REQUIRED_FIELDS = {
    'id', 'family', 'vendor', 'device', 'bus', 'ids',
    'kernel_module', 'module_dependencies', 'firmware_files',
    'firmware_requirements',
    'firmware_package', 'firmware_license', 'firmware_provenance',
    'minimum_kernel', 'minimum_mesa', 'representative_machine',
    'status', 'evidence_date', 'evidence_sources',
    'supporting_subsystems', 'notes',
}
ID_KEYS = {'pci_id', 'usb_id', 'subsystem_id', 'subsystem_id_status'}
VERSION_KEYS = {'status', 'version', 'reason', 'evidence_source', 'meets_baseline'}
LICENSE_KEYS = {'status', 'spdx', 'name', 'declared_by', 'source'}
PROVENANCE_KEYS = {
    'status', 'alpine_repository', 'package_index_checked_on',
    'package_index_source', 'notes',
}
MACHINE_KEYS = {'candidate', 'availability', 'selection_status', 'notes'}
EVIDENCE_KINDS = {
    'alpine-package-index', 'aports-apkbuild', 'alpine-release-notes',
    'upstream-driver-doc', 'repo-plan', 'image-inspection', 'physical-test',
}


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))


def load_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))


def load_build_env():
    values = {}
    for line in BUILD_ENV_PATH.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip()] = value.strip()
    return values


def entries_by_id(manifest):
    return {entry['id']: entry for entry in manifest['entries']}


class ManifestFileTests(unittest.TestCase):
    def test_files_exist_and_parse(self):
        self.assertTrue(MANIFEST_PATH.is_file())
        self.assertTrue(SCHEMA_PATH.is_file())
        load_schema()
        load_manifest()

    def test_schema_declares_draft_2020_12(self):
        schema = load_schema()
        self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        self.assertEqual(schema['properties']['schema_version']['const'], 2)

    @unittest.skipIf(jsonschema is None, 'jsonschema is not installed (not a repo dependency)')
    def test_manifest_validates_against_the_schema(self):
        jsonschema.validate(instance=load_manifest(), schema=load_schema())


class BaselineVersionTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()
        self.baseline = self.manifest['baseline_versions']

    def test_baseline_versions_are_present_and_sourced(self):
        self.assertEqual(self.baseline['status'], 'resolved-from-pinned-aports')
        self.assertRegex(self.baseline['resolved_on'], DATE_PATTERN)
        self.assertTrue(self.baseline['evidence_source'].strip())
        self.assertTrue(self.baseline['packages'])

    def test_baseline_versions_cite_the_pinned_aports_revision(self):
        aports_ref = load_build_env()['APORTS_REF']
        self.assertIn(aports_ref, self.baseline['evidence_source'])
        for name, package in self.baseline['packages'].items():
            if package['status'] == 'resolved':
                self.assertIn(aports_ref, package['evidence_source'], msg=name)

    def test_resolved_packages_have_versions_and_unresolved_do_not(self):
        for name, package in self.baseline['packages'].items():
            self.assertEqual(set(package.keys()),
                             {'status', 'version', 'alpine_repository', 'evidence_source'},
                             msg=name)
            if package['status'] == 'resolved':
                self.assertTrue(package['version'], msg=name)
                self.assertIn(package['alpine_repository'], {'main', 'community'}, msg=name)
            else:
                self.assertIsNone(package['version'], msg=name)

    def test_kernel_and_mesa_baselines_are_recorded(self):
        packages = self.baseline['packages']
        self.assertEqual(packages['linux-lts']['version'], '6.18.49-r0')
        self.assertEqual(packages['mesa']['version'], '25.2.7-r1')
        self.assertEqual(packages['linux-firmware']['version'], '20251125-r1')

    def test_baseline_note_does_not_claim_the_repositories_are_pinned(self):
        note = self.baseline['note'].lower()
        self.assertIn('keep moving', note)
        self.assertIn('build-manifest.json', note)


class ManifestStructureTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()

    def test_top_level_shape(self):
        self.assertEqual(self.manifest['schema_version'], 2)
        self.assertEqual(set(self.manifest.keys()),
                         {'schema_version', 'alpine_branch', 'baseline_versions', 'entries'})
        self.assertIsInstance(self.manifest['entries'], list)
        self.assertGreater(len(self.manifest['entries']), 0)

    def test_entry_ids_are_unique_slugs(self):
        ids = [entry['id'] for entry in self.manifest['entries']]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate entry id found')
        for entry_id in ids:
            self.assertRegex(entry_id, ID_SLUG_PATTERN)

    def test_every_entry_has_required_fields_only(self):
        for entry in self.manifest['entries']:
            self.assertEqual(set(entry.keys()), REQUIRED_FIELDS, msg=entry.get('id'))

    def test_family_and_bus_are_known_values(self):
        for entry in self.manifest['entries']:
            self.assertIn(entry['family'], FAMILIES, msg=entry['id'])
            self.assertIn(entry['bus'], BUSES, msg=entry['id'])

    def test_status_is_constrained_and_stage_one_entries_are_untested(self):
        for entry in self.manifest['entries']:
            self.assertIn(entry['status'], STATUSES, msg=entry['id'])
            # Stage 1 performs no physical-hardware testing: every entry must
            # remain untested until real evidence is recorded.
            self.assertEqual(entry['status'], 'untested', msg=entry['id'])

    def test_kernel_module_list_is_nonempty(self):
        for entry in self.manifest['entries']:
            self.assertIsInstance(entry['kernel_module'], list)
            self.assertGreater(len(entry['kernel_module']), 0, msg=entry['id'])

    def test_required_families_from_issue_scope_are_present(self):
        modules_present = set()
        for entry in self.manifest['entries']:
            modules_present.update(entry['kernel_module'])
        required_modules = {
            'iwlwifi', 'iwlmvm', 'iwldvm',
            'ath9k', 'ath10k_pci', 'ath11k_pci', 'ath12k',
            'mt7921e', 'mt7921u', 'mt7925e',
            'i915', 'xe', 'amdgpu', 'nouveau',
        }
        missing = required_modules - modules_present
        self.assertEqual(missing, set(), f'missing required module coverage: {missing}')


class IdentifierContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()

    def test_ids_object_requires_all_three_id_keys_plus_status(self):
        for entry in self.manifest['entries']:
            ids = entry['ids']
            self.assertEqual(set(ids.keys()), ID_KEYS, msg=entry['id'])
            for key in ('pci_id', 'usb_id', 'subsystem_id'):
                self.assertIn(key, ids, msg=entry['id'])
                if ids[key] is not None:
                    self.assertRegex(ids[key], ID_PATTERN, msg=f"{entry['id']}.{key}")

    def test_subsystem_status_distinguishes_pending_inventory_from_not_applicable(self):
        statuses = set()
        for entry in self.manifest['entries']:
            status = entry['ids']['subsystem_id_status']
            self.assertIn(status, {'recorded', 'pending_inventory', 'not_applicable'},
                          msg=entry['id'])
            statuses.add(status)
            if status != 'recorded':
                self.assertIsNone(entry['ids']['subsystem_id'], msg=entry['id'])
            else:
                self.assertRegex(entry['ids']['subsystem_id'], ID_PATTERN, msg=entry['id'])
        # Both meanings are actually used, so the distinction is not decorative.
        self.assertIn('pending_inventory', statuses)
        self.assertIn('not_applicable', statuses)

    def test_class_bound_entries_are_marked_not_applicable(self):
        by_id = entries_by_id(self.manifest)
        for entry_id in ('storage-nvme', 'storage-ahci', 'usb-xhci', 'hid-usb-generic',
                         'network-stack-cfg80211-mac80211', 'display-stack-drm-kms'):
            self.assertEqual(by_id[entry_id]['ids']['subsystem_id_status'], 'not_applicable',
                             msg=entry_id)

    def test_concrete_devices_are_marked_pending_inventory(self):
        by_id = entries_by_id(self.manifest)
        for entry_id in ('intel-wifi-ax210', 'mediatek-mt7921e', 'intel-gpu-i915-uhd630',
                         'amd-gpu-amdgpu-navi22'):
            self.assertEqual(by_id[entry_id]['ids']['subsystem_id_status'], 'pending_inventory',
                             msg=entry_id)

    def test_pci_or_usb_bus_entries_document_an_id_or_explain_why_not(self):
        for entry in self.manifest['entries']:
            if entry['bus'] not in ('pci', 'usb'):
                continue
            ids = entry['ids']
            if not (ids['pci_id'] or ids['usb_id']):
                # Class-bound drivers and unselected devices may omit an ID,
                # but only with a note saying so.
                self.assertTrue(entry['notes'].strip(), msg=entry['id'])


class EvidenceContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()

    def test_evidence_date_is_null_exactly_when_untested(self):
        for entry in self.manifest['entries']:
            if entry['status'] == 'untested':
                self.assertIsNone(entry['evidence_date'], msg=entry['id'])
            else:
                self.assertIsNotNone(entry['evidence_date'], msg=entry['id'])
                self.assertRegex(entry['evidence_date'], DATE_PATTERN, msg=entry['id'])

    def test_non_untested_entries_would_require_evidence_sources(self):
        for entry in self.manifest['entries']:
            if entry['status'] != 'untested':
                self.assertGreater(len(entry['evidence_sources']), 0, msg=entry['id'])

    def test_evidence_sources_are_structured_and_typed(self):
        for entry in self.manifest['entries']:
            self.assertIsInstance(entry['evidence_sources'], list)
            self.assertGreater(len(entry['evidence_sources']), 0, msg=entry['id'])
            for record in entry['evidence_sources']:
                self.assertEqual(set(record.keys()), {'kind', 'reference', 'retrieved', 'notes'},
                                 msg=entry['id'])
                self.assertIn(record['kind'], EVIDENCE_KINDS, msg=entry['id'])
                self.assertTrue(record['reference'].strip(), msg=entry['id'])
                if record['retrieved'] is not None:
                    self.assertRegex(record['retrieved'], DATE_PATTERN, msg=entry['id'])

    def test_no_entry_claims_a_physical_test_it_has_not_run(self):
        for entry in self.manifest['entries']:
            kinds = {record['kind'] for record in entry['evidence_sources']}
            self.assertNotIn('physical-test', kinds, msg=entry['id'])

    def test_schema_encodes_the_evidence_date_conditionals(self):
        conditions = load_schema()['$defs']['entry']['allOf']
        untested = next(c for c in conditions
                        if c['if']['properties'].get('status', {}).get('const') == 'untested')
        self.assertEqual(untested['then']['properties']['evidence_date']['type'], 'null')
        tested = next(c for c in conditions
                      if set(c['if']['properties'].get('status', {}).get('enum', []))
                      == {'verified', 'degraded', 'unsupported'})
        self.assertIn('evidence_date', tested['then']['required'])
        self.assertIn('evidence_sources', tested['then']['required'])
        self.assertEqual(tested['then']['properties']['evidence_sources']['minItems'], 1)

    def test_schema_requires_all_three_id_keys(self):
        required = load_schema()['$defs']['ids']['required']
        self.assertEqual(set(required), ID_KEYS)


class FirmwareProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()

    def test_firmware_requirements_are_explicit_groups(self):
        for entry in self.manifest['entries']:
            for group in entry['firmware_requirements']:
                self.assertEqual(set(group), {'id', 'any_of'}, msg=entry['id'])
                self.assertRegex(group['id'], ID_SLUG_PATTERN, msg=entry['id'])
                self.assertTrue(group['any_of'], msg=entry['id'])
                for pattern in group['any_of']:
                    self.assertTrue(pattern.strip(), msg=entry['id'])
            ids = [group['id'] for group in entry['firmware_requirements']]
            self.assertEqual(len(ids), len(set(ids)), msg=entry['id'])

    def test_firmware_files_stays_the_flattened_view_of_the_requirements(self):
        for entry in self.manifest['entries']:
            flattened = [pattern for group in entry['firmware_requirements']
                         for pattern in group['any_of']]
            self.assertEqual(entry['firmware_files'], flattened, msg=entry['id'])

    def test_co_required_firmware_is_split_into_separate_groups(self):
        by_id = entries_by_id(self.manifest)
        expected = {
            # A firmware image and its board data are both needed.
            'qualcomm-atheros-qca6174': ['qca6174-hw30-firmware', 'qca6174-hw30-board'],
            'qualcomm-atheros-wcn6855': ['wcn6855-hw20-amss', 'wcn6855-hw20-board'],
            # RAM code and the MCU patch are loaded together.
            'mediatek-mt7921e': ['mt7961-ram-code', 'mt7961-patch'],
            # GuC and HuC are separate images.
            'intel-gpu-xe-meteorlake': ['mtl-guc', 'mtl-huc'],
            # The DSP image is useless without its topology.
            'audio-sof-intel': ['sof-cnl-firmware', 'sof-cnl-topology'],
        }
        for entry_id, groups in expected.items():
            self.assertEqual([group['id'] for group in by_id[entry_id]['firmware_requirements']],
                             groups, msg=entry_id)
            for group in by_id[entry_id]['firmware_requirements']:
                self.assertEqual(len(group['any_of']), 1, msg=f'{entry_id}:{group["id"]}')

    def test_alternate_path_layouts_stay_inside_one_group(self):
        by_id = entries_by_id(self.manifest)
        for entry_id in ('intel-wifi-ax200', 'intel-wifi-ax210', 'intel-wifi-ax201-cnvi',
                         'intel-wifi-legacy-iwldvm'):
            groups = by_id[entry_id]['firmware_requirements']
            self.assertEqual(len(groups), 1, msg=entry_id)
            # The same microcode, packaged at the legacy root and under intel/.
            self.assertEqual(len(groups[0]['any_of']), 2, msg=entry_id)
            self.assertTrue(any(pattern.startswith('intel/iwlwifi/')
                                for pattern in groups[0]['any_of']), msg=entry_id)

    def test_entries_without_firmware_files_declare_no_requirements(self):
        for entry in self.manifest['entries']:
            if entry['firmware_files']:
                continue
            self.assertEqual(entry['firmware_requirements'], [], msg=entry['id'])

    def test_license_and_provenance_are_structured(self):
        for entry in self.manifest['entries']:
            self.assertEqual(set(entry['firmware_license'].keys()), LICENSE_KEYS, msg=entry['id'])
            self.assertEqual(set(entry['firmware_provenance'].keys()), PROVENANCE_KEYS,
                             msg=entry['id'])
            self.assertIn(entry['firmware_license']['status'],
                          {'declared', 'unresolved', 'not_applicable'}, msg=entry['id'])
            self.assertIn(entry['firmware_provenance']['status'],
                          {'packaged', 'unresolved', 'not_applicable'}, msg=entry['id'])

    def test_entries_with_firmware_files_declare_a_license_and_repository(self):
        for entry in self.manifest['entries']:
            if not entry['firmware_files']:
                continue
            licence = entry['firmware_license']
            self.assertIn(licence['status'], {'declared', 'unresolved'}, msg=entry['id'])
            self.assertTrue(licence['declared_by'], msg=entry['id'])
            self.assertTrue(licence['source'], msg=entry['id'])
            provenance = entry['firmware_provenance']
            self.assertIn(provenance['status'], {'packaged', 'unresolved'}, msg=entry['id'])
            if provenance['status'] == 'packaged':
                self.assertIn(provenance['alpine_repository'], {'main', 'community'},
                              msg=entry['id'])

    def test_entries_without_firmware_packages_are_marked_not_applicable(self):
        for entry in self.manifest['entries']:
            if entry['firmware_package']:
                continue
            self.assertEqual(entry['firmware_license']['status'], 'not_applicable', msg=entry['id'])
            self.assertEqual(entry['firmware_provenance']['status'], 'not_applicable',
                             msg=entry['id'])
            self.assertIsNone(entry['firmware_provenance']['alpine_repository'], msg=entry['id'])

    def test_sof_firmware_is_recorded_as_a_community_package_with_its_own_license(self):
        entry = entries_by_id(self.manifest)['audio-sof-intel']
        self.assertEqual(entry['firmware_package'], ['sof-firmware'])
        self.assertEqual(entry['firmware_provenance']['alpine_repository'], 'community')
        self.assertEqual(entry['firmware_license']['spdx'], 'BSD-3-Clause AND MIT AND ISC')

    def test_linux_firmware_entries_record_the_custom_license_and_main_repository(self):
        for entry in self.manifest['entries']:
            packages = entry['firmware_package']
            if not packages or not all(p.startswith('linux-firmware') for p in packages):
                continue
            self.assertEqual(entry['firmware_license']['status'], 'declared', msg=entry['id'])
            self.assertIn('WHENCE', entry['firmware_license']['source'], msg=entry['id'])
            if entry['firmware_provenance']['status'] == 'packaged':
                self.assertEqual(entry['firmware_provenance']['alpine_repository'], 'main',
                                 msg=entry['id'])

    def test_unresolved_provenance_explains_itself(self):
        for entry in self.manifest['entries']:
            if entry['firmware_provenance']['status'] == 'unresolved':
                self.assertTrue(entry['firmware_provenance']['notes'].strip(), msg=entry['id'])


class PackageAndModuleNamingTests(unittest.TestCase):
    """Mappings that must match the pinned Alpine v3.23 package/module naming."""

    def setUp(self):
        self.by_id = entries_by_id(load_manifest())

    def test_intel_graphics_firmware_uses_linux_firmware_i915(self):
        for entry_id in ('intel-gpu-i915-uhd630', 'intel-gpu-xe-meteorlake'):
            self.assertEqual(self.by_id[entry_id]['firmware_package'], ['linux-firmware-i915'],
                             msg=entry_id)

    def test_realtek_nic_firmware_uses_linux_firmware_rtl_nic(self):
        entry = self.by_id['ethernet-realtek-r8168']
        self.assertEqual(entry['firmware_package'], ['linux-firmware-rtl_nic'])
        self.assertTrue(all(path.startswith('rtl_nic/') for path in entry['firmware_files']))

    def test_no_entry_still_points_at_linux_firmware_other(self):
        for entry in self.by_id.values():
            self.assertNotIn('linux-firmware-other', entry['firmware_package'], msg=entry['id'])

    def test_acpi_modules_use_upstream_names(self):
        entry = self.by_id['acpi-core']
        self.assertEqual(entry['kernel_module'], ['battery', 'thermal', 'button', 'ac'])

    def test_no_entry_uses_invented_acpi_module_names(self):
        for entry in self.by_id.values():
            for module in entry['kernel_module']:
                self.assertNotIn(module, {'acpi_battery', 'acpi_thermal'}, msg=entry['id'])

    def test_mediatek_mt7921_firmware_paths_match_the_packaged_layout(self):
        for entry_id in ('mediatek-mt7921e', 'mediatek-mt7921u'):
            for path in self.by_id[entry_id]['firmware_files']:
                self.assertTrue(path.startswith('mediatek/'), msg=path)
                self.assertFalse(path.startswith('mediatek/mt7921/'), msg=path)

    def test_navi22_firmware_uses_its_upstream_codename(self):
        entry = self.by_id['amd-gpu-amdgpu-navi22']
        self.assertEqual(entry['firmware_files'], ['amdgpu/navy_flounder_*.bin'])
        for path in entry['firmware_files']:
            self.assertNotIn('navi22', path)

    def test_turing_entry_does_not_claim_a_nonexistent_firmware_path(self):
        entry = self.by_id['nvidia-nouveau-turing-tu117']
        self.assertEqual(entry['firmware_files'], [])
        self.assertEqual(entry['firmware_provenance']['status'], 'unresolved')
        self.assertIn('tu117', entry['firmware_provenance']['notes'])

    def test_display_stack_does_not_list_userspace_packages_as_firmware(self):
        entry = self.by_id['display-stack-drm-kms']
        self.assertEqual(entry['firmware_package'], [])
        self.assertIn('mesa-dri-gallium', entry['notes'])


class VersionRequirementTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()
        self.by_id = entries_by_id(self.manifest)

    def test_minimums_are_structured_not_free_text(self):
        for entry in self.manifest['entries']:
            for field in ('minimum_kernel', 'minimum_mesa'):
                value = entry[field]
                self.assertIsInstance(value, dict, msg=f"{entry['id']}.{field}")
                self.assertEqual(set(value.keys()), VERSION_KEYS, msg=f"{entry['id']}.{field}")
                self.assertIn(value['status'],
                              {'required', 'none_recorded', 'unresolved', 'not_applicable'},
                              msg=f"{entry['id']}.{field}")
                self.assertTrue(value['reason'].strip(), msg=f"{entry['id']}.{field}")

    def test_required_versions_carry_a_version_and_an_evidence_source(self):
        found = 0
        for entry in self.manifest['entries']:
            for field in ('minimum_kernel', 'minimum_mesa'):
                value = entry[field]
                if value['status'] != 'required':
                    continue
                found += 1
                self.assertTrue(value['version'], msg=f"{entry['id']}.{field}")
                self.assertTrue(value['evidence_source'], msg=f"{entry['id']}.{field}")
        self.assertGreater(found, 0, 'expected at least one structured version floor')

    def test_non_required_versions_do_not_smuggle_a_version_or_verdict(self):
        for entry in self.manifest['entries']:
            for field in ('minimum_kernel', 'minimum_mesa'):
                value = entry[field]
                if value['status'] == 'required':
                    continue
                self.assertIsNone(value['version'], msg=f"{entry['id']}.{field}")
                self.assertIsNone(value['meets_baseline'], msg=f"{entry['id']}.{field}")

    def test_newer_drivers_record_their_kernel_floor(self):
        self.assertEqual(self.by_id['mediatek-mt7925e']['minimum_kernel']['version'], '6.7')
        self.assertEqual(self.by_id['intel-gpu-xe-meteorlake']['minimum_kernel']['version'], '6.8')

    def test_unselected_device_records_an_unresolved_floor(self):
        entry = self.by_id['qualcomm-ath12k-conditional']
        self.assertEqual(entry['minimum_kernel']['status'], 'unresolved')
        self.assertIsNone(entry['minimum_kernel']['version'])


class RepresentativeMachineTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest()
        self.by_id = entries_by_id(self.manifest)

    def test_representative_machines_are_structured(self):
        for entry in self.manifest['entries']:
            machine = entry['representative_machine']
            self.assertEqual(set(machine.keys()), MACHINE_KEYS, msg=entry['id'])
            self.assertIn(machine['availability'],
                          {'unconfirmed', 'available', 'not_applicable'}, msg=entry['id'])
            self.assertIn(machine['selection_status'],
                          {'candidate', 'selected', 'not_applicable'}, msg=entry['id'])
            self.assertTrue(machine['notes'].strip(), msg=entry['id'])

    def test_named_candidates_are_marked_unconfirmed_not_owned(self):
        for entry in self.manifest['entries']:
            machine = entry['representative_machine']
            if machine['selection_status'] != 'candidate':
                continue
            self.assertTrue(machine['candidate'].strip(), msg=entry['id'])
            self.assertIn(machine['availability'], {'unconfirmed', 'available'}, msg=entry['id'])

    def test_core_stack_entries_have_no_representative_machine(self):
        for entry_id in ('network-stack-cfg80211-mac80211', 'display-stack-drm-kms'):
            machine = self.by_id[entry_id]['representative_machine']
            self.assertIsNone(machine['candidate'], msg=entry_id)
            self.assertEqual(machine['availability'], 'not_applicable', msg=entry_id)

    def test_concrete_hardware_families_name_a_candidate(self):
        for entry_id in ('intel-wifi-ax200', 'intel-wifi-ax210', 'qualcomm-atheros-qca6174',
                         'mediatek-mt7921e', 'intel-gpu-i915-uhd630', 'intel-gpu-xe-meteorlake',
                         'amd-gpu-amdgpu-renoir', 'nvidia-nouveau-turing-tu117'):
            machine = self.by_id[entry_id]['representative_machine']
            self.assertTrue(machine['candidate'], msg=entry_id)
            self.assertEqual(machine['availability'], 'unconfirmed', msg=entry_id)
            self.assertEqual(self.by_id[entry_id]['status'], 'untested', msg=entry_id)

    def test_only_the_virtual_nic_claims_an_available_machine(self):
        available = {entry['id'] for entry in self.manifest['entries']
                     if entry['representative_machine']['availability'] == 'available'}
        self.assertEqual(available, {'ethernet-virtio-net'})

    def test_no_entry_says_not_yet_selected_without_structure(self):
        for entry in self.manifest['entries']:
            self.assertNotEqual(entry['representative_machine'].get('candidate'),
                                'not yet selected', msg=entry['id'])


class ManifestPinnedInputConsistencyTests(unittest.TestCase):
    def test_alpine_branch_matches_build_env(self):
        manifest = load_manifest()
        build_env = load_build_env()
        self.assertEqual(
            manifest['alpine_branch'], build_env['ALPINE_BRANCH'],
            'docs/qa/hardware-coverage.json alpine_branch has drifted from '
            'distro/alpine/build.env ALPINE_BRANCH; review every version and '
            'firmware-package claim before updating either pin.',
        )


if __name__ == '__main__':
    unittest.main()
