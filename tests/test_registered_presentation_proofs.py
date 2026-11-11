"""Native normal-export thumbnail aliases, without changing content identity."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from zipfile import ZipFile

from lxp_catalogue import PackageValidationError
from lxp_export import published_course
from lxp_multi_export import learning_compatibility_fingerprint, validate_product_contract
from lxp_presentation_compat import build_proof, registered_proof_payloads
import test_multi_framework_export as fixtures
from test_presentation_compatibility import source, IDENTITY


class RegisteredProofTest(unittest.TestCase):
    def setUp(self):
        self.previous = source()
        self.previous['course']['_boxMenu'] = {'_graphic': {'_src': 'course/assets/old.png', 'alt': 'Old'}, 'other': 'keep'}
        self.current = deepcopy(self.previous)
        self.current['course']['_boxMenu']['_graphic'] = {'_src': 'course/assets/new.png', 'alt': 'New'}
        self.current['data']['contentObjects'][0]['_graphic']['src'] = 'course/assets/new.png'
        self.envelope, _ = build_proof(self.previous, self.current, IDENTITY, learning_compatibility_fingerprint)
        self.registration = {IDENTITY['packageId']: {self.envelope['currentFingerprint']: [self.envelope]}}

    def check(self, registration=None, current=None):
        return registered_proof_payloads(self.registration if registration is None else registration,
            self.current if current is None else current, IDENTITY, learning_compatibility_fingerprint)

    def test_cover_and_menu_graphics_are_explicit_full_replay_aliases(self):
        payloads = self.check()
        self.assertEqual(len(payloads), 1)
        path, raw, row = payloads[0]
        self.assertEqual(row['path'], path)
        self.assertEqual(json.loads(raw), self.envelope)
        self.assertEqual(row['componentDisplayTitles'], [])
        self.assertNotEqual(self.envelope['previousFingerprint'], self.envelope['currentFingerprint'])

    def test_other_offer_has_no_implicit_alias(self):
        self.assertEqual(self.check({'another-package': {self.envelope['currentFingerprint']: [self.envelope]}}), [])
        later = deepcopy(self.current); later['data']['components'][1]['body'] = 'A later real content change'
        self.assertEqual(self.check(current=later), [])

    def test_cover_rule_never_changes_other_course_or_menu_settings(self):
        for change in (lambda data: data['course']['_boxMenu'].update(other='changed'),
                       lambda data: data['course'].update(title='Changed'),
                       lambda data: data['config'].update(_isTrackable=False),
                       lambda data: data['data']['components'][1].update(body='Other question'),
                       lambda data: data['course']['_boxMenu']['_graphic'].update(_src='https://example.org/image.png')):
            current = deepcopy(self.current); change(current)
            with self.subTest(change=change), self.assertRaises(PackageValidationError):
                build_proof(self.previous, current, IDENTITY, learning_compatibility_fingerprint)

    def test_missing_old_cover_is_not_a_replacement(self):
        previous = deepcopy(self.previous); previous['course'].pop('_boxMenu')
        with self.assertRaises(PackageValidationError):
            build_proof(previous, self.current, IDENTITY, learning_compatibility_fingerprint)

    def test_registry_is_bounded_and_duplicate_stale_or_altered_proofs_fail(self):
        bad = deepcopy(self.envelope); bad['currentFingerprint'] = '0' * 64
        wrong = deepcopy(self.envelope); wrong['identity']['courseId'] = 'different'
        unknown = deepcopy(self.envelope); unknown['extra'] = True
        for values in ([bad], [wrong], [unknown], [self.envelope] * 2, [self.envelope] * 3, []):
            with self.subTest(values=len(values)), self.assertRaises(PackageValidationError):
                self.check({IDENTITY['packageId']: {self.envelope['currentFingerprint']: values}})
        with self.assertRaises(PackageValidationError):
            self.check({str(i): {self.envelope['currentFingerprint']: [self.envelope]} for i in range(9)})


class NormalExportProofTest(unittest.TestCase):
    setUp = fixtures.MixedPackageTest.setUp
    write_wrapper = fixtures.MixedPackageTest.write_wrapper
    add_source = fixtures.MixedPackageTest.add_source
    prepare = fixtures.MixedPackageTest.prepare
    export = fixtures.MixedPackageTest.export

    def registered_transition(self):
        with ZipFile(self.wrapper) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        marker = json.loads(entries['adapt-multi-framework-capabilities.json'])
        marker['presentationCompatibility'] = 1
        entries['adapt-multi-framework-capabilities.json'] = json.dumps(marker).encode()
        with ZipFile(self.wrapper, 'w') as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        build = self.builds[0]
        previous = published_course(build)
        previous['course']['_boxMenu'] = {'_graphic': {'_src': 'course/de/assets/old.png', 'alt': 'Old'}}
        graph = previous['data']
        graph['contentObjects'][0].update(_markledgeMenuContent={'_sourceKey': 'A/lesson.md'},
                                         _graphic={'src': 'course/de/assets/old.png', 'alt': 'Old'})
        for name, kind in [('articles', 'article'), ('blocks', 'block'), ('components', 'component')]:
            for row in graph[name]:
                row['_type'] = kind
        for name in ('old.png',):
            file = build / 'course/de/assets' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'fixture ' + name.encode())
        self.save_source(build, previous)
        baseline = self.prepare()
        self.baseline_contract = deepcopy(baseline.contract)
        sco = next(s for s in baseline.contract['scos'] if s.get('source', {}).get('originId') == 'tenant-a')
        current = deepcopy(previous)
        current['course']['_boxMenu']['_graphic']['_src'] = 'course/de/assets/new.png'
        current['data']['contentObjects'][0]['_graphic']['src'] = 'course/de/assets/new.png'
        (build / 'course/de/assets/new.png').write_bytes(b'new thumbnail fixture')
        self.save_source(build, current)
        identity = {'packageId': baseline.contract['package']['id'], 'scoId': sco['id'],
                    'originId': sco['source']['originId'], 'courseId': sco['source']['courseId']}
        envelope, _ = build_proof(previous, current, identity, learning_compatibility_fingerprint)
        registry = json.loads(self.registry.read_text())
        row = next(row for row in registry['sources'].values() if row['originId'] == 'tenant-a')
        row['presentationCompatibilityProofs'] = {identity['packageId']: {envelope['currentFingerprint']: [envelope]}}
        fixtures.save(self.registry, registry)
        return self.prepare(revision='r2'), envelope

    def save_source(self, build, source):
        fixtures.save(build / 'course/de/course.json', source['course'])
        for name, rows in source['data'].items():
            fixtures.save(build / ('course/de/' + name + '.json'), rows)

    def test_normal_export_packages_replayed_frozen_proof_and_keeps_test_builds(self):
        prepared, envelope = self.registered_transition()
        with self.export(prepared) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            sco = next(s for s in contract['scos'] if 'presentationCompatibility' in s['source'])
            proof = sco['source']['presentationCompatibility']['proofs'][0]
            self.assertEqual(json.loads(archive.read(proof['path'])), envelope)
            root = self.root / 'verified-package'; archive.extractall(root)
        self.assertTrue(validate_product_contract(contract, root))
        old_tests = {s['id']: s['source']['buildSha256'] for s in self.baseline_contract['scos'] if s['role'] != 'learning'}
        self.assertEqual(old_tests, {s['id']: s['source']['buildSha256'] for s in contract['scos'] if s['role'] != 'learning'})
        self.assertEqual(contract['questions'], self.baseline_contract['questions'])
        self.assertEqual(contract['assessments'], self.baseline_contract['assessments'])
        repeated = self.prepare(revision='r3')
        self.assertEqual(old_tests, {s['id']: s['source']['buildSha256'] for s in repeated.contract['scos'] if s['role'] != 'learning'})
        with self.export(repeated) as archive:
            for test in (s for s in contract['scos'] if s['role'] != 'learning'):
                prefix = test['launch']['root'] + '/course/en/assets/'
                self.assertFalse(any(name.startswith(prefix) and name.endswith('/new.png') for name in archive.namelist()))
            learning_root = sco['launch']['root']
            self.assertIn(learning_root + '/course/de/assets/new.png', archive.namelist())
            old_test = next(s for s in contract['scos'] if s['role'] != 'learning' and s['frameworkId'] == sco['frameworkId'])
            self.assertIn(old_test['launch']['root'] + '/course/en/assets/' + sco['id'] + '/course/old.png', archive.namelist())

    def test_tampered_frozen_proof_never_builds(self):
        prepared, _ = self.registered_transition()
        sco = next(s for s in prepared.contract['scos'] if 'presentationCompatibility' in s['source'])
        file = Path(prepared._temporary.name) / sco['source']['presentationCompatibility']['proofs'][0]['path']
        file.write_bytes(file.read_bytes() + b' ')
        with self.assertRaisesRegex(PackageValidationError, 'proof SHA256 differs'):
            self.export(prepared)

    def test_real_question_change_exports_normally_without_reusing_historical_alias(self):
        self.registered_transition()
        file = self.builds[0] / 'course/de/components.json'
        rows = json.loads(file.read_text()); rows[0]['body'] = 'Actual content revision'
        fixtures.save(file, rows)
        prepared = self.prepare(revision='r3')
        self.assertFalse(any('presentationCompatibility' in s['source'] for s in prepared.contract['scos']))
        with self.export(prepared) as archive:
            self.assertFalse(any(name.startswith('presentation-compatibility/') for name in archive.namelist()))
            self.assertTrue(any('/course/en/assets/' in name and name.endswith('/new.png') for name in archive.namelist()))

    def test_another_offer_can_use_the_sources_without_inheriting_alias(self):
        self.registered_transition()
        prepared = self.prepare(package_id='another-offer', revision='r1')
        self.assertFalse(any('presentationCompatibility' in s['source'] for s in prepared.contract['scos']))
        with self.export(prepared) as archive:
            self.assertFalse(any(name.startswith('presentation-compatibility/') for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
