"""Actual production preview/ZIP integration; no GUI or native-browser claim."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from lxp_catalogue import PackageValidationError, load_catalogue
from lxp_export import build_multi_framework_export
from lxp_multi_export import tree_manifest, _reference_question_identities
import test_multi_framework_export as export_fixture
from test_multi_framework_export import save


class AssessmentSelectionExportTest(unittest.TestCase):
    def setUp(self):
        self.fixture = export_fixture.MixedPackageTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for build in self.fixture.builds:
            path = build / 'course/de/components.json'
            original = json.loads(path.read_text())[0]
            questions = []
            for index in range(5):
                question = deepcopy(original)
                question.update(_id='question-' + str(index), body='Actual source prompt ' + str(index))
                questions.append(question)
            save(path, questions)
        self.source_hashes = [tree_manifest(build) for build in self.fixture.builds]

    def prepare(self, **changes):
        prepared = self.fixture.prepare(**changes)
        self.addCleanup(prepared.close)
        return prepared

    def options(self, count=2):
        return {prefix: {phase: {'mode': 'balanced', 'questionCount': count,
            'equalSampling': True, 'showMarking': phase == 'final', 'showModelAnswer': False}
            for phase in ('entry', 'final')} for prefix in ('DigComp', 'AILit')}

    def test_limited_selection_is_the_actual_confirmed_native_quiz_and_does_not_modify_sources(self):
        prepared = self.prepare(assessment_options=self.options())
        summaries = prepared.preview['assessmentSelection']
        self.assertEqual(len(summaries), 4)
        self.assertTrue(all(row['poolCount'] == 5 and row['selectedCount'] == 2 for row in summaries))
        with ZipFile(build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256)) as archive:
            actual = json.loads(archive.read('assessment-selection.json'))
            provenance = json.loads(archive.read('package-provenance.json'))
            self.assertEqual(actual, summaries)
            self.assertEqual(provenance['assessmentSelection'], summaries)
            for assessment in prepared.contract['assessments']:
                sco = next(row for row in prepared.contract['scos'] if row['id'] == assessment['scoId'])
                summary = next(row for row in summaries if row['frameworkId'] == assessment['frameworkId'] and row['phase'] == assessment['phase'])
                expected = {row['sourceComponentId'] for row in summary['questions']}
                contract_questions = {row['id']: row for row in prepared.contract['questions']}
                bank_ids = {qid for bank in assessment['banks'] for qid in bank['questionIds']}
                self.assertEqual({contract_questions[qid]['sourceComponentId'] for qid in bank_ids}, expected)
                root = sco['launch']['root'] + '/course/en/'
                components = json.loads(archive.read(root + 'components.json'))
                native = [row for row in components if row['_component'] != 'text']
                self.assertEqual(len(native), 2)
                self.assertEqual({row['_id'] for row in native}, {contract_questions[qid]['runtimeQuestionId'] for qid in bank_ids})
                marking = assessment['phase'] == 'final'
                self.assertTrue(all(row['_canShowMarking'] is marking and row['_canShowModelAnswer'] is False for row in native))
                article = json.loads(archive.read(root + 'articles.json'))[0]['_assessment']
                self.assertIs(article['_suppressMarking'], not marking)
                self.assertIs(article['_questions']['_canShowMarking'], marking)
                self.assertIs(article['_questions']['_canShowModelAnswer'], False)
        self.assertEqual([tree_manifest(build) for build in self.fixture.builds], self.source_hashes)

    def test_small_pool_is_explicit_and_zero_is_not_a_test_length(self):
        prepared = self.prepare(assessment_options=self.options(100))
        self.assertTrue(all(row['selectedCount'] == 5 and row['requestedCount'] == 100 and row['warnings'] for row in prepared.preview['assessmentSelection']))
        with self.assertRaises(PackageValidationError):
            self.prepare(assessment_options=self.options(0))

    def test_settings_and_concrete_question_fingerprints_bind_the_confirmation(self):
        first = self.prepare(assessment_options=self.options(2))
        second = self.prepare(assessment_options=self.options(3))
        self.assertNotEqual(first.preflight_sha256, second.preflight_sha256)
        self.assertTrue(all(len(row['fingerprint']) == 64 for summary in first.preview['assessmentSelection'] for row in summary['questions']))
        first._assessment_selection[0]['options']['showModelAnswer'] = True
        with self.assertRaisesRegex(PackageValidationError, 'changed'):
            build_multi_framework_export(first, confirmed_preflight_sha256=first.preflight_sha256)

    def test_public_preview_cannot_rewrite_the_frozen_export_selection(self):
        prepared = self.prepare(assessment_options=self.options())
        expected = deepcopy(prepared.preview['assessmentSelection'])
        prepared.preview['assessmentSelection'][0]['selectedCount'] = 999
        with ZipFile(build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256)) as archive:
            self.assertEqual(json.loads(archive.read('assessment-selection.json')), expected)

    def test_old_api_callers_keep_the_full_question_pool(self):
        prepared = self.prepare()
        self.assertTrue(all(row['selectedCount'] == 5 and row['options']['mode'] == 'all' for row in prepared.preview['assessmentSelection']))

    def test_known_reference_requires_matching_content_weight_and_complete_sources(self):
        reference = json.loads((Path(__file__).resolve().parents[1] / 'static/assessment-reference-digcomp22.json').read_text())
        framework = next(row for row in load_catalogue()['frameworks'] if row['prefix'] == 'DigComp')
        for phase in ('entry', 'final'):
            rows = reference['phases'][phase]['questions']
            candidates = [{'identity': ('actual-origin', 'course-' + row['moduleNumber'] + '.' + row['courseNumber'], row['sourceComponentId']),
                'moduleNumber': row['moduleNumber'], 'courseNumber': row['courseNumber'],
                'fingerprint': row['questionSha256'], 'domainId': row['domainId'],
                'effectiveQuestionWeight': row['effectiveQuestionWeight']} for row in rows]
            self.assertEqual(len(_reference_question_identities(candidates, framework, phase)), len(rows))
            for mutate in (lambda values: values.pop(), lambda values: values[0].update(fingerprint='0' * 64),
                           lambda values: values[0].update(effectiveQuestionWeight=9)):
                bad = deepcopy(candidates); mutate(bad)
                with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                    _reference_question_identities(bad, framework, phase)

    def test_reimported_reference_questions_require_exact_source_scoped_aliases(self):
        reference = json.loads((Path(__file__).resolve().parents[1] / 'static/assessment-reference-digcomp22.json').read_text())
        framework = next(row for row in load_catalogue()['frameworks'] if row['prefix'] == 'DigComp')
        for phase in ('entry', 'final'):
            candidates, aliases = [], {}
            rows = reference['phases'][phase]['questions']
            for row in rows:
                course = 'actual-' + row['moduleNumber'] + '.' + row['courseNumber']
                source_key = json.dumps(['actual-aat', course], separators=(',', ':'))
                current_id = 'imported-' + row['sourceComponentId']
                aliases.setdefault(source_key, {})[row['sourceComponentId']] = current_id
                candidates.append({'identity': ('actual-aat', course, current_id),
                    'moduleNumber': row['moduleNumber'], 'courseNumber': row['courseNumber'],
                    'fingerprint': row['questionSha256'], 'domainId': row['domainId'],
                    'effectiveQuestionWeight': row['effectiveQuestionWeight']})
            with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                _reference_question_identities(candidates, framework, phase)
            result = _reference_question_identities(candidates, framework, phase, source_component_aliases=aliases)
            self.assertEqual(result, [row['identity'] for row in candidates])
            wrong_scope = {key.replace('actual-aat', 'another-aat'): value for key, value in aliases.items()}
            with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                _reference_question_identities(candidates, framework, phase, source_component_aliases=wrong_scope)
            for field, value in (('fingerprint', '0' * 64), ('domainId', 'wrong-domain'), ('effectiveQuestionWeight', 9)):
                changed = deepcopy(candidates)
                changed[0][field] = value
                with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                    _reference_question_identities(changed, framework, phase, source_component_aliases=aliases)
            # An old ID and an alias pointing to a second current candidate may
            # never silently select one of two equally addressed questions.
            duplicate = deepcopy(candidates[0])
            duplicate['identity'] = (*duplicate['identity'][:2], rows[0]['sourceComponentId'])
            with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                _reference_question_identities(candidates + [duplicate], framework, phase, source_component_aliases=aliases)
            invalid = deepcopy(aliases)
            first = next(iter(invalid))
            invalid[first]['another-original-id'] = next(iter(invalid[first].values()))
            with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl'):
                _reference_question_identities(candidates, framework, phase, source_component_aliases=invalid)

    def test_migrated_reference_aliases_survive_real_zip_build_and_independent_validation(self):
        import lxp_multi_export as implementation
        baseline = self.prepare(assessment_options=self.options())
        pool = next(row for row in baseline.preview['assessmentSelection']
                    if row['prefix'] == 'DigComp' and row['phase'] == 'entry')['availableQuestions']
        source = self.fixture.metadata[0]
        source_key = json.dumps([source['originId'], source['courseId']], separators=(',', ':'))
        aliases = {'original-' + row['sourceComponentId']: row['sourceComponentId'] for row in pool}
        registry = json.loads(self.fixture.registry.read_text())
        registry['sources'][source_key]['sourceComponentAliases'] = {
            **aliases, 'removed-unused-original': 'removed-unused-current'}
        save(self.fixture.registry, registry)
        reference = json.loads((Path(__file__).resolve().parents[1] / 'static/assessment-reference-digcomp22.json').read_text())
        rows = [{'moduleNumber': '1', 'courseNumber': '1',
                 'sourceComponentId': 'original-' + row['sourceComponentId'],
                 'questionSha256': row['fingerprint'], 'domainId': row['domainId'],
                 'effectiveQuestionWeight': 1} for row in pool[:2]]
        reference['phases'] = {phase: {'expectedCount': 2, 'questions': deepcopy(rows)}
                               for phase in ('entry', 'final')}
        options = self.options()
        for phase in ('entry', 'final'):
            options['DigComp'][phase]['mode'] = 'reference'
        original_read = implementation._read

        def read_reference(path):
            return deepcopy(reference) if Path(path).name == 'assessment-reference-digcomp22.json' else original_read(path)

        with patch.object(implementation, '_read', side_effect=read_reference):
            prepared = self.prepare(assessment_options=options)
            # This executes the production ZIP builder AND its independent
            # packaged-source assessment validator, not only the preflight.
            output = build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256)
            delivered = self.fixture.root / 'delivered-alias-reference'
            with ZipFile(output) as archive:
                archive.extractall(delivered)
            contract = json.loads((delivered / 'lxp-contract.json').read_text())
            provenance = json.loads((delivered / 'package-provenance.json').read_text())
            sco = next(row for row in contract['scos'] if row['role'] == 'learning'
                       and row['source']['originId'] == source['originId'])
            self.assertEqual(sco['source']['sourceComponentAliases'], aliases)
            implementation.validate_product_contract(contract, delivered)
            original_contract, original_provenance = deepcopy(contract), deepcopy(provenance)
            for change in ('absent', 'wrong-existing-target', 'unknown-target'):
                with self.subTest(change=change):
                    contract, provenance = deepcopy(original_contract), deepcopy(original_provenance)
                    sco = next(row for row in contract['scos'] if row['id'] == sco['id'])
                    if change == 'absent':
                        del sco['source']['sourceComponentAliases']
                    else:
                        mapping = sco['source']['sourceComponentAliases']
                        first, third = list(aliases)[:3:2]
                        if change == 'wrong-existing-target':
                            mapping[first], mapping[third] = mapping[third], mapping[first]
                        else:
                            mapping[first] = 'not-in-the-actual-source'
                    report = next(row for row in provenance['courses'] if row['scoId'] == sco['id'])
                    report['source'] = deepcopy(sco['source'])
                    save(delivered / 'package-provenance.json', provenance)
                    with self.assertRaisesRegex(PackageValidationError, 'Referenzauswahl|Quellfragen-Zuordnung'):
                        implementation.validate_product_contract(contract, delivered)
