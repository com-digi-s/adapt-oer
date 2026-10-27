"""Native ZIP/manual-summary proofs reconstructed from actual tagged sources."""
from copy import deepcopy
import json
import unittest

from lxp_assessment_selection import question_fingerprint
from lxp_catalogue import PackageValidationError
import test_assessment_selection_validation as fixtures


class ManualAssessmentValidationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AssessmentSelectionValidationTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.native = self.fixture.fixture

    def reference(self, question_number):
        question = json.loads((self.native.builds[1] / 'course/de/components.json').read_text())[question_number]
        metadata = self.native.metadata[1]
        return {'identity': [metadata['originId'], metadata['courseId'], question['_id']],
                'fingerprint': question_fingerprint(question)}

    def options(self):
        result = {prefix: {phase: {'mode': 'balanced', 'questionCount': 2, 'equalSampling': True,
            'showMarking': False, 'showModelAnswer': False} for phase in ('entry', 'final')}
                  for prefix in ('DigComp', 'AILit')}
        result['AILit']['entry'].update(includeQuestions=[self.reference(4)], excludeQuestions=[self.reference(0)])
        result['AILit']['final'].update(excludeQuestions=[self.reference(4)])
        return result

    def site(self, **changes):
        changes.setdefault('assessment_options', self.options())
        return self.fixture.site(**changes)

    @staticmethod
    def manual_summary(summaries):
        return next(row for row in summaries if row['prefix'] == 'AILit' and row['phase'] == 'entry')

    def test_generated_zip_honours_per_phase_pins_exclusions_and_safe_complete_inventory(self):
        values = self.site()
        self.fixture.validate(values)
        _, contract, provenance, _ = values
        questions = {row['id']: row for row in contract['questions']}
        for phase, included, forbidden in [('entry', 'question-4', 'question-0'), ('final', None, 'question-4')]:
            assessment = next(row for row in contract['assessments'] if row['frameworkId'] == 'fw-ailit-2026' and row['phase'] == phase)
            components = [questions[identifier]['sourceComponentId'] for bank in assessment['banks'] for identifier in bank['questionIds']]
            self.assertEqual(len(components), 2)
            self.assertNotIn(forbidden, components)
            if included:
                self.assertIn(included, components)
        summary = self.manual_summary(provenance['assessmentSelection'])
        self.assertEqual((summary['poolCount'], summary['eligibleCount'], summary['includedCount'], summary['excludedCount']), (5, 4, 1, 1))
        self.assertEqual(len(summary['availableQuestions']), 5)
        self.assertTrue(all(set(row) == {'identity', 'sourceCourseId', 'sourceComponentId', 'title', 'domainId',
                                        'fingerprint', 'moduleId', 'manualChoice', 'selected'} for row in summary['availableQuestions']))

    def test_durable_manual_summaries_cannot_strip_available_inventory(self):
        values = self.site()
        destination, _, provenance, _ = values
        summaries = deepcopy(provenance['assessmentSelection'])
        self.manual_summary(summaries).pop('availableQuestions')
        self.fixture.persist_summary(destination, provenance, summaries)
        with self.assertRaisesRegex(PackageValidationError, 'complete available question inventory'):
            self.fixture.validate(values)

    def test_inventory_recomputed_from_native_sources_rejects_matching_sidecar_provenance_tampering(self):
        values = self.site()
        destination, _, provenance, _ = values
        original = deepcopy(provenance['assessmentSelection'])
        mutations = [
            lambda row: row['availableQuestions'][0].update(selected=not row['availableQuestions'][0]['selected']),
            lambda row: row['availableQuestions'][0].update(manualChoice='invented'),
            lambda row: row['availableQuestions'][0].update(fingerprint='0' * 64),
            lambda row: row['availableQuestions'][0].update(sourceCourseId='another-course'),
            lambda row: row['availableQuestions'][0].update(payload={'_correctAnswer': 'injected'}),
            lambda row: row['availableQuestions'].pop(),
            lambda row: row['availableQuestions'].append(deepcopy(row['availableQuestions'][0])),
            lambda row: row.update(eligibleCount=999),
            lambda row: row.update(includedCount=True),
        ]
        for mutate in mutations:
            summaries = deepcopy(original)
            mutate(self.manual_summary(summaries))
            self.fixture.persist_summary(destination, provenance, summaries)
            with self.subTest(mutation=mutate), self.assertRaises(PackageValidationError):
                self.fixture.validate(values)
        self.fixture.persist_summary(destination, provenance, original)
        self.fixture.validate(values)

    def test_manual_option_fingerprint_unknown_identity_and_conflict_are_rechecked(self):
        values = self.site()
        destination, _, provenance, _ = values
        original = deepcopy(provenance['assessmentSelection'])
        mutations = [
            lambda row: row['options']['includeQuestions'][0].update(fingerprint='0' * 64),
            lambda row: row['options']['includeQuestions'][0].update(identity=['foreign', 'course', 'component']),
            lambda row: row['options']['excludeQuestions'].append(deepcopy(row['options']['includeQuestions'][0])),
            lambda row: row['options'].update(mode='reference'),
        ]
        for mutate in mutations:
            summaries = deepcopy(original)
            mutate(self.manual_summary(summaries))
            self.fixture.persist_summary(destination, provenance, summaries)
            with self.subTest(mutation=mutate), self.assertRaises(PackageValidationError):
                self.fixture.validate(values)

    def test_deleted_retagged_or_changed_original_question_cannot_keep_old_manual_proof(self):
        values = self.site()
        _, contract, _, originals = values
        source_id = next(row['id'] for row in contract['scos'] if row['role'] == 'learning' and row['frameworkId'] == 'fw-ailit-2026')
        original = deepcopy(originals[source_id])
        for action in ('delete', 'retag', 'change'):
            originals[source_id] = deepcopy(original)
            components = originals[source_id]['data']['components']
            question = next(row for row in components if row['_id'] == 'question-4')
            if action == 'delete':
                components.remove(question)
            elif action == 'retag':
                question['_classes'] = 'at'
            else:
                question['body'] = 'Changed actual source content'
            with self.subTest(action=action), self.assertRaises(PackageValidationError):
                self.fixture.validate(values)
        originals[source_id] = original
        self.fixture.validate(values)

    def test_old_automatic_summaries_without_inventory_remain_valid(self):
        values = self.fixture.site()
        destination, _, provenance, _ = values
        summaries = deepcopy(provenance['assessmentSelection'])
        for summary in summaries:
            summary.pop('availableQuestions', None)
            self.assertNotIn('includeQuestions', summary['options'])
            self.assertNotIn('excludeQuestions', summary['options'])
        self.fixture.persist_summary(destination, provenance, summaries)
        self.fixture.validate(values)

    def test_exclude_all_is_honest_when_optional_and_blocks_required_test(self):
        options = {'AILit': {'entry': {'mode': 'all', 'excludeQuestions': [self.reference(index) for index in range(5)]}}}
        diagnostics = {'DigComp': {'entry': 'omit', 'final': 'omit'}, 'AILit': {'entry': 'ifAvailable', 'final': 'omit'}}
        values = self.site(assessment_options=options, diagnostics=diagnostics)
        self.fixture.validate(values)
        _, contract, provenance, _ = values
        self.assertEqual(contract['assessments'], [])
        summary = self.manual_summary(provenance['assessmentSelection'])
        self.assertEqual((summary['poolCount'], summary['eligibleCount'], summary['selectedCount']), (5, 0, 0))
        self.assertEqual(len(summary['availableQuestions']), 5)
        diagnostics['AILit']['entry'] = 'require'
        with self.assertRaisesRegex(PackageValidationError, 'required diagnostic has no assigned questions'):
            self.native.prepare(assessment_options=options, diagnostics=diagnostics)


if __name__ == '__main__':
    unittest.main()
