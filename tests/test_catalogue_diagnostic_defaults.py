"""Released AILit diagnostics configuration, not an automatic question alias.

These explicitly tagged native questions are technical test fixtures. Unknown
flat/menu domains, missing questions and unsupported marked plugins still fail.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from lxp_catalogue import load_catalogue, PackageValidationError
import test_multi_framework_export as fixtures


def hierarchical_questions(build):
    """Author three real native MCQs below explicit A/B/C menus in a fixture."""
    directory = Path(build) / 'course/de'
    template = json.loads((directory / 'components.json').read_text())[0]
    objects, articles, blocks, components = [], [], [], []
    for number, label in [('a', 'A - Verstehen'), ('b', 'B - Anwenden'), ('c', 'C - Bewerten')]:
        objects.extend([{'_id': 'menu-' + number, '_parentId': 'course', '_type': 'menu', 'title': label},
                        {'_id': 'page-' + number, '_parentId': 'menu-' + number, '_type': 'page', 'title': 'Zusatzfragen'}])
        articles.append({'_id': 'article-' + number, '_parentId': 'page-' + number, '_isHidden': True})
        blocks.append({'_id': 'block-' + number, '_parentId': 'article-' + number})
        components.append({**deepcopy(template), '_id': 'question-' + number, '_parentId': 'block-' + number,
                           '_classes': 'et at', '_isAvailable': True})
    for name, value in [('contentObjects', objects), ('articles', articles), ('blocks', blocks), ('components', components)]:
        fixtures.save(directory / (name + '.json'), value)


class CatalogueDiagnosticDefaultsTest(unittest.TestCase):
    def test_both_frameworks_require_real_tests_and_ailit_has_only_explicit_menu_domains(self):
        catalogue = load_catalogue()
        self.assertEqual(catalogue['revision'], 'local-digcomp22-ailit2026-r2')
        for framework in catalogue['frameworks']:
            self.assertEqual(framework['defaultDiagnosticIntents'], {'entry': 'require', 'final': 'require'})
        dig, ai = catalogue['frameworks']
        self.assertEqual(ai['prefix'], 'AILit')
        self.assertNotIn('defaultDomainId', ai, 'A flat AILit question has no inferred domain')
        self.assertEqual(ai['domains'], dig['domains'])
        self.assertEqual({domain['id'] for domain in ai['domains']}, {'verstehen', 'anwenden', 'bewerten'})


class ReleasedAilitDiagnosticExportTest(unittest.TestCase):
    write_wrapper = fixtures.MixedPackageTest.write_wrapper
    add_source = fixtures.MixedPackageTest.add_source
    prepare = fixtures.MixedPackageTest.prepare
    export = fixtures.MixedPackageTest.export
    change_question = fixtures.MixedPackageTest.change_question

    def setUp(self):
        fixtures.MixedPackageTest.setUp(self)
        # Use the actual released catalogue, not the original test-only flat
        # AILit domain. Domain assignment must work from published hierarchy.
        self.catalogue = load_catalogue()
        hierarchical_questions(self.builds[1])

    def test_product_defaults_generate_four_assessments_from_published_classes_and_domains(self):
        before = [(build / 'course/de/components.json').read_bytes() for build in self.builds]
        prepared = self.prepare(diagnostics=None)
        self.assertEqual(len(prepared.contract['assessments']), 4)
        for framework in prepared.contract['frameworks']:
            self.assertTrue(all(value == {'intent': 'require', 'status': 'provided', 'reason': None}
                                for value in framework['diagnostics'].values()))
        for assessment in prepared.contract['assessments']:
            if assessment['frameworkId'] == 'fw-ailit-2026':
                self.assertEqual({bank['domainId'] for bank in assessment['banks']}, {'verstehen', 'anwenden', 'bewerten'})
                self.assertEqual(sum(len(bank['questionIds']) for bank in assessment['banks']), 3)
        with self.export(prepared) as archive:
            self.assertIn('lxp-contract.json', archive.namelist())
        self.assertEqual(before, [(build / 'course/de/components.json').read_bytes() for build in self.builds])

    def test_missing_real_questions_is_not_replaced_by_default_or_other_framework_questions(self):
        fixtures.save(self.builds[1] / 'course/de/components.json', [])
        with self.assertRaisesRegex(PackageValidationError, 'AILit entry: required diagnostic has no assigned questions'):
            self.prepare(diagnostics=None)

    def test_flat_or_unknown_menu_domain_still_fails_closed(self):
        path = self.builds[1] / 'course/de/contentObjects.json'
        original = json.loads(path.read_text())
        for parent, title in [('course', 'A - Verstehen'), ('menu-a', 'Unknown authored menu')]:
            with self.subTest(parent=parent):
                values = deepcopy(original)
                next(item for item in values if item['_id'] == 'page-a')['_parentId'] = parent
                next(item for item in values if item['_id'] == 'menu-a')['title'] = title
                fixtures.save(path, values)
                with self.assertRaisesRegex(PackageValidationError, 'No explicitly configured diagnostic domain'):
                    self.prepare(diagnostics=None)

    def test_explicit_omit_remains_honest_and_marked_unsupported_alias_is_not_enabled(self):
        self.change_question(1, _component='markledgeMultipleChoiceAssessment')
        with self.assertRaisesRegex(PackageValidationError, 'Unsupported diagnostic component'):
            self.prepare(diagnostics=None)
        prepared = self.prepare(diagnostics={'AILit': {'entry': 'omit', 'final': 'omit'}})
        ai = next(value for value in prepared.contract['frameworks'] if value['prefix'] == 'AILit')
        self.assertTrue(all(value == {'intent': 'omit', 'status': 'notProvided', 'reason': 'not-requested'}
                            for value in ai['diagnostics'].values()))
        self.assertEqual(len(prepared.contract['assessments']), 2)
