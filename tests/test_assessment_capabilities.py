"""The central real template, not authored dummies, supplies quiz capabilities."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings
from zipfile import ZipFile, ZipInfo

from lxp_catalogue import PackageValidationError
from lxp_export import (ASSESSMENT_CAPABILITIES_FILE, APPROVED_ASSESSMENT_CONTENT_SHA256,
    assessment_template_capabilities, validate_assessment_template, make_quiz)
from tools.rebuild_assessment_template import rebuild


class AssessmentCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.template = Path(__file__).resolve().parents[1] / 'static/assessment-template.zip'
        with ZipFile(self.template) as archive:
            self.entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}

    def archive(self, entries, *, refresh=True, extra=None):
        entries = deepcopy(entries)
        if refresh:
            entries[ASSESSMENT_CAPABILITIES_FILE] = json.dumps(assessment_template_capabilities(entries)).encode()
        path = self.root / 'custom-template.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with ZipFile(path, 'w') as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)
                if extra:
                    archive.writestr(*extra)
        return path

    def changed_json(self, filename, modify):
        entries = deepcopy(self.entries)
        value = json.loads(entries[filename])
        value = modify(value)
        entries[filename] = json.dumps(value).encode()
        return self.archive(entries)

    def test_real_release_manifest_qualifies_actual_five_runtime_and_six_input_types(self):
        manifest = validate_assessment_template(self.template)
        self.assertEqual(manifest['contentSha256'], APPROVED_ASSESSMENT_CONTENT_SHA256)
        self.assertEqual(len(manifest['fileSha256']), 79)
        self.assertNotIn(ASSESSMENT_CAPABILITIES_FILE, manifest['fileSha256'])
        self.assertEqual(set(manifest['sourceQuestionTypes']), {'mcq', 'gmcq', 'matching', 'slider', 'textinput', 'infai-dragndrop'})
        self.assertEqual(set(manifest['nativeQuestionTypes']), {'mcq', 'matching', 'slider', 'textinput', 'infai-dragndrop'})
        self.assertEqual(manifest['componentAdapters'], {'gmcq': {'id': 'gmcq-to-mcq-v1', 'runtimeType': 'mcq'}})
        self.assertEqual(manifest['navigation']['strategy'], 'single-page-submit-all-host-return')
        self.assertIs(manifest['navigation']['pageNavDummyRequired'], False)

    def test_missing_or_false_manifest_and_stale_file_hashes_fail_closed(self):
        for kind in ('missing', 'version', 'nativeGmcq', 'stale'):
            with self.subTest(kind=kind):
                entries = deepcopy(self.entries)
                if kind == 'missing':
                    del entries[ASSESSMENT_CAPABILITIES_FILE]
                elif kind == 'stale':
                    entries['adapt/js/adapt.min.js'] += b'\n// changed'
                else:
                    manifest = json.loads(entries[ASSESSMENT_CAPABILITIES_FILE])
                    if kind == 'version':
                        manifest['version'] = 99
                    else:
                        manifest['nativeQuestionTypes'].append('gmcq')
                    entries[ASSESSMENT_CAPABILITIES_FILE] = json.dumps(manifest).encode()
                with self.assertRaisesRegex(PackageValidationError, 'manifest'):
                    validate_assessment_template(self.archive(entries, refresh=False))

    def test_rehashed_bundle_without_real_models_renderers_or_libraries_is_rejected(self):
        cases = [('module', 'adapt/js/adapt.min.js', b'components/adapt-contrib-matching/js/MatchingModel', b'not-a-model'),
                 ('React question renderer', 'adapt/js/adapt.min.js', b'register("mcq", function', b'register("no-mcq", function'),
                 ('compiled question renderer', 'templates.js', b'templates"]["infai-dragndrop"]', b'templates"]["not-dragndrop"]')]
        for message, name, before, after in cases:
            with self.subTest(message=message):
                entries = deepcopy(self.entries)
                self.assertIn(before, entries[name])
                entries[name] = entries[name].replace(before, after)
                with self.assertRaisesRegex(PackageValidationError, message):
                    validate_assessment_template(self.archive(entries))
        for name in ('libraries/jquery-ui.min.js', 'libraries/jquery.ui.touch-punch.js', 'libraries/rangeslider.js'):
            with self.subTest(library=name):
                entries = {key: value for key, value in self.entries.items() if key != name}
                with self.assertRaisesRegex(PackageValidationError, 'dependency file missing'):
                    validate_assessment_template(self.archive(entries))

    def test_self_consistent_hashes_and_plugin_metadata_cannot_approve_arbitrary_code(self):
        entries = deepcopy(self.entries)
        entries['adapt/js/adapt.min.js'] += b'\n// unreviewed code despite all registrations remaining'
        with self.assertRaisesRegex(PackageValidationError, 'unapproved content SHA-256'):
            validate_assessment_template(self.archive(entries))
        entries = deepcopy(self.entries)
        build = json.loads(entries['adapt/js/build.min.js'])
        next(value for value in build['plugins'] if value['name'] == 'adapt-contrib-mcq')['version'] = 'unreviewed'
        entries['adapt/js/build.min.js'] = json.dumps(build).encode()
        with self.assertRaisesRegex(PackageValidationError, 'pinned plugin metadata'):
            validate_assessment_template(self.archive(entries))

    def test_single_page_submit_all_actual_manifest_and_host_return_are_checked(self):
        cases = [('course/en/articles.json', lambda values: [{**values[0], '_submitAll': {'_isEnabled': False}}], 'SubmitAll'),
                 ('course/en/blocks.json', lambda values: [{**values[0], '_assessment': {'_quizBankID': 1}}], 'fictitious result bank'),
                 ('course/en/contentObjects.json', lambda values: values + [{**values[0], '_id': 'unexpected-page'}], 'single-page'),
                 ('course/en/course.json', lambda value: {**value, '_start': {**value['_start'], '_isMenuDisabled': False}}, 'start strategy'),
                 ('course/en/components.json', lambda values: [{**values[0], 'instruction': '<p>No host return</p>'}], 'host-return')]
        for filename, modify, message in cases:
            with self.subTest(filename=filename), self.assertRaisesRegex(PackageValidationError, message):
                validate_assessment_template(self.changed_json(filename, modify))
        entries = deepcopy(self.entries)
        entries['imsmanifest.xml'] = entries['imsmanifest.xml'].replace(b'href="index_lms.html"', b'href="missing.html"')
        with self.assertRaisesRegex(PackageValidationError, 'actual LMS entry'):
            validate_assessment_template(self.archive(entries))

    def test_unexpected_json_types_raise_meaningful_validation_errors(self):
        cases = [('adapt/js/build.min.js', lambda _: []),
                 ('adapt/js/build.min.js', lambda value: {**value, 'plugins': [None]}),
                 ('course/config.json', lambda _: []), ('course/en/course.json', lambda _: None),
                 ('course/en/course.json', lambda value: {**value, '_start': []}),
                 ('course/en/articles.json', lambda _: [None]),
                 ('course/en/articles.json', lambda value: [{**value[0], '_assessment': []}]),
                 ('course/en/components.json', lambda value: [{**value[0], 'instruction': {}}])]
        for filename, modify in cases:
            with self.subTest(filename=filename), self.assertRaises(PackageValidationError):
                validate_assessment_template(self.changed_json(filename, modify))

    def test_unsafe_duplicate_and_symlink_zip_entries_are_rejected_before_extraction(self):
        link = ZipInfo('linked-runtime')
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        for extra in (('../escape', b'no'), ('adapt/js/adapt.min.js', b'duplicate'), (link, b'adapt/js/adapt.min.js')):
            with self.subTest(entry=extra[0]), self.assertRaises(PackageValidationError):
                validate_assessment_template(self.archive(self.entries, extra=extra))
        entries = deepcopy(self.entries)
        del entries[ASSESSMENT_CAPABILITIES_FILE]
        invalid = self.archive(entries, refresh=False)
        destination = self.root / 'must-not-be-created'
        with self.assertRaises(PackageValidationError):
            make_quiz(destination, invalid, [(None, None, None, None)], 'Invalid')
        self.assertFalse(destination.exists())

    def test_rebuild_is_idempotent_and_rejected_unknown_input_is_not_overwritten(self):
        path = self.root / 'template.zip'
        path.write_bytes(self.template.read_bytes())
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        rebuild(path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        entries = deepcopy(self.entries)
        entries['adapt/js/adapt.min.js'] += b'\n// unknown bundle'
        path = self.archive(entries)
        before = path.read_bytes()
        with self.assertRaisesRegex(PackageValidationError, 'unapproved content SHA-256'):
            rebuild(path)
        self.assertEqual(path.read_bytes(), before)

    def test_rebuild_checks_unsafe_or_duplicate_original_entries_before_dict_collapse(self):
        for name in ('adapt.css', ASSESSMENT_CAPABILITIES_FILE, '../escape'):
            with self.subTest(entry=name):
                path = self.archive(self.entries, extra=(name, self.entries.get(name, b'unsafe')))
                before = path.read_bytes()
                with self.assertRaises(PackageValidationError):
                    rebuild(path)
                self.assertEqual(path.read_bytes(), before)

    def test_six_type_generation_keeps_pre_manifest_payload_bytes_and_native_ids_exact(self):
        source = {'build': self.root / 'source', 'language': 'de', 'config': {'_questionWeight': 2}}
        assets = source['build'] / 'course/de/assets'
        assets.mkdir(parents=True)
        (assets / 'option.svg').write_text('<svg>preserved source graphic</svg>')
        types = {'mcq': {'_items': [{'text': 'Yes', '_shouldBeSelected': True}, {'text': 'No'}]},
            'gmcq': {'_items': [{'text': 'Picture', '_shouldBeSelected': True, '_graphic': {'src': 'course/de/assets/option.svg', 'alt': 'Option'}}]},
            'matching': {'_items': [{'_options': [{'text': 'Accepted', '_isCorrect': True}, {'text': 'Wrong'}]}]},
            'slider': {'_scaleStart': 0, '_scaleEnd': 10, '_correctAnswer': 0},
            'textinput': {'_items': [{'answers': ['Ärger']}]}, 'infai-dragndrop': {'_items': [{'accepted': 'drop'}]}}
        candidates = [({'_id': kind + '-original', '_component': kind, 'body': 'Actual native prompt', **values}, source, index, 'stable-source')
                      for index, (kind, values) in enumerate(types.items(), 1)]
        # The previous central template consists of exactly the same 79 pinned
        # payload files, before the input-only manifest was added. Bypass only
        # that missing old marker for this explicit compatibility comparison.
        old_template = self.archive({key: value for key, value in self.entries.items() if key != ASSESSMENT_CAPABILITIES_FILE}, refresh=False)
        before, after = self.root / 'old-output', self.root / 'new-output'
        with patch('lxp_export.validate_assessment_template', return_value={}):
            make_quiz(before, old_template, candidates, 'Same assessment')
        metadata = make_quiz(after, self.template, candidates, 'Same assessment')
        manifest = lambda root: {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}
        self.assertEqual(manifest(before), manifest(after))
        # Independently captured from the committed pre-capability make_quiz
        # implementation at aa7fa059, not recomputed as an approval in this test.
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in manifest(after).items()}
        self.assertEqual(hashlib.sha256(json.dumps(hashes, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(),
            'ffd1657386234700acecf2076f047d64fba9757d1a6f163aad6ffa2bbb1635ae')
        self.assertFalse((after / ASSESSMENT_CAPABILITIES_FILE).exists())
        self.assertEqual(metadata['templateCapabilities'], validate_assessment_template(self.template))
        components = json.loads((after / 'course/en/components.json').read_text())
        self.assertEqual([q['_component'] for q in components[:-1]], ['mcq', 'mcq', 'matching', 'slider', 'textinput', 'infai-dragndrop'])
        self.assertEqual([q['_id'] for q in components[:-1]], ['diagnostic-stable-source-' + kind + '-original' for kind in types])
        self.assertIn('<img', components[1]['_items'][0]['text'])
        self.assertEqual((after / 'course/en/assets/stable-source/course/option.svg').read_bytes(), (assets / 'option.svg').read_bytes())


if __name__ == '__main__':
    unittest.main()
