"""Manual references are exact phase-pool/content proofs, never source edits."""
from copy import deepcopy
import unittest

from lxp_assessment_overrides import normalize_overrides, resolve_overrides
from lxp_catalogue import PackageValidationError
import test_assessment_selection as fixtures


def reference(candidate):
    return {'identity': list(candidate['identity']), 'fingerprint': candidate['fingerprint']}


class AssessmentOverrideNormalizationTest(unittest.TestCase):
    def setUp(self):
        self.pool = fixtures.pool(courses=2, questions=2)

    def test_absent_and_empty_arrays_do_not_change_existing_five_option_shape(self):
        options = {'mode': 'balanced', 'questionCount': 12, 'equalSampling': True,
                   'showMarking': True, 'showModelAnswer': False}
        self.assertEqual(normalize_overrides(options), {})
        for extra in ({'includeQuestions': []}, {'excludeQuestions': []},
                      {'includeQuestions': [], 'excludeQuestions': []}):
            self.assertEqual(normalize_overrides({**options, **extra}), {})
        self.assertEqual(len(options), 5)

    def test_records_are_canonical_deep_copies_and_only_nonempty_keys_are_returned(self):
        options = {'includeQuestions': [reference(self.pool[-1]), reference(self.pool[0])],
                   'excludeQuestions': []}
        original = deepcopy(options)
        normalized = normalize_overrides(options)
        self.assertEqual(set(normalized), {'includeQuestions'})
        self.assertEqual(normalized['includeQuestions'], sorted(options['includeQuestions'], key=lambda row: tuple(row['identity'])))
        self.assertEqual(options, original)
        normalized['includeQuestions'][0]['identity'][0] = 'changed returned data'
        self.assertEqual(options, original)

    def test_unknown_record_keys_missing_keys_and_wrong_array_types_are_rejected(self):
        valid = reference(self.pool[0])
        for record in [None, [], 'question', {'identity': valid['identity']}, {'fingerprint': valid['fingerprint']},
                       {**valid, 'answer': 'not permitted'}, {**valid, 'selected': True}]:
            with self.subTest(record=record), self.assertRaises(PackageValidationError):
                normalize_overrides({'includeQuestions': [record]})
        for value in [None, {}, (), 'question', 1, True]:
            for key in ('includeQuestions', 'excludeQuestions'):
                with self.subTest(key=key, value=value), self.assertRaises(PackageValidationError):
                    normalize_overrides({key: value})
        for options in [None, [], '', 1, False]:
            with self.subTest(options=options), self.assertRaises(PackageValidationError):
                normalize_overrides(options)

    def test_identities_require_exactly_three_nonempty_utf8_strings(self):
        valid = reference(self.pool[0])
        for identity in [None, 'tenant/course/component', ('tenant', 'course', 'component'), [], ['one', 'two'],
                         ['one', 'two', 'three', 'four'], ['tenant', '', 'component'],
                         ['tenant', '  ', 'component'], ['tenant', 1, 'component'],
                         ['tenant', 'course', 'bad\ncomponent'], ['tenant', 'course', '\ud800']]:
            with self.subTest(identity=identity), self.assertRaises(PackageValidationError):
                normalize_overrides({'includeQuestions': [{**valid, 'identity': identity}]})

    def test_fingerprints_must_be_finite_typed_lowercase_sha256(self):
        valid = reference(self.pool[0])
        for fingerprint in [None, 0, True, [], {}, float('nan'), float('inf'), '0' * 63, 'G' * 64, 'A' * 64]:
            with self.subTest(fingerprint_type=type(fingerprint)), self.assertRaises(PackageValidationError):
                normalize_overrides({'excludeQuestions': [{**valid, 'fingerprint': fingerprint}]})

    def test_duplicate_identities_and_include_exclude_conflicts_are_rejected(self):
        valid = reference(self.pool[0])
        for other in [deepcopy(valid), {**valid, 'fingerprint': '0' * 64}]:
            for key in ('includeQuestions', 'excludeQuestions'):
                with self.subTest(key=key, other=other), self.assertRaises(PackageValidationError):
                    normalize_overrides({key: [valid, other]})
            with self.assertRaisesRegex(PackageValidationError, 'gleichzeitig'):
                normalize_overrides({'includeQuestions': [valid], 'excludeQuestions': [other]})


class AssessmentOverrideResolutionTest(unittest.TestCase):
    def setUp(self):
        self.pool = fixtures.pool(courses=2, questions=2)

    def test_resolution_keeps_original_objects_and_eligible_contains_pins_but_not_exclusions(self):
        included_row, excluded_row = self.pool[0], self.pool[-1]
        options = {'mode': 'balanced', 'questionCount': 2,
                   'includeQuestions': [reference(included_row)], 'excludeQuestions': [reference(excluded_row)]}
        original_options, original_pool = deepcopy(options), deepcopy(self.pool)
        included, excluded, eligible = resolve_overrides(self.pool, options)
        self.assertEqual(included, [included_row])
        self.assertIs(included[0], included_row)
        self.assertEqual(excluded, {tuple(excluded_row['identity'])})
        self.assertTrue(any(candidate is included_row for candidate in eligible))
        self.assertFalse(any(candidate is excluded_row for candidate in eligible))
        self.assertEqual(eligible, self.pool[:-1])
        self.assertEqual(options, original_options)
        self.assertEqual(self.pool, original_pool)

    def test_absent_overrides_preserve_the_entire_pool_order_and_object_identity(self):
        included, excluded, eligible = resolve_overrides(self.pool, {})
        self.assertEqual(included, [])
        self.assertEqual(excluded, set())
        self.assertEqual(eligible, self.pool)
        self.assertTrue(all(left is right for left, right in zip(eligible, self.pool)))

    def test_reordered_submissions_or_pool_do_not_change_canonical_pin_order(self):
        records = [reference(self.pool[-1]), reference(self.pool[0])]
        first = resolve_overrides(self.pool, {'includeQuestions': records})[0]
        second = resolve_overrides(list(reversed(self.pool)), {'includeQuestions': list(reversed(records))})[0]
        self.assertEqual(first, second)

    def test_both_included_and_excluded_references_must_exist_in_current_phase_pool(self):
        removed = self.pool[0]
        for key in ('includeQuestions', 'excludeQuestions'):
            with self.subTest(key=key), self.assertRaisesRegex(PackageValidationError, 'markierten Fragenpool'):
                resolve_overrides(self.pool[1:], {key: [reference(removed)]})
        for identity in [['foreign-origin', *removed['identity'][1:]],
                         [removed['identity'][0], 'other-course', removed['identity'][2]],
                         [*removed['identity'][:2], 'missing-component']]:
            with self.subTest(identity=identity), self.assertRaises(PackageValidationError):
                resolve_overrides(self.pool, {'includeQuestions': [{**reference(removed), 'identity': identity}]})

    def test_stale_content_fingerprint_is_rejected_for_inclusion_and_exclusion(self):
        stale = {**reference(self.pool[0]), 'fingerprint': '0' * 64}
        for key in ('includeQuestions', 'excludeQuestions'):
            with self.subTest(key=key), self.assertRaisesRegex(PackageValidationError, 'inhaltlich geändert'):
                resolve_overrides(self.pool, {key: [stale]})

    def test_same_component_id_in_another_course_is_a_distinct_source_question(self):
        first = self.pool[0]
        other = next(row for row in self.pool if row['identity'][-1] == first['identity'][-1]
                     and row['identity'][:2] != first['identity'][:2])
        included, excluded, eligible = resolve_overrides(self.pool, {
            'includeQuestions': [reference(first)], 'excludeQuestions': [reference(other)]})
        self.assertEqual(included, [first])
        self.assertEqual(excluded, {tuple(other['identity'])})
        self.assertIn(first, eligible)
        self.assertNotIn(other, eligible)

    def test_pins_cannot_silently_expand_a_bounded_balanced_test(self):
        options = {'mode': 'balanced', 'questionCount': 1,
                   'includeQuestions': [reference(self.pool[0]), reference(self.pool[1])]}
        with self.assertRaisesRegex(PackageValidationError, 'mehr Fragen fest eingeschlossen'):
            resolve_overrides(self.pool, options)
        options['questionCount'] = 2
        self.assertEqual(len(resolve_overrides(self.pool, options)[0]), 2)
        options['questionCount'] = None
        self.assertEqual(len(resolve_overrides(self.pool, options)[0]), 2)

    def test_all_mode_does_not_apply_a_count_bound_to_explicit_inclusions(self):
        options = {'mode': 'all', 'questionCount': 1,
                   'includeQuestions': [reference(self.pool[0]), reference(self.pool[1])],
                   'excludeQuestions': [reference(self.pool[-1])]}
        included, excluded, eligible = resolve_overrides(self.pool, options)
        self.assertEqual(len(included), 2)
        self.assertEqual(len(eligible), len(self.pool) - 1)

    def test_reference_mode_forbids_nonempty_manual_overrides_but_accepts_empty_arrays(self):
        for key in ('includeQuestions', 'excludeQuestions'):
            with self.subTest(key=key), self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                resolve_overrides(self.pool, {'mode': 'reference', key: [reference(self.pool[0])]})
        self.assertEqual(resolve_overrides(self.pool, {'mode': 'reference', 'includeQuestions': [],
                                                    'excludeQuestions': []})[2], self.pool)

    def test_excluding_every_question_returns_empty_eligible_pool_without_inventing_a_replacement(self):
        included, excluded, eligible = resolve_overrides(self.pool, {'excludeQuestions': [reference(row) for row in self.pool]})
        self.assertEqual(included, [])
        self.assertEqual(len(excluded), len(self.pool))
        self.assertEqual(eligible, [])

    def test_malformed_current_pool_and_options_are_rejected_without_coercion(self):
        for pool in [None, {}, 'questions', [None], [dict(self.pool[0], identity=None)],
                     [dict(self.pool[0], fingerprint='invalid')], [self.pool[0], self.pool[0]]]:
            with self.subTest(pool=pool), self.assertRaises(PackageValidationError):
                resolve_overrides(pool, {})
        for options in [{'mode': []}, {'mode': 'unknown'}, {'questionCount': 0}, {'questionCount': True},
                        {'questionCount': float('inf')}, {'questionCount': '12'}]:
            with self.subTest(options=options), self.assertRaises(PackageValidationError):
                resolve_overrides(self.pool, options)


if __name__ == '__main__':
    unittest.main()
