"""Actual generated ZIP/JSON consistency tests for the selection sidecar."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from lxp_assessment_validation import validate_assessment_selection
from lxp_catalogue import PackageValidationError
from lxp_export import published_course
import test_multi_framework_export as fixtures


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


class AssessmentSelectionValidationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MixedPackageTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for build in self.fixture.builds:
            path = build / 'course/de/components.json'
            original = read(path)[0]
            fixtures.save(path, [{**deepcopy(original), '_id': 'question-' + str(index),
                'title': 'Source ' + str(index)} for index in range(5)])

    def site(self, *, configured=True, **changes):
        if configured:
            changes.setdefault('assessment_options', {prefix: {phase: {'mode': 'balanced',
                'questionCount': 2, 'equalSampling': True, 'showMarking': phase == 'final',
                'showModelAnswer': False} for phase in ('entry', 'final')}
                for prefix in ('DigComp', 'AILit')})
        prepared = self.fixture.prepare(**changes)
        destination = Path(tempfile.mkdtemp(prefix='delivered-', dir=self.fixture.root))
        with self.fixture.export(prepared) as archive:
            archive.extractall(destination)
        contract = read(destination / 'lxp-contract.json')
        provenance = read(destination / 'package-provenance.json')
        originals = {}
        for sco in contract['scos']:
            if sco['role'] != 'learning':
                continue
            index = next(index for index, metadata in enumerate(self.fixture.metadata)
                         if metadata['originId'] == sco['source']['originId']
                         and metadata['courseId'] == sco['source']['courseId'])
            originals[sco['id']] = published_course(self.fixture.builds[index])
        return destination, contract, provenance, originals

    def persist_summary(self, destination, provenance, summaries):
        fixtures.save(destination / 'assessment-selection.json', summaries)
        provenance['assessmentSelection'] = deepcopy(summaries)

    def validate(self, values):
        destination, contract, provenance, originals = values
        validate_assessment_selection(contract, destination, originals, provenance)

    def test_current_archive_reconstructs_exact_selection_and_legacy_archives_remain_supported(self):
        values = self.site()
        self.validate(values)
        destination, _, provenance, _ = values
        (destination / 'assessment-selection.json').unlink()
        provenance.pop('assessmentSelection')
        self.validate(values)

    def test_partial_or_disagreeing_sidecar_provenance_is_rejected(self):
        values = self.site()
        destination, _, provenance, _ = values
        original = deepcopy(provenance['assessmentSelection'])
        fixtures.save(destination / 'assessment-selection.json', [])
        with self.assertRaisesRegex(PackageValidationError, 'differs'):
            self.validate(values)
        self.persist_summary(destination, provenance, original)
        provenance.pop('assessmentSelection')
        with self.assertRaisesRegex(PackageValidationError, 'missing'):
            self.validate(values)
        provenance['assessmentSelection'] = original
        (destination / 'assessment-selection.json').unlink()
        with self.assertRaisesRegex(PackageValidationError, 'missing'):
            self.validate(values)

    def test_counts_coverage_fingerprints_options_and_reference_claim_are_verified_against_sources(self):
        values = self.site()
        destination, _, provenance, _ = values
        original = deepcopy(provenance['assessmentSelection'])
        mutations = [lambda rows: rows[0].update(selectedCount=999),
            lambda rows: rows[0].update(poolCount=999),
            lambda rows: rows[0]['questions'][0].update(fingerprint='0' * 64),
            lambda rows: rows[0]['questions'][0].update(sourceCourseId='foreign-course'),
            lambda rows: rows[0]['coverage']['domains'][0].update(selectedCount=999),
            lambda rows: rows[0]['options'].update(mode='reference'),
            lambda rows: rows[0]['options'].update(unknownOption=True),
            lambda rows: rows[0].update(selectedCount=True)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = deepcopy(original)
                mutate(changed)
                self.persist_summary(destination, provenance, changed)
                with self.assertRaises(PackageValidationError):
                    self.validate(values)
        self.persist_summary(destination, provenance, original)
        self.validate(values)

    def test_missing_duplicate_unknown_or_omitted_phase_cannot_hide_selection_metadata(self):
        values = self.site()
        destination, _, provenance, _ = values
        original = deepcopy(provenance['assessmentSelection'])
        for changed in [original[:-1], original + [original[0]],
                        [{**original[0], 'phase': 'unknown'}, *original[1:]],
                        [{**original[0], 'frameworkId': 'foreign'}, *original[1:]]]:
            with self.subTest(changed=changed):
                self.persist_summary(destination, provenance, changed)
                with self.assertRaises(PackageValidationError):
                    self.validate(values)

    def test_actual_banks_must_have_exactly_the_selected_source_pairs(self):
        values = self.site()
        _, contract, _, _ = values
        assessment = contract['assessments'][0]
        assessment['banks'][0]['questionIds'].pop()
        with self.assertRaisesRegex(PackageValidationError, 'banks'):
            self.validate(values)

    def test_generated_native_article_and_component_marking_flags_must_match(self):
        values = self.site()
        destination, contract, _, _ = values
        assessment = contract['assessments'][0]
        sco = next(sco for sco in contract['scos'] if sco['id'] == assessment['scoId'])
        base = destination / sco['launch']['root'] / 'course/en'
        components = read(base / 'components.json')
        original = deepcopy(components)
        component = next(question for question in components if question['_component'] != 'text')
        component['_canShowModelAnswer'] = True
        fixtures.save(base / 'components.json', components)
        with self.assertRaisesRegex(PackageValidationError, 'marking'):
            self.validate(values)
        fixtures.save(base / 'components.json', original)
        articles = read(base / 'articles.json')
        articles[0]['_assessment']['_questions']['_canShowModelAnswer'] = True
        fixtures.save(base / 'articles.json', articles)
        with self.assertRaisesRegex(PackageValidationError, 'marking'):
            self.validate(values)

    def test_old_full_pool_api_retains_verified_native_inheritance_and_authored_flags(self):
        for build in self.fixture.builds:
            path = build / 'course/de/components.json'
            values = read(path)
            values[0]['_canShowMarking'] = False
            values[0]['_canShowModelAnswer'] = False
            fixtures.save(path, values)
        self.validate(self.site(configured=False))

    def test_explicit_source_domain_overrides_are_bound_and_reconstructed(self):
        framework = self.fixture.catalogue['frameworks'][1]
        framework['domains'].append({'id': 'explicit-override', 'label': 'Explicit source override'})
        self.fixture.metadata[1]['questionDomains'] = {'question-' + str(index): 'explicit-override'
                                                     for index in range(5)}
        values = self.site()
        self.validate(values)
        _, contract, _, _ = values
        source = next(sco['source'] for sco in contract['scos']
                      if sco['role'] == 'learning' and sco['frameworkId'] == framework['id'])
        self.assertEqual(source['questionDomains'], self.fixture.metadata[1]['questionDomains'])
        source['questionDomains']['question-0'] = 'explicit-test-domain'
        with self.assertRaises(PackageValidationError):
            self.validate(values)

    def test_if_available_empty_pool_is_valid_and_omitted_phases_have_no_summary(self):
        fixtures.save(self.fixture.builds[1] / 'course/de/components.json', [])
        values = self.site(diagnostics={'DigComp': {'entry': 'require', 'final': 'omit'},
            'AILit': {'entry': 'ifAvailable', 'final': 'omit'}})
        self.validate(values)
        _, _, provenance, _ = values
        summaries = provenance['assessmentSelection']
        self.assertEqual(len(summaries), 2)
        empty = next(summary for summary in summaries if summary['prefix'] == 'AILit')
        self.assertEqual((empty['poolCount'], empty['selectedCount']), (0, 0))

    def test_no_diagnostic_request_produces_valid_empty_selection_metadata(self):
        self.validate(self.site(diagnostics={prefix: {'entry': 'omit', 'final': 'omit'}
                                           for prefix in ('DigComp', 'AILit')}))


if __name__ == '__main__':
    unittest.main()
