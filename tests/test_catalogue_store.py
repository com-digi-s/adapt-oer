"""Safe instance overrides: no source mutation or silent stale-form writes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from lxp_catalogue import PackageValidationError, catalogue_sha256, load_catalogue
from lxp_catalogue_store import (
    CatalogueConflictError, MAX_CATALOGUE_BYTES, read_snapshot, save_catalogue,
)


class CatalogueStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.base = load_catalogue()
        self.original = deepcopy(self.base)
        self.path = self.directory / 'instance' / 'catalogue.json'

    def changed(self, label='New label'):
        candidate = read_snapshot(self.base, self.path)['catalogue']
        candidate['frameworks'][0]['label'] = label
        return candidate

    def save(self, candidate=None, expected=None, **kwargs):
        expected = expected or read_snapshot(self.base, self.path)['sha256']
        return save_catalogue(self.base, self.path, candidate or self.changed(), expected, **kwargs)

    def backups(self):
        return sorted(self.path.with_name(self.path.name + '.backups').glob('*.json'))

    def test_read_has_no_side_effects_and_returns_independent_canonical_snapshot(self):
        snapshot = read_snapshot(self.base, self.path)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(snapshot['sha256'], catalogue_sha256(self.base))
        snapshot['catalogue']['frameworks'][0]['label'] = 'Local mutation'
        self.assertEqual(self.base, self.original)
        self.assertEqual(read_snapshot(self.base, self.path)['catalogue'], self.original)

    def test_existing_one_letter_ids_survive_normal_admin_save(self):
        framework = self.base['frameworks'][0]
        framework['id'] = 'F'
        framework['modules'][0]['id'] = 'M'
        framework['domains'][0]['id'] = 'D'
        framework['defaultDomainId'] = 'D'
        snapshot = read_snapshot(self.base, self.path)
        candidate = deepcopy(self.base)
        candidate['frameworks'][0]['label'] = 'Bestehende Kennungen bleiben erhalten'
        saved = save_catalogue(self.base, self.path, candidate, snapshot['sha256'])['catalogue']['frameworks'][0]
        self.assertEqual((saved['id'], saved['modules'][0]['id'], saved['domains'][0]['id']), ('F', 'M', 'D'))

    def test_save_is_versioned_and_override_survives_restart_without_changing_inputs(self):
        base_path = self.directory / 'configured.json'
        configured_bytes = json.dumps(self.base, indent=4, ensure_ascii=False).encode('utf-8')
        base_path.write_bytes(configured_bytes)
        candidate = self.changed()
        original_candidate = deepcopy(candidate)
        result = save_catalogue(base_path, self.path, candidate, read_snapshot(base_path, self.path)['sha256'])
        self.assertNotEqual(result['catalogue']['revision'], self.base['revision'])
        self.assertEqual(result['sha256'], catalogue_sha256(result['catalogue']))
        self.assertEqual(read_snapshot(base_path, self.path), result)
        self.assertEqual(base_path.read_bytes(), configured_bytes)
        self.assertEqual(candidate, original_candidate)
        self.assertEqual(self.base, self.original)
        self.assertEqual(self.backups()[0].read_bytes(), configured_bytes)
        self.assertEqual(self.backups()[0].stem, hashlib.sha256(configured_bytes).hexdigest())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups()[0].stat().st_mode & 0o777, 0o600)

    def test_none_base_loads_production_catalogue_and_none_override_is_read_only(self):
        self.assertEqual(read_snapshot(None, None)['catalogue'], load_catalogue())
        with self.assertRaisesRegex(PackageValidationError, 'explicit catalogue override'):
            save_catalogue(self.base, None, self.changed(), catalogue_sha256(self.base))

    def test_noop_ignores_user_revision_without_writing_or_backups(self):
        candidate = deepcopy(self.base)
        candidate['revision'] = 'user-must-not-control-revision'
        result = self.save(candidate)
        self.assertEqual(result, read_snapshot(self.base, None))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.backups(), [])

    def test_each_write_backs_up_exact_previous_version(self):
        first = self.save()
        previous_bytes = self.path.read_bytes()
        second = self.save(self.changed('Second label'))
        self.assertNotEqual(first['catalogue']['revision'], second['catalogue']['revision'])
        self.assertEqual(len(self.backups()), 2)
        self.assertIn(previous_bytes, [path.read_bytes() for path in self.backups()])
        self.assertEqual(len({path.stem for path in self.backups()}), 2)

    def test_stale_form_is_rejected_with_current_hash_and_no_extra_write(self):
        stale = read_snapshot(self.base, self.path)
        first = self.save()
        before = self.path.read_bytes()
        with self.assertRaises(CatalogueConflictError) as raised:
            self.save(self.changed('Stale edit'), stale['sha256'])
        self.assertEqual(raised.exception.current_sha256, first['sha256'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_concurrent_forms_cannot_overwrite_each_other(self):
        snapshot = read_snapshot(self.base, self.path)
        barrier = threading.Barrier(2)

        def submit(label):
            candidate = deepcopy(snapshot['catalogue'])
            candidate['frameworks'][0]['label'] = label
            barrier.wait(timeout=5)
            try:
                return self.save(candidate, snapshot['sha256'])
            except CatalogueConflictError:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, ['First browser', 'Second browser']))
        self.assertEqual(outcomes.count('conflict'), 1)
        winner = next(result for result in outcomes if isinstance(result, dict))
        self.assertEqual(read_snapshot(self.base, self.path), winner)
        self.assertEqual(len(self.backups()), 1)

    def test_invalid_hash_is_rejected_before_storage_creation(self):
        for value in [None, '', '0', 'X' * 64, 42]:
            with self.subTest(value=value), self.assertRaisesRegex(PackageValidationError, 'snapshot hash'):
                save_catalogue(self.base, self.path, self.changed(), value)
        self.assertFalse(self.path.parent.exists())

    def test_framework_module_and_domain_deletions_are_forbidden(self):
        mutations = [
            lambda c: c['frameworks'].pop(),
            lambda c: c['frameworks'][0]['modules'].pop(),
            lambda c: c['frameworks'][0]['domains'].pop(),
            lambda c: c['frameworks'][0]['modules'][0]['courseNumbers'].pop(),
        ]
        for mutate in mutations:
            candidate = deepcopy(self.base)
            mutate(candidate)
            with self.subTest(mutate=mutate), self.assertRaises(PackageValidationError):
                self.save(candidate)
        self.assertFalse(self.path.exists())

    def test_existing_ids_prefix_version_and_module_number_are_immutable(self):
        mutations = [
            lambda c: c['frameworks'][0].update(id='replacement-framework'),
            lambda c: c['frameworks'][0].update(prefix='Other'),
            lambda c: c['frameworks'][0].update(version='3.0'),
            lambda c: c['frameworks'][0]['modules'][0].update(id='replacement-module'),
            lambda c: c['frameworks'][0]['modules'][0].update(number='99'),
            lambda c: c['frameworks'][0]['domains'][1].update(id='replacement-domain'),
        ]
        for mutate in mutations:
            candidate = deepcopy(self.base)
            mutate(candidate)
            with self.subTest(mutate=mutate), self.assertRaises(PackageValidationError):
                self.save(candidate)

    def test_existing_module_cannot_be_moved_to_another_framework(self):
        candidate = deepcopy(self.base)
        module = candidate['frameworks'][0]['modules'].pop()
        module['number'] = '99'
        candidate['frameworks'][1]['modules'].append(module)
        with self.assertRaisesRegex(PackageValidationError, 'framework ownership'):
            self.save(candidate)

    def test_labels_domain_markers_and_diagnostic_defaults_are_editable(self):
        candidate = deepcopy(self.base)
        framework = candidate['frameworks'][0]
        framework['label'] = 'Rahmen – überarbeitet'
        framework['modules'][0]['label'] = 'Neue Modulbezeichnung'
        framework['domains'][1].update(label='Praktisch anwenden', sourceTitles=['B – Praxis'], sourceTitlePatterns=['^Praxis'])
        framework['defaultDomainId'] = framework['domains'][1]['id']
        framework['defaultDiagnosticIntents'] = {'entry': 'ifAvailable', 'final': 'omit'}
        saved = self.save(candidate)['catalogue']['frameworks'][0]
        self.assertEqual(saved['label'], framework['label'])
        self.assertEqual(saved['domains'], framework['domains'])
        self.assertEqual(saved['defaultDiagnosticIntents'], framework['defaultDiagnosticIntents'])

    def test_new_registered_prefix_version_module_and_domain_can_be_added(self):
        candidate = deepcopy(self.base)
        candidate['frameworks'][0]['modules'].append({'id': 'mod-digcomp22-6', 'number': '6', 'label': 'Neues Modul', 'courseNumbers': []})
        candidate['frameworks'][0]['domains'].append({'id': 'gestalten', 'label': 'Gestalten', 'sourceTitles': ['D – Gestalten']})
        candidate['frameworks'].extend([
            {'id': 'fw-ailit-next', 'prefix': 'AILit', 'version': '2027', 'label': 'AILit 2027', 'modules': [], 'domains': []},
            {'id': 'fw-media-1', 'prefix': 'Media', 'version': '1', 'label': 'Media 1', 'modules': [], 'domains': []},
        ])
        result = self.save(candidate)['catalogue']
        self.assertEqual(len(result['frameworks']), 4)
        self.assertEqual(result['frameworks'][0]['modules'][-1]['courseNumbers'], [])
        self.assertEqual(self.base, self.original)

    def test_assessment_defaults_use_explicit_typed_options_and_allowed_reference(self):
        candidate = deepcopy(self.base)
        candidate['frameworks'][0]['defaultAssessmentOptions'] = {
            'entry': {'mode': 'reference', 'questionCount': 24, 'equalSampling': True,
                      'showMarking': False, 'showModelAnswer': True},
            'final': {'mode': 'reference', 'questionCount': 39},
        }
        candidate['frameworks'][1]['defaultAssessmentOptions'] = {
            'entry': {'mode': 'balanced', 'questionCount': 12, 'equalSampling': False},
            'final': {'mode': 'all', 'questionCount': None},
        }
        result = self.save(candidate)['catalogue']
        for actual, submitted in zip(result['frameworks'], candidate['frameworks']):
            self.assertEqual(actual['defaultAssessmentOptions'], submitted['defaultAssessmentOptions'])

    def test_unknown_malformed_or_inconsistent_assessment_defaults_are_rejected(self):
        values = [
            None, [], {'typo': {}}, {'entry': []}, {'entry': {'unknown': True}},
            {'entry': {'mode': 'invented'}}, {'entry': {'questionCount': 0}},
            {'entry': {'questionCount': -1}}, {'entry': {'questionCount': True}},
            {'entry': {'questionCount': 1.5}}, {'entry': {'questionCount': '12'}},
            {'entry': {'questionCount': None}}, {'entry': {'equalSampling': 'yes'}},
            {'entry': {'showMarking': 1}}, {'entry': {'showModelAnswer': None}},
            {'entry': {'mode': 'all', 'questionCount': 12}},
            {'entry': {'mode': 'reference', 'questionCount': 25}},
            {'final': {'mode': 'reference', 'questionCount': 24}},
        ]
        for value in values:
            candidate = deepcopy(self.base)
            candidate['frameworks'][0]['defaultAssessmentOptions'] = value
            with self.subTest(options=value), self.assertRaises(PackageValidationError):
                self.save(candidate)
        for prefix, version in [('AILit', '2026'), ('DigComp', '3.0')]:
            candidate = deepcopy(self.base)
            framework = deepcopy(next(item for item in candidate['frameworks'] if item['prefix'] == prefix))
            framework.update(id='fw-new-version', version=version,
                             modules=[], defaultAssessmentOptions={'entry': {'mode': 'reference'}})
            if prefix == 'AILit':
                framework['version'] = 'future'
            candidate['frameworks'].append(framework)
            with self.subTest(prefix=prefix, version=version), self.assertRaisesRegex(PackageValidationError, 'DigComp 2.2'):
                self.save(candidate)

    def test_protected_registry_bindings_keep_versions_modules_and_dynamic_course_slots(self):
        binding = {'frameworkId': self.base['frameworks'][0]['id'], 'prefix': 'DigComp', 'version': '2.2',
                   'moduleNumber': '1', 'courseNumber': '99'}
        registry = {'sources': {'origin:course': {'binding': binding}, 'new-course': {'courseId': 'new'}}}
        self.save(protected_bindings=registry)
        for changed in [dict(binding, version='99'), dict(binding, frameworkId='missing'), dict(binding, moduleNumber='99'),
                        dict(binding, domainId='missing'), dict(binding, moduleId='wrong-module')]:
            with self.subTest(binding=changed), self.assertRaisesRegex(PackageValidationError, 'binding'):
                self.save(self.changed('Another edit'), protected_bindings=[changed])

    def test_protected_binding_input_is_not_silently_ignored(self):
        for value in [42, ['wrong'], {'sources': ['wrong']}, [{'binding': 'wrong'}], [{'frameworkId': []}]]:
            with self.subTest(value=value), self.assertRaises(PackageValidationError):
                self.save(protected_bindings=value)

    def test_invalid_types_patterns_defaults_ids_and_ambiguous_prefixes_are_rejected(self):
        mutations = [
            lambda c: c.update(version=True),
            lambda c: c.update(frameworks=[42]),
            lambda c: c['frameworks'][0].update(modules={}),
            lambda c: c['frameworks'][0]['modules'][0].update(courseNumbers='1'),
            lambda c: c['frameworks'][0]['modules'][0].update(courseLabels=[]),
            lambda c: c['frameworks'][0]['modules'][0].update(id='../unsafe'),
            lambda c: c['frameworks'][0]['domains'][0].update(sourceTitlePatterns=['[']),
            lambda c: c['frameworks'][0]['domains'][0].update(sourceTitlePatterns=['(' * 1000]),
            lambda c: c['frameworks'][0]['domains'][0].update(sourceTitles='A'),
            lambda c: c['frameworks'][0].update(defaultDiagnosticIntents={'entry': 'invented'}),
            lambda c: c['frameworks'][0].update(defaultDiagnosticIntents={'typo': 'omit'}),
            lambda c: c['frameworks'][0].update(defaultDomainId='unknown'),
            lambda c: c['frameworks'][0].update(defaultDomainId=42),
            lambda c: c['frameworks'][0].update(prefix='Media2'),
            lambda c: c['frameworks'].append({'id': 'case-only', 'prefix': 'digcomp', 'version': '99', 'label': 'Collision'}),
        ]
        for mutate in mutations:
            candidate = deepcopy(self.base)
            mutate(candidate)
            with self.subTest(mutate=mutate), self.assertRaises(PackageValidationError):
                self.save(candidate)
        self.assertFalse(self.path.exists())

    def test_nonfinite_nonjson_surrogate_and_oversized_values_are_rejected(self):
        for value in [float('nan'), float('inf'), (1, 2), {1: 'bad key'}, '\ud800', 'x' * MAX_CATALOGUE_BYTES]:
            candidate = deepcopy(self.base)
            candidate['extra'] = value
            with self.subTest(value_type=type(value)), self.assertRaises(PackageValidationError):
                self.save(candidate)
        self.assertFalse(self.path.parent.exists())

    def test_invalid_overlay_fails_closed_instead_of_falling_back_to_base(self):
        self.path.parent.mkdir()
        for data in [b'{', b'\xff', b'{"format":"one","format":"two"}', b'{"extra":NaN}', b'x' * (MAX_CATALOGUE_BYTES + 1)]:
            self.path.write_bytes(data)
            with self.subTest(data_length=len(data)), self.assertRaises(PackageValidationError):
                read_snapshot(self.base, self.path)
        self.assertEqual(self.base, self.original)

    def test_original_configured_file_cannot_be_used_as_override(self):
        source = self.directory / 'source.json'
        source.write_text(json.dumps(self.base), encoding='utf-8')
        before = source.read_bytes()
        for operation in [lambda: read_snapshot(source, source),
                          lambda: save_catalogue(source, source, self.changed(), catalogue_sha256(self.base))]:
            with self.assertRaisesRegex(PackageValidationError, 'separate'):
                operation()
        self.assertEqual(source.read_bytes(), before)

    def test_symlink_store_or_lock_or_backup_directory_is_rejected(self):
        self.path.parent.mkdir()
        target = self.directory / 'target.json'
        target.write_text('unchanged', encoding='utf-8')
        self.path.symlink_to(target)
        with self.assertRaises(PackageValidationError):
            read_snapshot(self.base, self.path)
        with self.assertRaises(PackageValidationError):
            self.save(self.changed_from_base())
        self.path.unlink()
        lock = self.path.with_name(self.path.name + '.lock')
        lock.symlink_to(target)
        with self.assertRaises(PackageValidationError):
            self.save(self.changed_from_base())
        lock.unlink()
        backup_directory = self.path.with_name(self.path.name + '.backups')
        backup_directory.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(PackageValidationError):
            self.save(self.changed_from_base())
        self.assertEqual(target.read_text(), 'unchanged')

    def changed_from_base(self):
        value = deepcopy(self.base)
        value['frameworks'][0]['label'] = 'Safe attempted edit'
        return value

    def test_failed_atomic_replace_preserves_current_file_and_backup(self):
        self.save()
        before = self.path.read_bytes()
        with patch('lxp_catalogue_store.os.replace', side_effect=OSError('simulated write failure')):
            with self.assertRaisesRegex(PackageValidationError, 'simulated write failure'):
                self.save(self.changed('Failed edit'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn(before, [path.read_bytes() for path in self.backups()])
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_backup_corruption_prevents_edit_instead_of_overwriting_history(self):
        self.save()
        before = self.path.read_bytes()
        backup_directory = self.path.with_name(self.path.name + '.backups')
        corrupted = backup_directory / (hashlib.sha256(before).hexdigest() + '.json')
        corrupted.write_bytes(b'corrupt existing history')
        with self.assertRaisesRegex(PackageValidationError, 'backup integrity'):
            self.save(self.changed('Blocked edit'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(corrupted.read_bytes(), b'corrupt existing history')

    def test_recovery_backup_can_only_be_applied_as_validated_new_revision(self):
        first = self.save()
        first_bytes = self.path.read_bytes()
        self.save(self.changed('Second edit'))
        recovered = self.save(json.loads(first_bytes))
        self.assertEqual(recovered['catalogue']['frameworks'][0]['label'], first['catalogue']['frameworks'][0]['label'])
        self.assertNotEqual(recovered['catalogue']['revision'], first['catalogue']['revision'])
        self.assertEqual(len(self.backups()), 3)


if __name__ == '__main__':
    unittest.main()
