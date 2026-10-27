"""Manual pins refine an actual phase pool without changing legacy policies."""
from copy import deepcopy
import hashlib
import tempfile
from pathlib import Path
import unittest

from lxp_assessment_selection import normalize_assessment_options, select_questions
from lxp_catalogue import PackageValidationError, canonical_bytes, load_catalogue
from lxp_catalogue_store import read_snapshot, save_catalogue
import test_assessment_selection as fixtures


def reference(candidate):
    return {'identity': list(candidate['identity']), 'fingerprint': candidate['fingerprint']}


class ManualAssessmentSelectionTest(unittest.TestCase):
    def setUp(self):
        self.pool, self.framework = fixtures.pool(), fixtures.framework()

    def select(self, options, candidates=None, **changes):
        return select_questions(self.pool if candidates is None else candidates, options,
                                self.framework, 'entry', **changes)

    def test_no_override_legacy_summary_bytes_and_algorithms_are_exactly_preserved(self):
        cases = [
            ({'questionCount': 12}, None, '9253373e67e31b3c073d72893cd7d78d4b4720c3a08f337f1f098138f9299c18'),
            ({'mode': 'all', 'questionCount': 1}, None, '857f31da0dba05ac1d0e0942a2d0cb9bd130b19820b12754d8722dd4d640f822'),
            ({'questionCount': 9, 'equalSampling': False}, None, '8316f0b5bccb30b7d1a746e5fb203859be55e70739a9acce69fba81380f5d20a'),
            ({'mode': 'reference', 'questionCount': 2}, [self.pool[5]['identity'], self.pool[1]['identity']],
             'ee6bf868f1bdb59cdd206387792ed308e7b38223ed42ea80c893937d1e916e98'),
        ]
        for options, references, expected in cases:
            for empty in ({}, {'includeQuestions': [], 'excludeQuestions': []}):
                _, summary = self.select({**options, **empty}, reference_identities=references)
                with self.subTest(options=options, empty=empty):
                    self.assertEqual(hashlib.sha256(canonical_bytes(summary)).hexdigest(), expected)
                    self.assertNotIn('availableQuestions', summary)
                    self.assertEqual(len(summary['options']), 5)

    def test_pins_win_then_existing_balanced_policy_fills_without_duplicates(self):
        pins = [self.pool[-1], self.pool[0]]
        excluded = [self.pool[1], self.pool[2]]
        options = {'questionCount': 8, 'includeQuestions': [reference(row) for row in pins],
                   'excludeQuestions': [reference(row) for row in excluded]}
        selected, summary = self.select(options, include_available_questions=True)
        identities = [tuple(row['identity']) for row in selected]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(len(selected), 8)
        self.assertEqual(set(identities[:2]), {tuple(row['identity']) for row in pins})
        self.assertFalse(set(identities) & {tuple(row['identity']) for row in excluded})
        self.assertEqual(summary['algorithm'], 'adapt-oer-manual-balanced-selection@1')
        self.assertEqual((summary['poolCount'], summary['eligibleCount'], summary['includedCount'], summary['excludedCount']), (36, 34, 2, 2))
        self.assertEqual(sum(row['poolCount'] for row in summary['coverage']['modules']), 36)
        self.assertTrue(all(any(candidate is row for row in self.pool) for candidate in selected))

    def test_manual_selection_is_order_independent_and_non_mutating(self):
        options = {'questionCount': 7, 'includeQuestions': [reference(self.pool[0]), reference(self.pool[-1])],
                   'excludeQuestions': [reference(self.pool[1]), reference(self.pool[2])]}
        original_pool, original_options = deepcopy(self.pool), deepcopy(options)
        expected = self.select(options, include_available_questions=True)
        reordered = {**options, 'includeQuestions': list(reversed(options['includeQuestions'])),
                     'excludeQuestions': list(reversed(options['excludeQuestions']))}
        actual = self.select(reordered, list(reversed(self.pool)), include_available_questions=True)
        self.assertEqual(actual, expected)
        self.assertEqual(self.pool, original_pool)
        self.assertEqual(options, original_options)

    def test_all_mode_keeps_every_eligible_question_once_and_pins_first(self):
        pin, exclusion = self.pool[-1], self.pool[0]
        selected, summary = self.select({'mode': 'all', 'questionCount': 1,
            'includeQuestions': [reference(pin)], 'excludeQuestions': [reference(exclusion)]})
        self.assertIs(selected[0], pin)
        self.assertEqual(len(selected), 35)
        self.assertEqual(len({tuple(row['identity']) for row in selected}), 35)
        self.assertNotIn(exclusion, selected)
        self.assertIsNone(summary['requestedCount'])
        self.assertIsNone(summary['options']['questionCount'])
        self.assertEqual(summary['algorithm'], 'adapt-oer-manual-all-selection@1')

    def test_flat_manual_selection_remains_explicitly_unbalanced(self):
        selected, summary = self.select({'questionCount': 2, 'equalSampling': False,
                                        'includeQuestions': [reference(self.pool[-1])]})
        self.assertEqual(len(selected), 2)
        self.assertIs(selected[0], self.pool[-1])
        self.assertEqual(summary['algorithm'], 'adapt-oer-manual-fixed-selection@1')

    def test_underfilled_and_completely_excluded_pools_report_honest_counts(self):
        excluded = self.pool[2:]
        selected, summary = self.select({'questionCount': 12, 'excludeQuestions': [reference(row) for row in excluded]})
        self.assertEqual(len(selected), 2)
        self.assertEqual(summary['poolCount'], 36)
        self.assertEqual(summary['eligibleCount'], 2)
        self.assertEqual(summary['requestedCount'], 12)
        self.assertIn('nur 2 geeignete Fragen', summary['warnings'][0])
        selected, summary = self.select({'questionCount': 12, 'excludeQuestions': [reference(row) for row in self.pool]},
                                        include_available_questions=True)
        self.assertEqual(selected, [])
        self.assertEqual(summary['selectedCount'], 0)
        self.assertEqual(summary['eligibleCount'], 0)
        self.assertTrue(all(row['status'] == 'not-assessed' for row in summary['coverage']['domains']))
        self.assertTrue(all(row['manualChoice'] == 'exclude' and row['selected'] is False
                            for row in summary['availableQuestions']))

    def test_pins_cannot_increase_requested_count_and_reference_cannot_be_modified(self):
        with self.assertRaisesRegex(PackageValidationError, 'mehr Fragen fest eingeschlossen'):
            self.select({'questionCount': 1, 'includeQuestions': [reference(self.pool[0]), reference(self.pool[1])]})
        for key in ('includeQuestions', 'excludeQuestions'):
            with self.subTest(key=key), self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                self.select({'mode': 'reference', key: [reference(self.pool[0])]},
                            reference_identities=[self.pool[0]['identity']])

    def test_stale_deleted_foreign_and_other_phase_questions_are_rejected(self):
        for key in ('includeQuestions', 'excludeQuestions'):
            options = {key: [reference(self.pool[0])]}
            with self.subTest(key=key, kind='deleted'), self.assertRaises(PackageValidationError):
                self.select(options, self.pool[1:])
            options[key][0]['fingerprint'] = '0' * 64
            with self.subTest(key=key, kind='stale'), self.assertRaises(PackageValidationError):
                self.select(options)
            options[key][0] = {**reference(self.pool[0]), 'identity': ['another-tenant', 'course', 'component']}
            with self.subTest(key=key, kind='foreign'), self.assertRaises(PackageValidationError):
                self.select(options)
        with self.assertRaises(PackageValidationError):
            select_questions(self.pool[1:], {'includeQuestions': [reference(self.pool[0])]}, self.framework, 'final')

    def test_phase_overrides_are_independent_and_source_component_collisions_are_scoped(self):
        pin = self.pool[0]
        twin = next(row for row in self.pool if row['identity'][-1] == pin['identity'][-1]
                    and row['identity'][:2] != pin['identity'][:2])
        options = normalize_assessment_options({'DigComp': {
            'entry': {'questionCount': 2, 'includeQuestions': [reference(pin)], 'excludeQuestions': [reference(twin)]},
            'final': {'questionCount': 2, 'excludeQuestions': [reference(pin)], 'includeQuestions': [reference(twin)]},
        }}, {'frameworks': [self.framework]})['fw-dig']
        for phase, required, forbidden in [('entry', pin, twin), ('final', twin, pin)]:
            selected, _ = select_questions(self.pool, options[phase], self.framework, phase)
            self.assertIn(required, selected)
            self.assertNotIn(forbidden, selected)

    def test_available_inventory_is_opt_in_safe_complete_and_marks_manual_choices(self):
        options = {'questionCount': 2, 'includeQuestions': [reference(self.pool[0])],
                   'excludeQuestions': [reference(self.pool[1])]}
        _, pure_summary = self.select(options)
        self.assertNotIn('availableQuestions', pure_summary)
        selected, summary = self.select(options, include_available_questions=True)
        keys = {'identity', 'sourceCourseId', 'sourceComponentId', 'title', 'domainId', 'fingerprint',
                'moduleId', 'manualChoice', 'selected'}
        inventory = summary['availableQuestions']
        self.assertEqual(len(inventory), len(self.pool))
        self.assertTrue(all(set(row) == keys for row in inventory))
        self.assertTrue(all(type(row['selected']) is bool for row in inventory))
        by_identity = {tuple(row['identity']): row for row in inventory}
        self.assertEqual(by_identity[tuple(self.pool[0]['identity'])]['manualChoice'], 'include')
        self.assertEqual(by_identity[tuple(self.pool[1]['identity'])]['manualChoice'], 'exclude')
        self.assertFalse(by_identity[tuple(self.pool[1]['identity'])]['selected'])
        self.assertEqual(sum(row['selected'] for row in inventory), len(selected))
        for value in [1, None, 'yes']:
            with self.subTest(value=value), self.assertRaises(PackageValidationError):
                self.select({}, include_available_questions=value)

    def test_global_catalogue_defaults_reject_course_specific_overrides_even_empty(self):
        base = load_catalogue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'override.json'
            current = read_snapshot(base, path)
            for key in ('includeQuestions', 'excludeQuestions'):
                for value in [[], [reference(self.pool[0])]]:
                    candidate = deepcopy(base)
                    candidate['frameworks'][0]['defaultAssessmentOptions'] = {'entry': {key: value}}
                    with self.subTest(key=key, value=value), self.assertRaisesRegex(PackageValidationError, 'export profile'):
                        save_catalogue(base, path, candidate, current['sha256'])
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
