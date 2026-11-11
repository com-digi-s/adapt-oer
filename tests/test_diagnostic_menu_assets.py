"""Only proved new menu assets may be absent from a generated diagnostic SCO."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from lxp_catalogue import PackageValidationError
from lxp_diagnostics_articles import learning_source_view
from lxp_export import make_quiz
from lxp_multi_export import learning_compatibility_fingerprint
from lxp_presentation_compat import (build_proof, registered_proof_payloads,
    diagnostic_menu_asset_exclusions)
from test_presentation_compatibility import source, IDENTITY


class DiagnosticMenuAssetsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.previous = source()
        self.previous['build'] = self.root
        self.previous['course']['_boxMenu'] = {'_graphic': {'_src': 'course/de/assets/old.png', 'alt': 'Old'}}
        self.previous['data']['contentObjects'][0]['_graphic'] = {'src': 'course/de/assets/old.png', 'alt': 'Old'}
        self.current = deepcopy(self.previous)
        self.current['course']['_boxMenu']['_graphic']['_src'] = 'course/de/assets/new.png'
        self.current['data']['contentObjects'][0]['_graphic']['src'] = 'course/de/assets/new.png'
        self.write('course/de/assets/old.png', b'old PNG')
        self.write('course/de/assets/new.png', b'new PNG')

    def write(self, relative, data):
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)

    def payloads(self, previous=None):
        current = learning_source_view(self.current)[0]
        old = learning_source_view(previous or self.previous)[0]
        envelope, _ = build_proof(old, current, IDENTITY, learning_compatibility_fingerprint)
        return registered_proof_payloads({IDENTITY['packageId']: {envelope['currentFingerprint']: [envelope]}},
            current, IDENTITY, learning_compatibility_fingerprint)

    def exclusions(self, payloads=None):
        return diagnostic_menu_asset_exclusions(self.current, learning_source_view(self.current)[0],
            self.payloads() if payloads is None else payloads, IDENTITY, learning_compatibility_fingerprint)

    def test_new_menu_file_only_old_file_stays_and_source_is_unchanged(self):
        before = deepcopy(self.current)
        self.assertEqual(self.exclusions(), {'course/new.png'})
        self.assertEqual(self.current, before)
        self.assertEqual(self.exclusions(), self.exclusions())

    def test_missing_or_forged_proof_never_excludes(self):
        self.assertEqual(self.exclusions([]), set())
        valid = self.payloads()
        for field in ('path', 'sha256'):
            tampered = deepcopy(valid)
            tampered[0][2][field] = 'bad'
            with self.subTest(field=field), self.assertRaises(PackageValidationError):
                self.exclusions(tampered)
        tampered = [(valid[0][0], valid[0][1] + b' ', valid[0][2])]
        with self.assertRaises(PackageValidationError):
            self.exclusions(tampered)

    def test_every_previous_snapshot_must_not_reference_candidate(self):
        middle = deepcopy(self.previous)
        middle['course']['_boxMenu']['_graphic']['_src'] = 'course/de/assets/new.png'
        self.assertEqual(self.exclusions(self.payloads() + self.payloads(middle)), set())

    def test_question_feedback_body_config_and_unselected_components_retain_asset(self):
        for path in [('data', 'components', 1, 'body'), ('data', 'components', 1, '_feedback'),
                     ('data', 'components', 0, 'body'), ('config', 'customBackground'), ('course', 'body')]:
            with self.subTest(path=path):
                for data in (self.previous, self.current):
                    target = data
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = '<img src="course/de/assets/new.png">'
                self.assertEqual(self.exclusions(), set())
                for data in (self.previous, self.current):
                    target = data
                    for key in path[:-1]:
                        target = target[key]
                    del target[path[-1]]

    def test_full_diagnostic_only_subtree_is_checked_not_just_learning_view(self):
        self.current['data']['articles'].append({'_id': 'hidden-article', '_parentId': 'page', '_type': 'article',
            '_classes': 'lxp-diagnostics-only', '_isAvailable': False, '_isOptional': True})
        self.current['data']['blocks'].append({'_id': 'hidden-block', '_parentId': 'hidden-article', '_type': 'block'})
        self.current['data']['components'].append({'_id': 'hidden-question', '_parentId': 'hidden-block', '_type': 'component',
            '_component': 'mcq', '_classes': 'et at', '_feedback': {'correct': '<img src="course/de/assets/new.png">'}})
        self.assertEqual(len(learning_source_view(self.current)[0]['data']['components']), 2)
        self.assertEqual(self.exclusions(), set())

    def test_actual_content_change_fails_replay(self):
        payloads = self.payloads()
        self.current['data']['components'][1]['body'] = 'A genuinely different question'
        with self.assertRaises(PackageValidationError):
            self.exclusions(payloads)

    def test_encoded_or_ambiguous_reference_retains_asset(self):
        for value in ('NEW.PNG', 'https://example.org/new.png', 'course/de/assets/%6Eew.png'):
            self.current['config']['extra'] = self.previous['config']['extra'] = value
            with self.subTest(value=value):
                self.assertEqual(self.exclusions(), set())
        del self.current['config']['extra'], self.previous['config']['extra']
        self.write('assets/new.png', b'other same-name asset')
        self.assertEqual(self.exclusions(), set())

    def test_runtime_reference_retains_asset_but_inventory_does_not(self):
        self.write('imsmanifest.xml', b'<file href="course/de/assets/new.png"/>')
        self.assertEqual(self.exclusions(), {'course/new.png'})
        self.write('adapt/css/theme.css', b'.cover { background: url(../../course/de/assets/new.png) }')
        self.assertEqual(self.exclusions(), set())

    def test_root_assets_supported_but_unknown_alias_and_svg_not_filtered(self):
        for target, expected in [('assets/root.webp', {'root/root.webp'}), ('course/assets/alias.png', set()),
                                 ('course/de/assets/vector.svg', set())]:
            self.current['course']['_boxMenu']['_graphic']['_src'] = target
            self.current['data']['contentObjects'][0]['_graphic']['src'] = target
            self.write(target, b'fixture')
            with self.subTest(target=target):
                self.assertEqual(self.exclusions(), expected)

    def test_copy_exclusions_are_namespace_and_exact_path_scoped(self):
        template = Path(__file__).resolve().parents[1] / 'static/assessment-template.zip'
        question = deepcopy(self.current['data']['components'][1])
        candidates = [(question, self.current, 1, 'course-a'), (question, self.current, 2, 'course-b')]
        destination = self.root / 'quiz-result'
        make_quiz(destination, template, candidates, 'Diagnostic', excluded_assets={'course-a': {'course/new.png'}})
        assets = destination / 'course/en/assets'
        self.assertFalse((assets / 'course-a/course/new.png').exists())
        self.assertTrue((assets / 'course-a/course/old.png').is_file())
        self.assertTrue((assets / 'course-b/course/new.png').is_file())
        for invalid in ({'unknown': {'course/new.png'}}, {'course-a': {'../new.png'}}, {'course-a': {'course'}}):
            with self.subTest(invalid=invalid), self.assertRaises(PackageValidationError):
                make_quiz(self.root / 'bad', template, candidates, 'Diagnostic', excluded_assets=invalid)


if __name__ == '__main__':
    unittest.main()
