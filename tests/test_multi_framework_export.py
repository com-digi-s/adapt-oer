"""Production-contract regressions. These small sources are test data, not demos."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from lxp_catalogue import (PackageValidationError, canonical_bytes, catalogue_sha256,
                           load_catalogue, normalize_title)
from lxp_export import prepare_multi_framework_export, build_multi_framework_export, published_course
from lxp_multi_export import (get_source_binding, register_native_source, safe_relative,
                              tree_manifest, validate_archive_entries, validate_product_contract,
                              approve_legacy_source, safe_file_path, learning_compatibility_fingerprint)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = load_catalogue()

    def test_exact_prefix_multidigit_slots_and_no_float_identity(self):
        self.catalogue['frameworks'][0]['modules'][4]['courseNumbers'].append('10')
        mapping = normalize_title(' DigComp5.10 - Zehn ', self.catalogue)
        self.assertEqual((mapping['moduleNumber'], mapping['courseNumber'], mapping['label']), ('5', '10', 'Zehn'))
        self.assertNotEqual(mapping['courseNumber'], normalize_title('DigComp5.1 – Eins', self.catalogue)['courseNumber'])
        self.assertEqual(normalize_title('AILit1.1 – KI', self.catalogue)['prefix'], 'AILit')

    def test_unknown_prefix_invalid_segments_and_empty_titles_fail(self):
        for title in ('AILti1.1 – Fehler', '1.1 – Legacy', 'DigComp1.01 – Null', 'DigComp01.1 – Null',
                      'AILit1.1.1 – Extra', 'AILit0.1 – Null', 'AILit-1.1 – Negativ', 'AILit1.1 – ',
                      'AILit1.5 – Unknown', 'DigComp5.10 – Not in default catalogue'):
            with self.subTest(title=title), self.assertRaises(PackageValidationError):
                normalize_title(title, self.catalogue)
        self.assertEqual(normalize_title('1.1 – Alt', self.catalogue, legacy_import=True)['prefix'], 'DigComp')

    def test_ambiguous_versions_require_choice_and_bound_version_survives_default_change(self):
        second = deepcopy(self.catalogue['frameworks'][0])
        second.update(id='fw-digcomp-other', version='future')
        for module in second['modules']:
            module['id'] += '-future'
        self.catalogue['frameworks'].append(second)
        with self.assertRaisesRegex(PackageValidationError, 'eindeutig'):
            normalize_title('DigComp1.1 – Test', self.catalogue)
        self.assertEqual(normalize_title('DigComp1.1 – Test', self.catalogue, bound_version='2.2')['version'], '2.2')
        with self.assertRaisesRegex(PackageValidationError, 'migration'):
            normalize_title('DigComp1.1 – Test', self.catalogue, bound_version='2.2', framework_versions={'DigComp': 'future'})

    def test_proposal_and_fixture_catalogues_are_never_production(self):
        for field, value in (('format', 'adapt-lxp-catalogue-proposal'), ('exampleOnly', True), ('version', 99)):
            bad = deepcopy(self.catalogue)
            bad[field] = value
            with self.assertRaisesRegex(PackageValidationError, 'Production catalogue'):
                load_catalogue(bad)


class MixedPackageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / 'registry.json'
        self.wrapper = self.root / 'wrapper.zip'
        self.template = Path(__file__).resolve().parents[1] / 'static/assessment-template.zip'
        self.catalogue = load_catalogue()
        # AILit diagnosis is only enabled in this test-specific explicit domain
        # configuration. Production's AILit catalogue has no invented domains.
        ail = self.catalogue['frameworks'][1]
        ail['domains'] = [{'id': 'explicit-test-domain', 'label': 'Explicit domain'}]
        ail['defaultDomainId'] = 'explicit-test-domain'
        self.write_wrapper()
        self.builds, self.metadata = [], []
        self.add_source('digcomp', 'DigComp1.1 – Digitaler Kurs', 'tenant-a', 'aat-1')
        self.add_source('ailit', 'AILit1.1 – KI-Kurs', 'tenant-b', 'aat-1')

    def write_wrapper(self, *, capability=True):
        implementation = {
            'index.html': '<html><script type="module">if (window.CONF.MULTI_FRAMEWORK_CONTRACT) await import("./js/multiFramework.js");</script></html>',
            'js/multiFramework.js': "class App { attachAdaptPersistence() {} }; fetch('lxp-contract.json'); let frameworkId, assessmentId;",
            'js/corePersistence.js': '// Unit file-hash fixture, not an integration runtime',
            'js/multiContract.js': '// Unit contract module fixture',
            'js/multiState.js': '// Unit state module fixture',
            'js/multiBridge.js': '// Unit bridge module fixture',
            'js/multiAssessment.js': '// Unit assessment module fixture'}
        files = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in implementation.items()}
        marker = {'capability': 'multi-framework', 'version': 1, 'packageContract': 'adapt-lxp-multi-framework@1',
                  'persistenceContract': 1, 'launchContext': 'explicit-sco-id', 'implementationFiles': files,
                  'implementationSha256': hashlib.sha256(canonical_bytes(files)).hexdigest()}
        with ZipFile(self.wrapper, 'w') as archive:
            archive.writestr('config.json', json.dumps({'SCOS': {'M0': {}}, 'MULTI_FRAMEWORK_CONTRACT': 'lxp-contract.json'}))
            archive.writestr('adapt-persistence-contract.json', json.dumps({'version': 1, 'bootstrap': 'parent.App.attachAdaptPersistence', 'adapter': 'js/corePersistence.js'}))
            for name, value in implementation.items():
                archive.writestr(name, value)
            if capability:
                archive.writestr('adapt-multi-framework-capabilities.json', json.dumps(marker))

    def add_source(self, directory, title, origin, course_id):
        build = self.root / directory
        save(build / 'course/config.json', {'_defaultLanguage': 'de'})
        save(build / 'course/de/course.json', {'_id': 'course', 'title': title})
        data = {'contentObjects': [{'_id': 'page', '_parentId': 'course', '_type': 'page', 'title': 'Original page'}],
                'articles': [{'_id': 'article', '_parentId': 'page'}],
                'blocks': [{'_id': 'block', '_parentId': 'article'}],
                'components': [{'_id': 'same-component', '_parentId': 'block', '_component': 'mcq', '_classes': 'et at',
                                'body': '<p>Wirkliche Frage</p><img src="course/de/assets/test.svg">',
                                '_items': [{'text': 'Correct', '_shouldBeSelected': True}, {'text': 'Wrong'}]}]}
        for name, values in data.items():
            save(build / ('course/de/' + name + '.json'), values)
        runtime = build / 'adapt/js/adapt.min.js'
        runtime.parent.mkdir(parents=True)
        runtime.write_text('var Adapt$1=__AMD("core/js/adapt",Adapt);')
        (build / 'index_lms.html').write_text('<html>actual entry</html>')
        assets = build / 'course/de/assets'
        assets.mkdir()
        (assets / 'test.svg').write_text('<svg>' + origin + '</svg>')
        (build / 'imsmanifest.xml').write_text('<manifest><organizations default="o"><organization identifier="o"><item identifierref="r"/></organization></organizations><resources><resource identifier="r" href="index_lms.html"/></resources></manifest>')
        self.builds.append(build)
        self.metadata.append({'kind': 'aat-published', 'originId': origin, 'courseId': course_id, 'currentTitle': title})
        return build

    def prepare(self, **overrides):
        options = {'source_metadata': self.metadata, 'registry_path': self.registry, 'catalogue': self.catalogue,
                   'package_id': 'pkg-stable-offer', 'revision': 'r1', 'title': 'Gemeinsames Lernangebot',
                   'diagnostics': {'DigComp': {'entry': 'require', 'final': 'require'}, 'AILit': {'entry': 'require', 'final': 'require'}}}
        options.update(overrides)
        builds = options.pop('builds', self.builds)
        result = prepare_multi_framework_export(builds, self.wrapper, self.template, **options)
        self.addCleanup(result.close)
        return result

    def export(self, prepared):
        return ZipFile(build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256))

    def change_question(self, index=0, **changes):
        path = self.builds[index] / 'course/de/components.json'
        questions = json.loads(path.read_text())
        questions[0].update(changes)
        save(path, questions)

    def test_one_zip_real_manifest_per_framework_banks_and_no_mutation(self):
        originals = [tree_manifest(b) for b in self.builds]
        prepared = self.prepare()
        with self.export(prepared) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            self.assertFalse(contract['exampleOnly'])
            self.assertEqual(contract['format'], 'adapt-lxp-multi-framework')
            self.assertEqual(len(contract['frameworks']), 2)
            self.assertEqual(len(contract['assessments']), 4)
            self.assertEqual(len(contract['questions']), 2)
            self.assertEqual({a['banks'][0]['runtimeBankId'] for a in contract['assessments']}, {1})
            self.assertEqual(len({a['banks'][0]['id'] for a in contract['assessments']}), 4)
            for assessment in contract['assessments']:
                bank = assessment['banks'][0]
                course = next(c for c in contract['scos'] if c['id'] == bank['targetCourseId'])
                self.assertEqual(course['frameworkId'], assessment['frameworkId'])
                test_sco = next(c for c in contract['scos'] if c['id'] == assessment['scoId'])
                components = json.loads(archive.read(test_sco['launch']['root'] + '/course/en/components.json'))
                self.assertIn(course['id'], components[0]['_id'])
                self.assertIn('/assets/' + course['id'] + '/', components[0]['body'])
                self.assertEqual(test_sco['source']['kind'], 'generated-assessment')
                self.assertEqual(test_sco['source']['catalogueSha256'], catalogue_sha256(self.catalogue))
            manifest = ET.fromstring(archive.read('imsmanifest.xml'))
            resources = [n for n in manifest.iter() if n.tag.endswith('}resource')]
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0].get('href'), 'index.html')
            self.assertNotIn('SCOS', json.loads(archive.read('config.json')))
            provenance = json.loads(archive.read('package-provenance.json'))
            self.assertEqual(provenance['version'], 2)
            self.assertTrue(all(c['copyChanges'][0]['kind'] == 'persistence-bootstrap' for c in provenance['courses']))
            for sco in contract['scos']:
                if sco['role'] == 'learning':
                    self.assertEqual(sco['launch']['entry'], 'index_lms.html')
                    self.assertIn(sco['launch']['root'] + '/index_lms.html', archive.namelist())
        self.assertEqual(originals, [tree_manifest(b) for b in self.builds])

    def test_source_order_title_and_optional_settings_do_not_change_identity(self):
        first = self.prepare()
        first_contract = deepcopy(first.contract)
        reordered = self.prepare(builds=list(reversed(self.builds)), source_metadata=list(reversed(self.metadata)))
        self.assertEqual(first.contract, reordered.contract)
        self.assertEqual(first.preflight_sha256, reordered.preflight_sha256)
        title = 'DigComp1.1 – Umbenannt ohne Identitätswechsel'
        save(self.builds[0] / 'course/de/course.json', {'_id': 'course', 'title': title})
        self.metadata[0]['currentTitle'] = title
        renamed = self.prepare(required_courses={'tenant-a::aat-1': False})
        old = next(s for s in first_contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
        new = next(s for s in renamed.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
        self.assertEqual(old['id'], new['id'])
        self.assertEqual(old['source']['componentFingerprint'], new['source']['componentFingerprint'])
        self.assertFalse(new['requiredForCompletion'])
        self.assertEqual(get_source_binding(self.registry, origin_id='tenant-a', course_id='aat-1')['scoId'], old['id'])

    def test_completion_fingerprint_binds_ancestor_flags_hierarchy_and_completion_rules(self):
        source = published_course(self.builds[0])
        before = learning_compatibility_fingerprint(source)
        for collection in ('contentObjects', 'articles', 'blocks'):
            for key, value in (('_isOptional', True), ('_isAvailable', False), ('_isTrackable', False),
                               ('_requireCompletionOf', 2), ('_assessment', {'_isEnabled': True}), ('_parentId', 'changed-parent')):
                with self.subTest(collection=collection, field=key):
                    changed = deepcopy(source)
                    changed['data'][collection][0][key] = value
                    self.assertNotEqual(before, learning_compatibility_fingerprint(changed))
        changed = deepcopy(source)
        changed['config']['_completionCriteria'] = {'_requireContentCompleted': False}
        self.assertNotEqual(before, learning_compatibility_fingerprint(changed))
        changed = deepcopy(source)
        changed['data']['components'].append({'_id': 'new-required', '_parentId': 'block', '_component': 'text', '_isOptional': False})
        self.assertNotEqual(before, learning_compatibility_fingerprint(changed))
        changed = deepcopy(source)
        changed['course'].update(title='New technical course title', displayTitle='New display title')
        for collection in ('contentObjects', 'articles', 'blocks'):
            changed['data'][collection][0].update(title='New label', displayTitle='New display label')
        self.assertEqual(before, learning_compatibility_fingerprint(changed))

    def test_structural_update_changes_fingerprint_but_identical_revision_update_does_not(self):
        first = self.prepare()
        course = next(s for s in first.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
        revision_only = self.prepare(revision='r2')
        unchanged = next(s for s in revision_only.contract['scos'] if s['id'] == course['id'])
        self.assertEqual(course['source']['componentFingerprint'], unchanged['source']['componentFingerprint'])
        self.assertEqual([s['source']['buildSha256'] for s in first.contract['scos'] if s['role'] != 'learning'],
                         [s['source']['buildSha256'] for s in revision_only.contract['scos'] if s['role'] != 'learning'])
        path = self.builds[0] / 'course/de/blocks.json'
        values = json.loads(path.read_text())
        values[0]['_isOptional'] = True
        save(path, values)
        structural = self.prepare(revision='r3')
        changed = next(s for s in structural.contract['scos'] if s['id'] == course['id'])
        self.assertNotEqual(course['source']['componentFingerprint'], changed['source']['componentFingerprint'])
        with self.export(structural):
            pass  # Delivered validation uses the exact same semantic hash.

    def test_unpublished_title_and_changed_preview_are_closed_errors(self):
        wrong = deepcopy(self.metadata)
        wrong[0]['currentTitle'] = 'DigComp1.1 – Unpublished'
        with self.assertRaisesRegex(PackageValidationError, 'erneut veröffentlichen'):
            self.prepare(source_metadata=wrong)
        prepared = self.prepare()
        with self.assertRaisesRegex(PackageValidationError, 'confirmation'):
            build_multi_framework_export(prepared, confirmed_preflight_sha256='0' * 64)
        with self.assertRaisesRegex(PackageValidationError, 'AAT source'):
            build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256, source_metadata=wrong)
        (self.builds[1] / 'index_lms.html').write_text('Changed after confirmation')
        with self.assertRaisesRegex(PackageValidationError, 'changed since preview'):
            self.export(prepared)

    def test_manifest_bearing_aat_build_cannot_claim_another_source_course_id(self):
        path = self.builds[0] / 'course/config.json'
        save(path, {'_defaultLanguage': 'de', '_courseId': 'wrong-course-from-another-build'})
        self.assertTrue((self.builds[0] / 'imsmanifest.xml').is_file())
        with self.assertRaisesRegex(PackageValidationError, 'AAT _courseId differs'):
            self.prepare()
        save(path, {'_defaultLanguage': 'de', '_courseId': 'aat-1'})
        prepared = self.prepare()
        with self.export(prepared):
            pass
        # Explicit native registration is its own identity model, not an AAT
        # tenant/course assertion about the native package's internal metadata.
        save(path, {'_defaultLanguage': 'de', '_courseId': 'original-native-internal-id'})
        self.metadata[0]['kind'] = 'native-registered'
        register_native_source(self.registry, origin_id='tenant-a', course_id='aat-1')
        self.assertEqual(len(self.prepare().contract['frameworks']), 2)

    def test_catalogue_wrapper_and_contract_mutation_invalidate_preview(self):
        catalogue_file = self.root / 'catalogue.json'
        save(catalogue_file, self.catalogue)
        prepared = self.prepare(catalogue=catalogue_file)
        changed = deepcopy(self.catalogue)
        changed['revision'] += '-changed'
        save(catalogue_file, changed)
        with self.assertRaisesRegex(PackageValidationError, 'Catalogue changed'):
            self.export(prepared)
        prepared = self.prepare()
        prepared.contract['package']['title'] = 'Mutable forgery'
        with self.assertRaisesRegex(PackageValidationError, 'Prepared contract'):
            self.export(prepared)
        prepared = self.prepare()
        self.write_wrapper(capability=False)
        with self.assertRaisesRegex(PackageValidationError, 'Wrapper/template changed'):
            self.export(prepared)

    def test_omit_does_not_manufacture_or_silently_validate_unrequested_tests(self):
        self.change_question(index=1, _component='not-supported')
        prepared = self.prepare(diagnostics={'DigComp': {'entry': 'require', 'final': 'require'}, 'AILit': {'entry': 'omit', 'final': 'omit'}})
        ail = next(f for f in prepared.contract['frameworks'] if f['prefix'] == 'AILit')
        self.assertEqual(ail['diagnostics']['entry'], {'intent': 'omit', 'status': 'notProvided', 'reason': 'not-requested'})
        self.assertEqual(len(prepared.contract['assessments']), 2)
        with self.assertRaisesRegex(PackageValidationError, 'Unsupported diagnostic'):
            self.prepare()

    def test_if_available_missing_is_honest_but_unsupported_and_unavailable_block(self):
        self.change_question(index=1, _classes='')
        prepared = self.prepare(diagnostics={'AILit': {'entry': 'ifAvailable', 'final': 'ifAvailable'}})
        ail = next(f for f in prepared.contract['frameworks'] if f['prefix'] == 'AILit')
        self.assertEqual(ail['diagnostics']['entry']['reason'], 'no-assigned-questions')
        with self.assertRaisesRegex(PackageValidationError, 'required diagnostic'):
            self.prepare()
        self.change_question(index=1, _classes='et at', _isAvailable=False)
        with self.assertRaisesRegex(PackageValidationError, 'individually unavailable'):
            self.prepare(diagnostics={'AILit': {'entry': 'ifAvailable', 'final': 'omit'}})
        self.change_question(index=1, _classes='et at self-assessment', _isAvailable=True)
        with self.assertRaisesRegex(PackageValidationError, 'required diagnostic'):
            self.prepare()

    def test_native_registration_is_explicit_and_four_courses_keep_nineteen_pages(self):
        # An actual four-SCO/19-page preservation regression, not a flattening
        # test. Questions remain in the learning copies and tests are omitted.
        self.builds, self.metadata = [], []
        for i, pages in enumerate((5, 5, 5, 4), 1):
            build = self.add_source('native-' + str(i), 'AILit1.' + str(i) + ' – Native ' + str(i), 'native:registered', 'native-' + str(i))
            self.metadata[-1]['kind'] = 'native-registered'
            save(build / 'course/de/contentObjects.json', [{'_id': 'page' + str(j), '_parentId': 'course', '_type': 'page'} for j in range(pages)])
        with self.assertRaisesRegex(PackageValidationError, 'explicitly registered'):
            self.prepare(diagnostics={'AILit': {'entry': 'omit', 'final': 'omit'}})
        for meta in self.metadata:
            register_native_source(self.registry, origin_id=meta['originId'], course_id=meta['courseId'])
        before = [tree_manifest(b) for b in self.builds]
        prepared = self.prepare(diagnostics={'AILit': {'entry': 'omit', 'final': 'omit'}})
        with self.export(prepared) as archive:
            self.assertEqual(len(prepared.contract['scos']), 4)
            total = 0
            for source in prepared.contract['scos']:
                prefix = source['launch']['root']
                total += len(json.loads(archive.read(prefix + '/course/de/contentObjects.json')))
                original = next(b for b, meta in zip(self.builds, self.metadata) if meta['courseId'] == source['source']['courseId'])
                self.assertEqual(archive.read(prefix + '/course/de/components.json'), (original / 'course/de/components.json').read_bytes())
            self.assertEqual(total, 19)
        self.assertEqual(before, [tree_manifest(b) for b in self.builds])

    def test_missing_empty_required_set_and_duplicate_address_fail(self):
        with self.assertRaisesRegex(PackageValidationError, 'Pflichtumfang'):
            self.prepare(required_courses={'tenant-a::aat-1': False, 'tenant-b::aat-1': False})
        self.add_source('duplicate', 'DigComp1.1 – Other source', 'tenant-c', 'aat-2')
        with self.assertRaisesRegex(PackageValidationError, 'Duplicate selected framework'):
            self.prepare()

    def test_fixture_provenance_unknown_requested_framework_and_phases_fail(self):
        bad = deepcopy(self.metadata)
        bad[0]['kind'] = 'fixture'
        with self.assertRaisesRegex(PackageValidationError, 'never fixture'):
            self.prepare(source_metadata=bad)
        with self.assertRaisesRegex(PackageValidationError, 'unknown framework'):
            self.prepare(diagnostics={'AILti': {'entry': 'require'}})
        with self.assertRaisesRegex(PackageValidationError, 'unknown phase'):
            self.prepare(diagnostics={'AILit': {'entrance': 'require'}})

    def test_only_explicit_known_source_legacy_approval_accepts_numeric_titles(self):
        title = '1.1 – Bekannter alter DigComp-Kurs'
        save(self.builds[0] / 'course/de/course.json', {'_id': 'course', 'title': title})
        self.metadata[0]['currentTitle'] = title
        with self.assertRaisesRegex(PackageValidationError, 'explicitly approved'):
            self.prepare(legacy_import=True)
        approve_legacy_source(self.registry, origin_id='tenant-a', course_id='aat-1', current_title=title, catalogue=self.catalogue)
        prepared = self.prepare(legacy_import=True)
        course = next(s for s in prepared.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
        self.assertEqual(course['frameworkId'], 'fw-digcomp-22')
        with self.assertRaisesRegex(PackageValidationError, 'numeric'):
            approve_legacy_source(self.registry, origin_id='unknown', course_id='new', current_title='AILti1.1 – Wrong', catalogue=self.catalogue)

    def test_new_framework_does_not_inherit_flat_digcomp_domain_fallback(self):
        self.catalogue['frameworks'][1].pop('defaultDomainId')
        with self.assertRaisesRegex(PackageValidationError, 'explicitly configured diagnostic domain'):
            self.prepare()
        prepared = self.prepare(diagnostics={'AILit': {'entry': 'omit', 'final': 'omit'}})
        self.assertEqual(len(prepared.contract['assessments']), 2)

    def test_reassignment_requires_confirmation_and_does_not_recycle_id(self):
        prepared = self.prepare()
        old = next(s['id'] for s in prepared.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
        title = 'DigComp1.2 – Same source new address'
        save(self.builds[0] / 'course/de/course.json', {'_id': 'course', 'title': title})
        self.metadata[0]['currentTitle'] = title
        with self.assertRaisesRegex(PackageValidationError, 'Zuordnungsänderung'):
            self.prepare()
        confirmed = self.prepare(confirm_reassignment=True)
        self.assertIn(old, [s['id'] for s in confirmed.contract['scos']])

    def test_capability_is_not_just_a_cosmetic_json_marker(self):
        self.write_wrapper(capability=False)
        with self.assertRaisesRegex(PackageValidationError, 'multi-framework capability'):
            self.prepare()
        self.write_wrapper()
        with ZipFile(self.wrapper) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files['js/corePersistence.js'] = b'changed after capability build'
        with ZipFile(self.wrapper, 'w') as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(PackageValidationError, 'implementation SHA-256'):
            self.prepare()

    def test_sparse_or_inactive_wrapper_with_self_consistent_hashes_cannot_export(self):
        for inactive in (False, True):
            self.write_wrapper()
            with ZipFile(self.wrapper) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
            marker = json.loads(files['adapt-multi-framework-capabilities.json'])
            if inactive:
                files['index.html'] = b'<html>No multi-framework bootstrap</html>'
                marker['implementationFiles']['index.html'] = hashlib.sha256(files['index.html']).hexdigest()
            else:
                marker['implementationFiles'] = {name: digest for name, digest in marker['implementationFiles'].items() if name in {'js/corePersistence.js', 'js/multiFramework.js'}}
            marker['implementationSha256'] = hashlib.sha256(canonical_bytes(marker['implementationFiles'])).hexdigest()
            files['adapt-multi-framework-capabilities.json'] = json.dumps(marker).encode()
            with ZipFile(self.wrapper, 'w') as archive:
                for name, raw in files.items():
                    archive.writestr(name, raw)
            with self.assertRaisesRegex(PackageValidationError, 'seven runtime|does not match'):
                self.prepare()

    def test_real_source_encoded_aliases_fail_before_preview_and_output_is_self_validated(self):
        (self.builds[0] / 'fileA.txt').write_text('one')
        (self.builds[0] / 'file%41.txt').write_text('two')
        with self.assertRaisesRegex(PackageValidationError, 'collide after URL decoding'):
            self.prepare()

    def test_source_and_wrapper_static_assets_keep_legitimate_spaces(self):
        filename = 'course/de/assets/2024_07_16_ Final_Lehrplan_ComDigiS.pdf'
        original = b'unchanged static PDF test bytes'
        (self.builds[0] / filename).write_bytes(original)
        wrapper_filename = 'views/dashboard/static/2024_07_16_ Final_Lehrplan_ComDigiS.pdf'
        with ZipFile(self.wrapper, 'a') as archive:
            archive.writestr(wrapper_filename, original)
        prepared = self.prepare()
        with self.export(prepared) as archive:
            validate_archive_entries(archive)
            sco = next(s for s in prepared.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
            self.assertEqual(archive.read(sco['launch']['root'] + '/' + filename), original)
            self.assertEqual(archive.read(wrapper_filename), original)

    def test_manifest_resource_is_actual_and_never_guesses_index(self):
        manifest = self.builds[0] / 'imsmanifest.xml'
        manifest.unlink()
        with self.assertRaisesRegex(PackageValidationError, 'no imsmanifest'):
            self.prepare()
        manifest.write_text('<manifest><organizations default="o"><organization identifier="o"><item identifierref="r"/></organization></organizations><resources><resource identifier="r" href="../outside.html"/></resources></manifest>')
        with self.assertRaisesRegex(PackageValidationError, 'traversal'):
            self.prepare()

    def test_explicit_known_aat_core_build_profile_generates_declared_copy_manifest(self):
        build = self.builds[0]
        (build / 'imsmanifest.xml').unlink()
        (build / 'index_lms.html').unlink()
        (build / 'index.html').write_text('<script>window.ADAPT_BUILD_TYPE="production";</script><script src="adapt/js/scriptLoader.js"></script><link href="adapt.css">')
        (build / 'adapt.css').write_text('/* real build anchor */')
        (build / 'templates.js').write_text('// real build anchor')
        (build / 'adapt/js/scriptLoader.js').write_text("loadScript('adapt/js/adapt.min.js');")
        save(build / 'course/config.json', {'_defaultLanguage': 'de', '_courseId': 'aat-1'})
        with self.assertRaisesRegex(PackageValidationError, 'explicit verified'):
            self.prepare()
        self.metadata[0]['profile'] = 'aat-core-build-v1'
        before = tree_manifest(build)
        prepared = self.prepare()
        source = next(s for s in prepared.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-a')
        self.assertEqual(source['launch']['entry'], 'index.html')
        self.assertEqual(source['source']['manifestOrigin'], 'generated-aat-core-build')
        self.assertTrue(source['source']['launchAdapter']['originalManifestAbsent'])
        with self.export(prepared) as archive:
            raw = archive.read(source['launch']['root'] + '/imsmanifest.xml')
            self.assertEqual(hashlib.sha256(raw).hexdigest(), source['source']['manifest']['sha256'])
            provenance = json.loads(archive.read('package-provenance.json'))
            report = next(c for c in provenance['courses'] if c['scoId'] == source['id'])
            self.assertEqual(report['copyChanges'][1]['kind'], 'generated-core-build-manifest')
        self.assertEqual(before, tree_manifest(build))
        self.assertFalse((build / 'imsmanifest.xml').exists())
        self.metadata[0]['courseId'] = 'forged-id'
        with self.assertRaisesRegex(PackageValidationError, 'authenticated source'):
            self.prepare()

    def test_core_build_adapter_does_not_override_enabled_spoor_or_native_sources(self):
        build = self.builds[0]
        (build / 'imsmanifest.xml').unlink()
        self.metadata[0]['profile'] = 'aat-core-build-v1'
        save(build / 'course/config.json', {'_defaultLanguage': 'de', '_courseId': 'aat-1', '_spoor': {'_isEnabled': True}})
        with self.assertRaisesRegex(PackageValidationError, 'Spoor'):
            self.prepare()
        self.metadata[0]['kind'] = 'native-registered'
        register_native_source(self.registry, origin_id='tenant-a', course_id='aat-1')
        with self.assertRaisesRegex(PackageValidationError, 'explicit verified'):
            self.prepare()

    def test_explicit_pathway_and_glossary_resolve_source_ids_only(self):
        fw = self.catalogue['frameworks'][1]['id']
        self.catalogue['pathways'] = [{'id': 'path-ail', 'frameworkId': fw, 'label': 'KI-Weg', 'courseRefs': [{'originId': 'tenant-b', 'courseId': 'aat-1'}]}]
        self.catalogue['glossary'] = [{'id': 'term-ail', 'frameworkId': fw, 'term': 'Quellenprüfung', 'targets': [{'originId': 'tenant-b', 'courseId': 'aat-1', 'contentId': 'page'}]}]
        prepared = self.prepare()
        course_id = next(s['id'] for s in prepared.contract['scos'] if s['role'] == 'learning' and s['frameworkId'] == fw)
        self.assertEqual(prepared.contract['pathways'][0]['courseIds'], [course_id])
        self.assertEqual(prepared.contract['glossary'][0]['targets'], [{'scoId': course_id, 'contentId': 'page'}])

    def test_delivered_product_validator_checks_scores_source_aliases_and_provenance(self):
        prepared = self.prepare()
        destination = self.root / 'delivered'
        with self.export(prepared) as archive:
            archive.extractall(destination)
        self.assertTrue(validate_product_contract(prepared.contract, destination))
        changed = deepcopy(prepared.contract)
        changed['assessments'][0]['banks'][0]['maxPoints'] += 1
        with self.assertRaisesRegex(PackageValidationError, 'Bank maximum'):
            validate_product_contract(changed, destination)
        changed = deepcopy(prepared.contract)
        bank = changed['assessments'][0]['banks'][0]
        original = next(q for q in changed['questions'] if q['id'] == bank['questionIds'][0])
        alias = {**original, 'id': 'q-alias-must-not-count-twice'}
        changed['questions'].append(alias)
        bank['questionIds'].append(alias['id'])
        bank['maxPoints'] += alias['maxPoints']
        with self.assertRaisesRegex(PackageValidationError, 'counted twice'):
            validate_product_contract(changed, destination)
        changed = deepcopy(prepared.contract)
        generated = next(s for s in changed['scos'] if s['role'] != 'learning')
        generated['source']['questions'][0]['sourceComponentId'] = 'not-the-real-source-question'
        with self.assertRaisesRegex(PackageValidationError, 'provenance differs'):
            validate_product_contract(changed, destination)


class SupportedComponentsTest(unittest.TestCase):
    setUp = MixedPackageTest.setUp
    write_wrapper = MixedPackageTest.write_wrapper
    add_source = MixedPackageTest.add_source
    prepare = MixedPackageTest.prepare
    export = MixedPackageTest.export
    change_question = MixedPackageTest.change_question

    def test_each_supported_component_preserves_scoring_configuration_and_weight(self):
        components = {
            'mcq': {'_items': [{'text': 'A', '_shouldBeSelected': True}, {'text': 'B', '_shouldBeSelected': True}, {'text': 'C'}]},
            'gmcq': {'_items': [{'text': 'A', '_shouldBeSelected': True, '_graphic': {'src': 'course/de/assets/test.svg', 'alt': 'Image'}}]},
            'matching': {'_items': [{'_options': [{'text': 'Yes', '_isCorrect': True}, {'text': 'No'}]}]},
            'slider': {'_scaleStart': 0, '_scaleEnd': 10, '_scaleStep': 1, '_correctAnswer': 5},
            'textinput': {'_items': [{'_answers': ['Ärger', 'ärger']}], '_allowsAnyCase': True},
            'infai-dragndrop': {'_items': [{'accepted': ['a']}, {'accepted': ['b']}]},
        }
        for component, attributes in components.items():
            with self.subTest(component=component):
                self.change_question(_component=component, _questionWeight=2, **attributes)
                prepared = self.prepare(diagnostics={'DigComp': {'entry': 'require', 'final': 'omit'}, 'AILit': {'entry': 'omit', 'final': 'omit'}})
                question = prepared.contract['questions'][0]
                runtime_type = 'mcq' if component == 'gmcq' else component
                self.assertEqual(question['component'], runtime_type)
                self.assertEqual(question['sourceComponentType'], component)
                self.assertEqual(question['runtimeComponent'], runtime_type)
                if component == 'gmcq':
                    self.assertEqual(question['componentAdapter'], 'gmcq-to-mcq-v1')
                self.assertEqual(question['maxPoints'], 4 if component in {'mcq', 'infai-dragndrop'} else 2)
                with self.export(prepared) as archive:
                    sco = next(s for s in prepared.contract['scos'] if s['role'] == 'entry')
                    generated = json.loads(archive.read(sco['launch']['root'] + '/course/en/components.json'))[0]
                    self.assertEqual(generated['_questionWeight'], 2)
                    self.assertEqual(generated['_component'], 'mcq' if component == 'gmcq' else component)

    def test_slider_zero_and_ranges_use_the_real_native_model_fields(self):
        self.change_question(_component='slider', _scaleStart=0, _scaleEnd=10, _scaleStep=1, _correctAnswer=0)
        prepared = self.prepare()
        with self.export(prepared) as archive:
            sco = next(s for s in prepared.contract['scos'] if s['role'] == 'entry' and s['frameworkId'] == 'fw-digcomp-22')
            generated = json.loads(archive.read(sco['launch']['root'] + '/course/en/components.json'))[0]
            self.assertEqual(generated['_correctRange'], {'_bottom': 0, '_top': 0})
            self.assertEqual(generated['_scaleEnd'], 10)
        self.change_question(_correctAnswer=10.5)
        with self.assertRaisesRegex(PackageValidationError, 'not on its native scale'):
            self.prepare()

    def test_native_textinput_generic_answers_legacy_alias_and_source_global_weight(self):
        self.change_question(_component='textinput', _items=[{'answers': ['Ärger', 'ärger']}])
        save(self.builds[0] / 'course/config.json', {'_defaultLanguage': 'de', '_questionWeight': 3})
        prepared = self.prepare()
        question = next(q for q in prepared.contract['questions'] if q['sourceCourseId'] == next(s['id'] for s in prepared.contract['scos'] if s['role'] == 'learning' and s['frameworkId'] == 'fw-digcomp-22'))
        self.assertEqual(question['maxPoints'], 3)
        with self.export(prepared) as archive:
            sco = next(s for s in prepared.contract['scos'] if s['role'] == 'entry' and s['frameworkId'] == 'fw-digcomp-22')
            generated = json.loads(archive.read(sco['launch']['root'] + '/course/en/components.json'))[0]
            self.assertEqual(generated['_items'][0]['_answers'], ['Ärger', 'ärger'])
            self.assertEqual(generated['_questionWeight'], 3)
        self.change_question(_answers=[['A'], ['B']], _items=[{}, {}])
        self.assertEqual(len(self.prepare().contract['questions']), 2)

    def test_native_drag_accepts_string_or_multi_item_groups_and_counts_real_slots(self):
        self.change_question(_component='infai-dragndrop', _items=[{'accepted': 'one'}, {'accepted': ['two', 'three']}], _questionWeight=2)
        prepared = self.prepare()
        question = next(q for q in prepared.contract['questions'] if q['component'] == 'infai-dragndrop')
        self.assertEqual(question['maxPoints'], 6)
        with self.export(prepared) as archive:
            sco = next(s for s in prepared.contract['scos'] if s['role'] == 'entry' and s['frameworkId'] == 'fw-digcomp-22')
            generated = json.loads(archive.read(sco['launch']['root'] + '/course/en/components.json'))[0]
            self.assertEqual(generated['_items'][0]['accepted'], ['one'])

    def test_malformed_native_items_and_non_native_slider_schema_fail_closed(self):
        for component, values in [('matching', {'_items': [{'text': 'bad', '_options': [None]}]}),
                                  ('mcq', {'_items': {'not': 'a list'}}),
                                  ('slider', {'_items': [], '_scale': {'_min': 0, '_max': 10, '_correctAnswer': 5}})]:
            with self.subTest(component=component):
                self.change_question(_component=component, **values)
                with self.assertRaises(PackageValidationError):
                    self.prepare()


class LaunchAndArchiveSecurityTest(unittest.TestCase):
    def test_physical_filenames_allow_spaces_without_relaxing_launch_urls(self):
        name = 'static/2024_07_16_ Final_Lehrplan_ComDigiS.pdf'
        self.assertEqual(safe_file_path(name), name)
        self.assertEqual(safe_file_path('static/ Final #1?.pdf'), 'static/ Final #1?.pdf')
        with self.assertRaises(PackageValidationError):
            safe_relative(name)
        for name in ('../escape with space', 'dir/../escape', '/absolute with space', 'dir\\backslash space', 'file%2520name', 'bad\nname'):
            with self.subTest(name=name), self.assertRaises(PackageValidationError):
                safe_file_path(name)

    def test_traversal_external_double_encoding_control_and_backslash_fail(self):
        bad = ('../x', '/x', '//evil/x', 'https://evil/x', 'a\\x', 'a/../x', 'a//x', 'a/./x',
               '%2e%2e/x', '%252e%252e/x', 'a%2fx', 'a/%00x', 'a/%5cx', 'a\nx', 'C:/x')
        for path in bad:
            with self.subTest(path=path), self.assertRaises(PackageValidationError):
                safe_relative(path, allow_suffix=True)
        self.assertEqual(safe_relative('index_lms.html?mode=1#page', allow_suffix=True), 'index_lms.html?mode=1#page')

    def test_zip_encoded_duplicate_file_overlap_and_symlink_are_rejected(self):
        for names in (('a', '%61'), ('a', 'a/x'), ('../escape',), ('a b.pdf', 'a%20b.pdf')):
            with self.subTest(names=names):
                data = BytesIO()
                with ZipFile(data, 'w') as archive:
                    for name in names:
                        archive.writestr(name, 'x')
                data.seek(0)
                with ZipFile(data) as archive, self.assertRaises(PackageValidationError):
                    validate_archive_entries(archive)


if __name__ == '__main__':
    unittest.main()
