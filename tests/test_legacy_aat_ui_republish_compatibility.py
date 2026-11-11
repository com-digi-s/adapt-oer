"""One audited DigComp UI-default republish; not generic display normalization."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from lxp_catalogue import PackageValidationError
from lxp_multi_export import learning_compatibility_fingerprint
from lxp_presentation_compat import (RULE, build_proof, registered_proof_payloads,
    validate_presentation_compatibility)
from test_presentation_compatibility import source, IDENTITY


LEGACY_IDENTITY = {**IDENTITY, 'courseId': '6ae22a899986b0c258cb95a6'}
THEME = '660c6033c240300006d0c110'
REMOVALS = (
    (('config', 'screenSize', 'xlarge'), 1280),
    (('course', '_globals', '_accessibility', '_ariaLabels', 'optional'), 'Optional'),
    (('course', '_globals', '_accessibility', '_ariaLabels', 'required'), 'Required'),
    (('course', '_globals', '_accessibility', 'altFeedbackTitle'), 'Feedback'),
    (('course', '_globals', '_extensions', '_additionalResources'), {}),
    (('course', '_globals', '_extensions', '_drawer'), {
        '_navOrder': 100, '_navTooltip': {'_isEnabled': True, 'text': '{{ariaLabel}}'}}),
    (('course', '_globals', '_extensions', '_navigation'), {
        '_skipButton': {'_navOrder': -100}, '_backButton': {'_navOrder': 0},
        '_backNavTooltip': {'_isEnabled': True, 'text': '{{ariaLabel}}'}, '_spacers': [{'_navOrder': 0}]}),
)


def parent(data, path):
    for key in path[:-1]:
        data = data.setdefault(key, {})
    return data


def legacy_pair():
    previous = source()
    previous['config']['screenSize'] = {'small': 520, 'medium': 760, 'large': 1024}
    previous['config']['_spoor'] = {'_isEnabled': True}
    previous['course']['_globals'] = {'_accessibility': {'unrelated': 'Keep'}, '_extensions': {'unrelated': {}}}
    for path, value in REMOVALS:
        parent(previous, path)[path[-1]] = deepcopy(value)
    current = deepcopy(previous)
    for path, _ in REMOVALS:
        del parent(current, path)[path[-1]]
    current['config']['_themePreset'] = THEME
    return previous, current


class LegacyAatUiRepublishTest(unittest.TestCase):
    def setUp(self):
        self.previous, self.current = legacy_pair()

    def proof(self, previous=None, current=None, identity=None):
        return build_proof(self.previous if previous is None else previous,
            self.current if current is None else current,
            LEGACY_IDENTITY if identity is None else identity, learning_compatibility_fingerprint)

    def test_exact_ui_exception_is_pure_and_preserves_all_component_state_fields(self):
        before, after = deepcopy(self.previous), deepcopy(self.current)
        envelope, rows = self.proof()
        self.assertEqual(rows, [])
        self.assertEqual(envelope['componentDisplayTitles'], [])
        self.assertEqual(envelope['rule'], RULE)
        self.assertNotEqual(envelope['previousFingerprint'], envelope['currentFingerprint'])
        self.assertEqual(self.previous['data'], self.current['data'])
        self.assertEqual((self.previous, self.current), (before, after))

    def test_ui_exception_is_bound_to_the_single_native_course(self):
        for course in ('other', '6ae22a899986b0c258cb95a7'):
            with self.subTest(course=course), self.assertRaises(PackageValidationError):
                self.proof(identity={**LEGACY_IDENTITY, 'courseId': course})

    def test_every_proper_subset_of_eight_transitions_is_refused(self):
        for mask in range(255):
            current = deepcopy(self.previous)
            for index, (path, _) in enumerate(REMOVALS):
                if mask & (1 << index):
                    del parent(current, path)[path[-1]]
            if mask & 128:
                current['config']['_themePreset'] = THEME
            with self.subTest(mask=mask), self.assertRaises(PackageValidationError):
                self.proof(current=current)

    def test_exact_old_values_and_json_types_are_required(self):
        wrong_values = (1280.0, 'optional', 'required', 'feedback', None,
            {'_navOrder': 100, '_navTooltip': {'_isEnabled': 1, 'text': '{{ariaLabel}}'}},
            {'_skipButton': {'_navOrder': -100}, '_backButton': {'_navOrder': False},
             '_backNavTooltip': {'_isEnabled': True, 'text': '{{ariaLabel}}'}, '_spacers': [{'_navOrder': 0}]})
        for (path, _), value in zip(REMOVALS, wrong_values):
            previous = deepcopy(self.previous)
            parent(previous, path)[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(PackageValidationError):
                self.proof(previous=previous)

    def test_removals_are_absence_not_null_and_theme_is_exactly_added(self):
        for path, _ in REMOVALS:
            current = deepcopy(self.current)
            parent(current, path)[path[-1]] = None
            with self.subTest(path=path), self.assertRaises(PackageValidationError):
                self.proof(current=current)
        for value in (None, 'another-theme', 660):
            current = deepcopy(self.current); current['config']['_themePreset'] = value
            with self.subTest(theme=value), self.assertRaises(PackageValidationError):
                self.proof(current=current)
        previous = deepcopy(self.previous); previous['config']['_themePreset'] = THEME
        with self.assertRaises(PackageValidationError):
            self.proof(previous=previous)

    def test_graphics_titles_answers_feedback_and_completion_cannot_mix_with_ui_exception(self):
        changes = (
            lambda d: d['data']['contentObjects'][0]['_graphic'].update(src='course/assets/new.svg'),
            lambda d: d['data']['components'][0].update(displayTitle=''),
            lambda d: d['course'].update(title='Another title'),
            lambda d: d['course'].update(_boxMenu={'_graphic': {'_src': 'assets/new.png'}}),
            lambda d: d['data']['components'][1]['_items'][0].update(_shouldBeSelected=False),
            lambda d: d['data']['components'][1].update(_feedback={'correct': 'Changed'}),
            lambda d: d['data']['components'][1].update(_attempts=2, _questionWeight=2),
            lambda d: d['data']['contentObjects'][0].update(_isOptional=True),
            lambda d: d['data']['articles'][0].update(_isAvailable=False),
            lambda d: d['data']['blocks'][0].update(_trackingId=2),
            lambda d: d['config']['_spoor'].update(_isEnabled=False),
            lambda d: d['config']['screenSize'].update(medium=761),
            lambda d: d['config'].update(_completionCriteria={'_requireContentCompleted': False}),
            lambda d: d['course']['_globals']['_accessibility'].update(unrelated='Changed'),
            lambda d: d['data']['components'].reverse(),
            lambda d: d.update(language='en'),
        )
        for index, change in enumerate(changes):
            current = deepcopy(self.current); change(current)
            with self.subTest(change=index), self.assertRaises(PackageValidationError):
                self.proof(current=current)

    def test_registered_and_delivered_proof_use_the_same_full_replay(self):
        envelope, _ = self.proof()
        registration = {LEGACY_IDENTITY['packageId']: {envelope['currentFingerprint']: [envelope]}}
        payloads = registered_proof_payloads(registration, self.current,
            LEGACY_IDENTITY, learning_compatibility_fingerprint)
        self.assertEqual(len(payloads), 1)
        path, raw, record = payloads[0]
        self.assertEqual(json.loads(raw), envelope)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256'])
        metadata = {'version': 1, 'rule': RULE, 'proofs': [record]}
        sco = {'id': LEGACY_IDENTITY['scoId'], 'role': 'learning', 'source': {
            'originId': LEGACY_IDENTITY['originId'], 'courseId': LEGACY_IDENTITY['courseId'],
            'componentFingerprint': envelope['currentFingerprint']}}
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / path
            file.parent.mkdir(parents=True); file.write_bytes(raw)
            self.assertEqual(validate_presentation_compatibility(metadata, directory, sco,
                LEGACY_IDENTITY['packageId'], self.current, learning_compatibility_fingerprint), [record])
            tampered = deepcopy(envelope)
            tampered['previousSource']['config']['screenSize']['xlarge'] = 1281
            registration[LEGACY_IDENTITY['packageId']][envelope['currentFingerprint']] = [tampered]
            with self.assertRaises(PackageValidationError):
                registered_proof_payloads(registration, self.current,
                    LEGACY_IDENTITY, learning_compatibility_fingerprint)


if __name__ == '__main__':
    unittest.main()
