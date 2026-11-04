"""Independent, bounded security regressions for local presentation proofs.

These fixtures never launch a browser, Node, a model, an export or a service.
The real learning fingerprint is deliberately imported from the exporter: a
test-only digest must not conceal differences in its production semantics.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from lxp_catalogue import PackageValidationError, canonical_bytes
from lxp_multi_export import learning_compatibility_fingerprint
from lxp_presentation_compat import build_proof, validate_presentation_compatibility


RULE = 'markledge-generated-display-v1'


def source_pair():
    heading = 'Quellen prüfen und Ergebnisse einordnen'
    previous = {
        'language': 'de',
        'config': {'_defaultLanguage': 'de', '_spoor': {'_tracking': {'_submitScore': True}}},
        'course': {'_id': 'course', '_type': 'course', 'title': 'Unveränderter Kurs',
                   '_isTrackable': True},
        'data': {
            'contentObjects': [
                {'_id': 'menu', '_parentId': 'course', '_type': 'menu', 'title': 'Modul',
                 '_markledgeMenuContent': {'_sourceKey': 'Grundschulung/index.md'},
                 '_graphic': {'src': 'course/de/assets/menu-old.svg', 'alt': 'Modulgrafik'}},
                {'_id': 'page', '_parentId': 'menu', '_type': 'page', 'title': 'Lerneinheit',
                 '_markledgeMenuContent': {'_sourceKey': 'Grundschulung/lesson.md'},
                 '_graphic': {'src': 'course/de/assets/page-old.svg', 'alt': 'Kursgrafik'}},
            ],
            'articles': [
                {'_id': 'article', '_parentId': 'page', '_type': 'article',
                 'title': heading, 'displayTitle': heading, '_isAvailable': True},
                {'_id': 'assessment-article', '_parentId': 'page', '_type': 'article',
                 'title': 'Frage', 'displayTitle': 'Frage'},
            ],
            'blocks': [
                {'_id': 'block', '_parentId': 'article', '_type': 'block', 'displayTitle': ''},
                {'_id': 'question-block', '_parentId': 'assessment-article', '_type': 'block',
                 'displayTitle': '', '_isOptional': False},
            ],
            'components': [
                {'_id': 'generated-text', '_parentId': 'block', '_type': 'component',
                 '_component': 'text', 'title': heading, 'displayTitle': heading,
                 'body': '<p>Unveränderter Lerninhalt: ä, ß und 🧭.</p>', '_isOptional': False},
                {'_id': 'following-text', '_parentId': 'block', '_type': 'component',
                 '_component': 'text', 'title': 'Weitere Hinweise', 'displayTitle': 'Weitere Hinweise',
                 'body': '<p>Auch dieser Inhalt bleibt unverändert.</p>'},
                {'_id': 'question', '_parentId': 'question-block', '_type': 'component',
                 '_component': 'mcq', '_isQuestionType': True, 'title': 'Prüffrage',
                 'displayTitle': 'Prüffrage', 'body': '<p>Welche Quelle ist belegt?</p>',
                 '_questionWeight': 1, '_isOptional': False,
                 '_items': [{'text': 'Belegte Quelle', '_shouldBeSelected': True},
                            {'text': 'Ungeprüfte Behauptung', '_shouldBeSelected': False}],
                 '_feedback': {'correct': 'Begründung bleibt exakt erhalten.',
                               '_incorrect': {'final': 'Bitte prüfen Sie die Quelle.'}}},
            ],
        },
    }
    current = deepcopy(previous)
    current['data']['components'][0]['displayTitle'] = ''
    current['data']['contentObjects'][0]['_graphic']['src'] = 'course/de/assets/menu-restored.svg'
    current['data']['contentObjects'][1]['_graphic']['src'] = 'course/de/assets/page-restored.svg'
    return previous, current


class PresentationCompatibilitySecurityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package_root = self.root / 'package'
        self.package_root.mkdir()
        self.previous, self.current = source_pair()
        self.identity = {'packageId': 'local-package-r2', 'scoId': 'sco-current',
                         'originId': 'local-origin', 'courseId': 'native-course'}
        self.sco = {'id': self.identity['scoId'], 'role': 'learning', 'source': {
            'kind': 'native-registered', 'originId': self.identity['originId'],
            'courseId': self.identity['courseId'],
            'componentFingerprint': learning_compatibility_fingerprint(self.current)}}
        self.envelope, self.rows = build_proof(self.previous, self.current, self.identity,
                                             learning_compatibility_fingerprint)
        self.metadata = {'version': 1, 'rule': RULE, 'proofs': [self.install(self.envelope)]}

    def install(self, envelope, *, root=None, raw=None, previous_fingerprint=None):
        fingerprint = previous_fingerprint or envelope['previousFingerprint']
        relative = 'presentation-compatibility/' + self.identity['scoId'] + '/' + fingerprint + '.json'
        target = (root or self.package_root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = canonical_bytes(envelope) if raw is None else raw
        target.write_bytes(raw)
        return {'previousFingerprint': fingerprint, 'path': relative,
                'sha256': hashlib.sha256(raw).hexdigest(),
                'componentDisplayTitles': deepcopy(envelope['componentDisplayTitles'])}

    def validate(self, *, metadata=None, root=None, sco=None, package_id=None, current=None):
        return validate_presentation_compatibility(
            self.metadata if metadata is None else metadata,
            self.package_root if root is None else root,
            self.sco if sco is None else sco,
            self.identity['packageId'] if package_id is None else package_id,
            self.current if current is None else current,
            learning_compatibility_fingerprint)

    def test_real_fingerprint_valid_proof_and_inputs_are_immutable(self):
        previous, current, identity, sco, metadata = map(deepcopy, (
            self.previous, self.current, self.identity, self.sco, self.metadata))
        exact_before = canonical_bytes([previous, current, identity, sco, metadata])
        envelope, rows = build_proof(self.previous, self.current, self.identity,
                                     learning_compatibility_fingerprint)
        self.assertEqual(envelope['previousFingerprint'], learning_compatibility_fingerprint(previous))
        self.assertEqual(envelope['currentFingerprint'], learning_compatibility_fingerprint(current))
        self.assertEqual(rows, [{'componentId': 'generated-text',
            'previousDisplayTitle': previous['data']['articles'][0]['displayTitle'], 'currentDisplayTitle': ''}])
        self.assertEqual(self.validate(), metadata['proofs'])
        self.assertEqual((self.previous, self.current, self.identity, self.sco, self.metadata),
                         (previous, current, identity, sco, metadata))
        self.assertEqual(canonical_bytes([self.previous, self.current, self.identity, self.sco, self.metadata]),
                         exact_before, 'Immutability includes exact JSON value types')
        # Real published_course's filesystem-only build member is not content.
        with_build = deepcopy(previous)
        with_build['build'] = self.root / 'not-read-or-created'
        self.assertEqual(build_proof(with_build, current, identity,
                                     learning_compatibility_fingerprint)[0], envelope)
        self.assertFalse(with_build['build'].exists())

    def test_real_content_feedback_question_flags_config_and_course_changes_fail(self):
        changes = {
            'body': lambda s: s['data']['components'][0].update(body='<p>Anderer Inhalt</p>'),
            'feedback': lambda s: s['data']['components'][2]['_feedback'].update(correct='Andere Rückmeldung'),
            'question-answer': lambda s: s['data']['components'][2]['_items'][0].update(_shouldBeSelected=False),
            'question-weight': lambda s: s['data']['components'][2].update(_questionWeight=0),
            'component-optional': lambda s: s['data']['components'][0].update(_isOptional=True),
            'article-availability': lambda s: s['data']['articles'][0].update(_isAvailable=False),
            'block-optional': lambda s: s['data']['blocks'][1].update(_isOptional=True),
            'article-heading': lambda s: s['data']['articles'][0].update(displayTitle='Andere Überschrift'),
            'config': lambda s: s['config'].update(_defaultLanguage='en'),
            'non-fingerprinted-config': lambda s: s['config'].update(_drawer={'title': 'Andere Navigation'}),
            'course-title': lambda s: s['course'].update(title='Anderer Kurs'),
            'course-flags': lambda s: s['course'].update(_isTrackable=False),
            'language': lambda s: s.update(language='en'),
            'id': lambda s: s['data']['components'][0].update(_id='replacement-id'),
            'parent-link': lambda s: s['data']['components'][0].update(_parentId='question-block'),
            'added-field': lambda s: s['data']['components'][0].update(_resources=[]),
            'removed-field': lambda s: s['data']['components'][0].pop('body'),
            'bool-versus-int-component': lambda s: s['data']['components'][2].update(_questionWeight=True),
            'bool-versus-int-config': lambda s: s['config']['_spoor']['_tracking'].update(_submitScore=1),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                current = deepcopy(self.current)
                change(current)
                frozen = deepcopy(current)
                with self.assertRaises(PackageValidationError):
                    build_proof(self.previous, current, self.identity, learning_compatibility_fingerprint)
                self.assertEqual(current, frozen, 'Rejected input must remain intact')

    def test_collection_order_and_cardinality_are_not_presentation_changes(self):
        for name in ('contentObjects', 'articles', 'blocks', 'components'):
            for operation in ('reverse', 'remove'):
                with self.subTest(collection=name, operation=operation):
                    current = deepcopy(self.current)
                    if operation == 'reverse':
                        current['data'][name].reverse()
                    else:
                        current['data'][name].pop()
                    with self.assertRaises(PackageValidationError):
                        build_proof(self.previous, current, self.identity, learning_compatibility_fingerprint)

    def test_only_first_visible_plain_duplicate_text_heading_is_eligible(self):
        cases = ('later-component', 'question-type', 'different-title', 'hidden-article',
                 'hidden-block', 'hidden-component', 'block-heading', 'html-heading', 'entity-heading')
        for case in cases:
            with self.subTest(case=case):
                previous, current = source_pair()
                if case == 'later-component':
                    for source in (previous, current):
                        source['data']['components'][0:2] = list(reversed(source['data']['components'][0:2]))
                elif case == 'question-type':
                    for source in (previous, current):
                        source['data']['components'][0]['_isQuestionType'] = True
                elif case == 'different-title':
                    for source in (previous, current):
                        source['data']['components'][0]['title'] = 'Nicht die Artikelüberschrift'
                elif case.startswith('hidden-'):
                    collection = {'hidden-article': 'articles', 'hidden-block': 'blocks',
                                  'hidden-component': 'components'}[case]
                    for source in (previous, current):
                        source['data'][collection][0]['_isVisible'] = False
                elif case == 'block-heading':
                    for source in (previous, current):
                        source['data']['blocks'][0]['displayTitle'] = 'Eigene Blocküberschrift'
                else:
                    heading = '<h2>Überschrift</h2>' if case == 'html-heading' else 'Quelle &amp; Kontext'
                    for source in (previous, current):
                        source['data']['articles'][0]['displayTitle'] = heading
                        source['data']['components'][0]['title'] = heading
                    previous['data']['components'][0]['displayTitle'] = heading
                with self.assertRaises(PackageValidationError):
                    build_proof(previous, current, self.identity, learning_compatibility_fingerprint)

    def test_graphics_need_same_exact_markledge_source_key_and_safe_existing_graphics(self):
        changes = {
            'source-key': lambda s: s['data']['contentObjects'][1]['_markledgeMenuContent'].update(_sourceKey='Other/lesson.md'),
            'missing-marker': lambda s: s['data']['contentObjects'][1].pop('_markledgeMenuContent'),
            'unsafe-key': lambda s: s['data']['contentObjects'][1]['_markledgeMenuContent'].update(_sourceKey='../lesson.md'),
            'unsafe-graphic': lambda s: s['data']['contentObjects'][1]['_graphic'].update(src='../outside.svg'),
            'external-graphic': lambda s: s['data']['contentObjects'][1]['_graphic'].update(src='https://invalid.example/header.svg'),
            'graphic-field': lambda s: s['data']['contentObjects'][1]['_graphic'].update(_isAvailable=False),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                current = deepcopy(self.current)
                change(current)
                with self.assertRaises(PackageValidationError):
                    build_proof(self.previous, current, self.identity, learning_compatibility_fingerprint)
        previous = deepcopy(self.previous)
        previous['data']['contentObjects'][1].pop('_graphic')
        with self.assertRaises(PackageValidationError):
            build_proof(previous, self.current, self.identity, learning_compatibility_fingerprint)

    def test_graphics_only_empty_title_rows_are_valid_and_two_proofs_are_allowed(self):
        previous = deepcopy(self.previous)
        previous['data']['components'][0]['displayTitle'] = ''
        envelope, rows = build_proof(previous, self.current, self.identity, learning_compatibility_fingerprint)
        self.assertEqual(rows, [])
        metadata = deepcopy(self.metadata)
        metadata['proofs'].append(self.install(envelope))
        self.assertEqual(self.validate(metadata=metadata), metadata['proofs'])

    def test_noop_and_self_alias_are_rejected_even_with_a_valid_hash_shape(self):
        with self.assertRaises(PackageValidationError):
            build_proof(self.current, self.current, self.identity, learning_compatibility_fingerprint)
        with self.assertRaises(PackageValidationError):
            build_proof(self.previous, self.current, self.identity, lambda _: 'a' * 64)
        metadata = deepcopy(self.metadata)
        fingerprint = learning_compatibility_fingerprint(self.current)
        metadata['proofs'][0]['previousFingerprint'] = fingerprint
        metadata['proofs'][0]['path'] = 'presentation-compatibility/sco-current/' + fingerprint + '.json'
        with self.assertRaisesRegex(PackageValidationError, 'self fingerprint'):
            self.validate(metadata=metadata)

    def test_metadata_requires_exact_rule_version_fields_and_one_or_two_unique_proofs(self):
        for field, value in (('version', True), ('version', 2), ('rule', 'allow-all-display'), ('extra', True)):
            with self.subTest(field=field, value=value):
                metadata = deepcopy(self.metadata)
                metadata[field] = value
                with self.assertRaises(PackageValidationError):
                    self.validate(metadata=metadata)
        for count in (0, 3):
            with self.subTest(proof_count=count):
                metadata = deepcopy(self.metadata)
                metadata['proofs'] *= count
                with self.assertRaisesRegex(PackageValidationError, 'one or two'):
                    self.validate(metadata=metadata)
        metadata = deepcopy(self.metadata)
        metadata['proofs'] *= 2
        with self.assertRaisesRegex(PackageValidationError, 'duplicate'):
            self.validate(metadata=metadata)

    def test_proof_paths_must_be_canonical_not_merely_resolvable(self):
        canonical = self.metadata['proofs'][0]['path']
        paths = ('../' + canonical, '/' + canonical, './' + canonical, canonical + '?raw=1',
                 canonical + '#proof', canonical.replace('/', '\\'), canonical.replace('/', '%2f', 1),
                 canonical.replace('sco-current/', 'foreign-sco/'),
                 canonical.replace('sco-current/', 'sco-current/../sco-current/'))
        for path in paths:
            with self.subTest(path=path):
                metadata = deepcopy(self.metadata)
                metadata['proofs'][0]['path'] = path
                with self.assertRaisesRegex(PackageValidationError, 'unsafe or duplicate proof path'):
                    self.validate(metadata=metadata)

    def test_symlink_package_root_ancestor_sco_directory_and_proof_are_rejected(self):
        root_link = self.root / 'package-link'
        root_link.symlink_to(self.package_root, target_is_directory=True)
        with self.assertRaisesRegex(PackageValidationError, 'cannot be safely read'):
            self.validate(root=root_link)
        for component in ('presentation-compatibility', 'sco-current', 'proof'):
            with self.subTest(link=component):
                root = self.root / ('linked-' + component)
                root.mkdir()
                if component == 'presentation-compatibility':
                    (root / component).symlink_to(self.package_root / component, target_is_directory=True)
                elif component == 'sco-current':
                    (root / 'presentation-compatibility').mkdir()
                    (root / 'presentation-compatibility/sco-current').symlink_to(
                        self.package_root / 'presentation-compatibility/sco-current', target_is_directory=True)
                else:
                    target = root / self.metadata['proofs'][0]['path']
                    target.parent.mkdir(parents=True)
                    target.symlink_to(self.package_root / self.metadata['proofs'][0]['path'])
                with self.assertRaisesRegex(PackageValidationError, 'cannot be safely read'):
                    self.validate(root=root)

    def test_proof_over_two_mib_is_rejected_before_json_parsing(self):
        oversized = b' ' * (2 * 1024 * 1024 + 1)
        metadata = deepcopy(self.metadata)
        metadata['proofs'][0] = self.install(self.envelope, raw=oversized)
        with self.assertRaisesRegex(PackageValidationError, 'ordinary file|2 MiB|read limit'):
            self.validate(metadata=metadata)

    def test_tampered_bytes_and_metadata_digest_cannot_pass(self):
        metadata = deepcopy(self.metadata)
        metadata['proofs'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(PackageValidationError, 'SHA256'):
            self.validate(metadata=metadata)
        path = self.package_root / self.metadata['proofs'][0]['path']
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(PackageValidationError, 'SHA256'):
            self.validate()

    def test_foreign_identity_is_rejected_even_after_resigning_proof(self):
        for field in ('packageId', 'scoId', 'originId', 'courseId'):
            with self.subTest(field=field):
                envelope = deepcopy(self.envelope)
                envelope['identity'][field] = 'foreign-' + field
                metadata = deepcopy(self.metadata)
                metadata['proofs'][0] = self.install(envelope)
                with self.assertRaisesRegex(PackageValidationError, 'identity'):
                    self.validate(metadata=metadata)
        self.install(self.envelope)
        with self.assertRaisesRegex(PackageValidationError, 'identity'):
            self.validate(package_id='other-package')

    def test_signed_envelope_cannot_change_prior_content_or_derived_fingerprints(self):
        changes = {
            'prior-body': lambda e: e['previousSource']['data']['components'][0].update(body='Forged body'),
            'prior-feedback': lambda e: e['previousSource']['data']['components'][2]['_feedback'].update(correct='Forged'),
            'prior-config': lambda e: e['previousSource']['config'].update(_drawer={'title': 'Forged'}),
            'prior-course': lambda e: e['previousSource']['course'].update(title='Forged'),
            'old-fingerprint': lambda e: e.update(previousFingerprint='1' * 64),
            'current-fingerprint': lambda e: e.update(currentFingerprint='2' * 64),
            'envelope-rule': lambda e: e.update(rule='not-the-allowed-rule'),
            'envelope-version': lambda e: e.update(version=True),
            'unknown-field': lambda e: e.update(allowAnyContent=True),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                envelope = deepcopy(self.envelope)
                change(envelope)
                metadata = deepcopy(self.metadata)
                metadata['proofs'][0] = self.install(envelope)
                with self.assertRaises(PackageValidationError):
                    self.validate(metadata=metadata)

    def test_title_rows_are_exact_replayed_changes_not_a_trusted_allowlist(self):
        for case in ('missing', 'foreign-component', 'different-prior-title', 'nonempty-current', 'duplicate'):
            with self.subTest(case=case):
                metadata = deepcopy(self.metadata)
                rows = metadata['proofs'][0]['componentDisplayTitles']
                if case == 'missing':
                    rows.clear()
                elif case == 'foreign-component':
                    rows[0]['componentId'] = 'question'
                elif case == 'different-prior-title':
                    rows[0]['previousDisplayTitle'] = 'Invented title'
                elif case == 'nonempty-current':
                    rows[0]['currentDisplayTitle'] = 'Not suppressed'
                else:
                    rows.append(deepcopy(rows[0]))
                with self.assertRaises(PackageValidationError):
                    self.validate(metadata=metadata)
        envelope = deepcopy(self.envelope)
        envelope['componentDisplayTitles'][0]['componentId'] = 'following-text'
        metadata = deepcopy(self.metadata)
        metadata['proofs'][0] = self.install(envelope)
        with self.assertRaises(PackageValidationError):
            self.validate(metadata=metadata)

    def test_bounded_title_rows_duplicate_json_keys_and_nonfinite_values_fail(self):
        metadata = deepcopy(self.metadata)
        metadata['proofs'][0]['componentDisplayTitles'] = [
            {'componentId': 'component-' + str(index), 'previousDisplayTitle': 'Heading', 'currentDisplayTitle': ''}
            for index in range(513)]
        with self.assertRaisesRegex(PackageValidationError, 'title-change list'):
            self.validate(metadata=metadata)
        canonical = canonical_bytes(self.envelope)
        for raw in (b'{"version":1,' + canonical[1:],
                    b'{"extra":NaN,' + canonical[1:]):
            with self.subTest(raw_prefix=raw[:25]):
                metadata = deepcopy(self.metadata)
                metadata['proofs'][0] = self.install(self.envelope, raw=raw)
                with self.assertRaisesRegex(PackageValidationError, 'duplicate JSON key|nonfinite'):
                    self.validate(metadata=metadata)

    def test_actual_delivered_source_and_learning_only_scope_are_mandatory(self):
        for role in ('entry', 'final'):
            with self.subTest(role=role):
                sco = deepcopy(self.sco)
                sco['role'] = role
                sco['source']['kind'] = 'generated-assessment'
                with self.assertRaisesRegex(PackageValidationError, 'learning-only'):
                    self.validate(sco=sco)
        sco = deepcopy(self.sco)
        sco['source']['componentFingerprint'] = '0' * 64
        with self.assertRaisesRegex(PackageValidationError, 'current canonical fingerprint'):
            self.validate(sco=sco)
        current = deepcopy(self.current)
        current['data']['components'][2]['_items'][0]['_shouldBeSelected'] = False
        with self.assertRaises(PackageValidationError):
            self.validate(current=current)


if __name__ == '__main__':
    unittest.main()
