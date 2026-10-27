"""Pure selection policy tests, not a claim of validated competency sampling."""
from collections import Counter
from copy import deepcopy
import hashlib
import unittest

from lxp_assessment_selection import (DEFAULT_OPTIONS, QUESTION_FINGERPRINT_FIELDS,
    normalize_assessment_options, question_fingerprint, select_questions)
from lxp_catalogue import PackageValidationError


def framework():
    return {'id': 'fw-dig', 'prefix': 'DigComp', 'label': 'DigComp', 'version': '2.2',
            'modules': [{'id': 'opaque-module-b', 'number': '2', 'label': 'Second', 'courseNumbers': ['1', '2', '3']},
                        {'id': 'opaque-module-a', 'number': '1', 'label': 'First', 'courseNumbers': ['1', '2', '3']}],
            'domains': [{'id': 'v', 'label': 'Verstehen'}, {'id': 'a', 'label': 'Anwenden'},
                        {'id': 'b', 'label': 'Bewerten'}]}


def pool(courses=2, questions=3):
    result = []
    for module in framework()['modules']:
        for course in range(1, courses + 1):
            course_id = 'sco-' + module['id'] + '-' + str(course)
            for domain in framework()['domains']:
                for index in range(questions):
                    component = domain['id'] + '-' + str(index)
                    result.append({'identity': ('tenant', course_id, component),
                        'moduleId': module['id'], 'moduleNumber': module['number'],
                        'courseId': course_id, 'courseNumber': str(course), 'courseLabel': 'Course ' + str(course),
                        'domainId': domain['id'], 'title': 'Question ' + component,
                        'fingerprint': hashlib.sha256(component.encode()).hexdigest(),
                        'payload': {'actual': 'native question ' + component}})
    return result


class AssessmentOptionsTest(unittest.TestCase):
    def test_omitted_api_options_preserve_full_pool_and_aliases_are_canonical(self):
        catalogue = {'frameworks': [framework()]}
        self.assertEqual(normalize_assessment_options(None, catalogue), {})
        actual = normalize_assessment_options({'DigComp': {'entry': {'questionCount': 24}}}, catalogue)
        self.assertEqual(actual, {'fw-dig': {'entry': {**DEFAULT_OPTIONS, 'questionCount': 24}}})
        actual['fw-dig']['entry']['questionCount'] = 1
        self.assertIsNone(DEFAULT_OPTIONS['questionCount'])
        same = {'DigComp': {'entry': {'questionCount': 24}}, 'fw-dig': {'entry': {'questionCount': 24}}}
        self.assertEqual(normalize_assessment_options(same, catalogue)['fw-dig']['entry']['questionCount'], 24)
        same['fw-dig']['entry']['questionCount'] = 12
        with self.assertRaisesRegex(PackageValidationError, 'Conflicting'):
            normalize_assessment_options(same, catalogue)

    def test_strict_framework_phase_key_count_and_boolean_allowlists(self):
        catalogue = {'frameworks': [framework()]}
        for value in [[], {'Other': {}}, {'DigComp': {'ET': {}}}, {'DigComp': []},
                      {'DigComp': {'entry': {'typo': True}}}, {'DigComp': {'entry': []}},
                      {'DigComp': {'entry': {'mode': 'random'}}},
                      {'DigComp': {'entry': {'mode': []}}}]:
            with self.subTest(value=value), self.assertRaises(PackageValidationError):
                normalize_assessment_options(value, catalogue)
        for count in [0, -1, True, False, 1.5, '24', [], {}]:
            with self.subTest(count=count), self.assertRaises(PackageValidationError):
                normalize_assessment_options({'DigComp': {'entry': {'questionCount': count}}}, catalogue)
        for key in ['equalSampling', 'showMarking', 'showModelAnswer']:
            for value in [1, 'false', None]:
                with self.subTest(key=key, value=value), self.assertRaises(PackageValidationError):
                    normalize_assessment_options({'DigComp': {'entry': {key: value}}}, catalogue)


class AssessmentSelectionTest(unittest.TestCase):
    def test_balanced_selection_is_deterministic_order_independent_and_non_mutating(self):
        candidates = pool()
        original = deepcopy(candidates)
        selected, summary = select_questions(candidates, {'questionCount': 12}, framework(), 'entry')
        reversed_selected, reversed_summary = select_questions(list(reversed(candidates)), {'questionCount': 12}, framework(), 'entry')
        self.assertEqual(selected, reversed_selected)
        self.assertEqual(summary, reversed_summary)
        self.assertEqual(candidates, original)
        self.assertTrue(all(any(candidate is item for item in candidates) for candidate in selected))
        self.assertEqual(summary['algorithm'], 'adapt-oer-balanced-selection@1')
        self.assertEqual(Counter(candidate['moduleId'] for candidate in selected), {'opaque-module-a': 6, 'opaque-module-b': 6})
        self.assertEqual(set(Counter(candidate['courseId'] for candidate in selected).values()), {3})
        self.assertEqual(Counter(candidate['domainId'] for candidate in selected), {'v': 4, 'a': 4, 'b': 4})
        self.assertEqual(len({tuple(candidate['identity']) for candidate in selected}), 12)
        self.assertEqual(summary['requestedCount'], 12)
        self.assertEqual(summary['poolCount'], 36)
        self.assertEqual(summary['selectedCount'], 12)
        self.assertEqual(summary['coverage']['domains'][0]['label'], 'Verstehen')

    def test_small_selection_covers_domains_globally_instead_of_resetting_every_course_to_first_domain(self):
        selected, summary = select_questions(pool(courses=3), {'questionCount': 6}, framework(), 'entry')
        self.assertEqual(len({candidate['courseId'] for candidate in selected}), 6)
        self.assertEqual(Counter(candidate['domainId'] for candidate in selected), {'v': 2, 'a': 2, 'b': 2})
        self.assertEqual(summary['warnings'], [])

    def test_capacity_redistributes_without_duplicates_or_excluding_available_content(self):
        candidates = [candidate for candidate in pool() if candidate['moduleNumber'] == '2']
        candidates.append(next(candidate for candidate in pool() if candidate['moduleNumber'] == '1'))
        selected, summary = select_questions(candidates, {'questionCount': 10}, framework(), 'entry')
        self.assertEqual(len(selected), 10)
        self.assertEqual(Counter(candidate['moduleNumber'] for candidate in selected), {'1': 1, '2': 9})
        self.assertEqual(len({tuple(candidate['identity']) for candidate in selected}), 10)
        self.assertEqual(summary['coverage']['modules'][0]['poolCount'], 1)

    def test_all_and_missing_count_return_full_pool_without_forcing_new_selection(self):
        candidates = pool()
        for options in [None, {}, {'questionCount': None}, {'mode': 'all', 'questionCount': 1}]:
            selected, summary = select_questions(candidates, options, framework(), 'final')
            self.assertEqual(len(selected), len(candidates))
            self.assertEqual(summary['requestedCount'], None)
            self.assertEqual(summary['algorithm'], 'adapt-oer-all-selection@1')
            self.assertEqual(summary['warnings'], [])

    def test_flat_selection_remains_reproducible_but_does_not_claim_balance(self):
        candidates = pool()
        selected, summary = select_questions(candidates, {'questionCount': 9, 'equalSampling': False}, framework(), 'entry')
        self.assertEqual(len(selected), 9)
        self.assertEqual(summary['algorithm'], 'adapt-oer-fixed-selection@1')
        self.assertEqual(selected, select_questions(list(reversed(candidates)), {'questionCount': 9, 'equalSampling': False}, framework(), 'entry')[0])

    def test_small_and_empty_pools_report_actual_counts_without_inventing_zero_scores(self):
        candidates = pool()[:2]
        selected, summary = select_questions(candidates, {'questionCount': 24}, framework(), 'entry')
        self.assertEqual(len(selected), 2)
        self.assertEqual(summary['requestedCount'], 24)
        self.assertIn('nur 2 geeignete Fragen', summary['warnings'][0])
        self.assertEqual(summary['coverage']['modules'][0]['status'], 'not-assessed')
        self.assertEqual(summary['coverage']['domains'][1]['selectedCount'], 0)
        self.assertNotIn('score', summary['coverage']['domains'][1])
        selected, summary = select_questions([], {'questionCount': 24}, framework(), 'entry')
        self.assertEqual(selected, [])
        self.assertEqual(summary['selectedCount'], 0)
        self.assertTrue(all(row['status'] == 'not-assessed' for row in summary['coverage']['domains']))

    def test_reference_mode_requires_the_complete_exact_trusted_identity_list(self):
        candidates = pool()
        identities = [candidates[5]['identity'], candidates[1]['identity']]
        selected, summary = select_questions(candidates, {'mode': 'reference', 'questionCount': 2}, framework(), 'entry', reference_identities=identities)
        self.assertIs(selected[0], candidates[5])
        self.assertIs(selected[1], candidates[1])
        self.assertEqual(summary['algorithm'], 'adapt-oer-public-reference@1')
        self.assertEqual(summary['selectedCount'], 2)
        self.assertEqual(summary['questions'][0]['sourceComponentId'], candidates[5]['identity'][-1])
        for references in [None, [], [identities[0], identities[0]], [('other', 'course', 'missing')]]:
            with self.subTest(references=references), self.assertRaises(PackageValidationError):
                select_questions(candidates, {'mode': 'reference'}, framework(), 'entry', reference_identities=references)
        with self.assertRaisesRegex(PackageValidationError, 'fixed question count'):
            select_questions(candidates, {'mode': 'reference', 'questionCount': 24}, framework(), 'entry', reference_identities=identities)

    def test_invalid_candidates_and_framework_leaks_fail_before_selecting(self):
        candidates = pool()
        for change in [lambda value: value.update(moduleId='foreign'), lambda value: value.update(moduleNumber='99'),
                       lambda value: value.update(courseNumber='99'), lambda value: value.update(domainId='foreign'),
                       lambda value: value.update(frameworkId='foreign'), lambda value: value.update(fingerprint='not-a-hash'),
                       lambda value: value.update(moduleId=[]), lambda value: value.update(domainId=[]),
                       lambda value: value.update(identity=('tenant', '', 'component'))]:
            bad = deepcopy(candidates)
            change(bad[0])
            with self.subTest(change=change), self.assertRaises(PackageValidationError):
                select_questions(bad, {'questionCount': 1}, framework(), 'entry')
        with self.assertRaisesRegex(PackageValidationError, 'Duplicate'):
            select_questions(candidates + [candidates[0]], {}, framework(), 'entry')
        with self.assertRaisesRegex(PackageValidationError, 'phase'):
            select_questions(candidates, {}, framework(), 'unknown')
        for options in [[], '', 0, False]:
            with self.subTest(options=options), self.assertRaises(PackageValidationError):
                select_questions(candidates, options, framework(), 'entry')

    def test_fingerprint_binds_exact_allowed_content_keys_and_distinguishes_absent_from_null(self):
        question = {'_component': 'mcq', 'body': '<p>Original</p>', '_items': [{'text': 'A', '_shouldBeSelected': True}], '_selectable': 1, '_questionWeight': 1}
        actual = question_fingerprint(question)
        self.assertEqual(actual, question_fingerprint({**question, '_id': 'rewritten-public-id', '_classes': 'et at'}))
        self.assertNotEqual(actual, question_fingerprint({**question, 'title': None}))
        for key in QUESTION_FINGERPRINT_FIELDS:
            changed = deepcopy(question)
            changed[key] = 'changed'
            self.assertNotEqual(actual, question_fingerprint(changed))
        with self.assertRaisesRegex(PackageValidationError, 'non-finite'):
            question_fingerprint({'_questionWeight': float('nan')})


if __name__ == '__main__':
    unittest.main()
