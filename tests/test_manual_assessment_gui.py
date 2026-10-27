"""Manual diagnostics through real HTML, owned previews, profiles and ZIPs.

Small native questions and a contract-wrapper fixture prove server-side GUI
integration, not a real browser/LMS runtime or psychometric representativeness.
"""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from bson import ObjectId
from bs4 import BeautifulSoup
from pymongo.results import DeleteResult
from werkzeug.datastructures import MultiDict

from lxp_catalogue import canonical_bytes
from lxp_multi_export import tree_manifest
import test_gui_multi_framework as fixtures
import test_gui_catalogue_defaults as catalogue_fixtures


class ManualAssessmentGuiTest(unittest.TestCase):
    setUpClass = fixtures.MultiFrameworkGuiTest.__dict__['setUpClass']
    tearDownClass = fixtures.MultiFrameworkGuiTest.__dict__['tearDownClass']
    setUp = catalogue_fixtures.GuiCatalogueDefaultsTest.setUp
    make_course = fixtures.MultiFrameworkGuiTest.make_course
    token = fixtures.MultiFrameworkGuiTest.token
    prepare = fixtures.MultiFrameworkGuiTest.prepare
    confirm = fixtures.MultiFrameworkGuiTest.confirm

    def prepare_manual(self, **extra):
        options = {'assessment_AILit_entry_questionCount': '1', 'assessment_AILit_final_questionCount': '2'}
        options.update(extra)
        return self.prepare(**options)

    def value(self, location):
        return self.module.app.extensions['lxp_previews'][location.rsplit('/', 1)[-1]]

    def summary(self, location, phase='entry'):
        return next(row for row in self.value(location)['prepared']._assessment_selection
                    if row['frameworkId'] == 'fw-ailit-2026' and row['phase'] == phase)

    def document(self, location):
        response = self.client.get(location)
        self.assertEqual(response.status_code, 200, response.data.decode())
        return BeautifulSoup(response.data, 'html.parser')

    def form(self, location):
        form = self.document(location).select_one('#lxp-preview-form')
        self.assertIsNotNone(form)
        result = {}
        for element in form.select('input[name], select[name], textarea[name]'):
            if element.name == 'select':
                selected = element.select_one('option[selected]') or element.select_one('option')
                value = selected.get('value', selected.text)
            elif element.name == 'textarea':
                value = element.text
            else:
                if element.get('type') == 'checkbox' and not element.has_attr('checked'):
                    continue
                value = element.get('value', '')
            self.assertNotIn(element['name'], result)
            result[element['name']] = value
        return result

    def field(self, row, phase='entry', framework='fw-ailit-2026'):
        digest = hashlib.sha256(canonical_bytes(row['identity'])).hexdigest()
        return 'choice_' + framework + '_' + phase + '_' + digest

    def pinned_form(self, location):
        form = self.form(location)
        pool = self.summary(location)['availableQuestions']
        include = next(row for row in pool if not row['selected'])
        exclude = next(row for row in pool if row['selected'])
        form[self.field(include)] = 'include'
        form[self.field(exclude)] = 'exclude'
        return form, include, exclude

    def refine(self, location, form=None):
        response = self.client.post(location.replace('/preview/', '/refine/'), data=form or self.form(location))
        self.assertEqual(response.status_code, 302, response.data.decode())
        self.assertIn('/lxp/preview/', response.location)
        self.assertNotEqual(response.location, location)
        return response.location

    def confirm_full(self, location):
        return self.client.post(location.replace('/preview/', '/confirm/'), data=self.form(location))

    def archive(self, response):
        self.assertEqual(response.status_code, 200, response.data[:1000])
        self.assertEqual(response.mimetype, 'application/zip')
        return ZipFile(BytesIO(response.data))

    def assessment_options(self, settings, phase='entry'):
        values = settings['assessment_options']
        return values.get('fw-ailit-2026', values.get('AILit', {}))[phase]

    def pending(self, location):
        return self.db.lxp_export_previews.find_one({'_id': location.rsplit('/', 1)[-1]})

    def test_single_html_form_has_every_pool_control_marker_and_opaque_identity_name(self):
        location = self.prepare_manual()
        document = self.document(location)
        forms = document.select('form')
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form.get('id'), 'lxp-preview-form')
        self.assertEqual(form.get('action'), location.replace('/preview/', '/confirm/'))
        self.assertTrue(form.select_one('button').has_attr('data-confirm-lxp'))
        self.assertEqual(form.select_one('[data-apply-manual-selection]')['formaction'],
                         location.replace('/preview/', '/refine/'))
        for selection in self.value(location)['prepared']._assessment_selection:
            stem = selection['frameworkId'] + '_' + selection['phase']
            self.assertEqual(form.select_one('[name="manual_' + stem + '"]')['value'], 'yes')
            self.assertEqual(len(form.select('select[name^="choice_' + stem + '_"]')), selection['poolCount'])
            for row in selection['availableQuestions']:
                name = self.field(row, selection['phase'], selection['frameworkId'])
                element = form.select_one('[name="' + name + '"]')
                self.assertIsNotNone(element)
                self.assertEqual(json.loads(element['data-question-identity']), row['identity'])
                self.assertEqual(element['data-question-fingerprint'], row['fingerprint'])
        self.assertEqual(len(form.select('select[name^="choice_fw-ailit-2026_entry_"]')), 3)

    def test_include_exclude_creates_new_frozen_preview_and_native_zip_without_source_changes(self):
        location = self.prepare_manual()
        original = self.value(location)
        old_hash = original['prepared'].preflight_sha256
        profile_id = original['profile_id']
        manifests = {cid: tree_manifest(self.root / 'builds' / cid / 'build') for cid in (self.dig, self.ai)}
        form, include, exclude = self.pinned_form(location)
        updated = self.refine(location, form)
        summary = self.summary(updated)
        self.assertNotEqual(self.value(updated)['prepared'].preflight_sha256, old_hash)
        self.assertEqual((summary['poolCount'], summary['selectedCount'], summary['eligibleCount'],
                          summary['includedCount'], summary['excludedCount']), (3, 1, 2, 1, 1))
        self.assertEqual([row['identity'] for row in summary['questions']], [include['identity']])
        self.assertEqual(self.summary(updated, 'final')['selectedCount'], 2)
        self.assertNotIn('includeQuestions', self.summary(updated, 'final')['options'])
        self.assertEqual(self.client.get(location).status_code, 410)
        self.assertIsNone(self.pending(location))
        self.assertEqual(self.client.post(location.replace('/preview/', '/confirm/'), data={
            'csrf_token': self.token(), 'preflight_sha256': old_hash}).status_code, 410)
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': profile_id})['revision'], 0)
        with self.archive(self.confirm_full(updated)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            assessment = next(row for row in contract['assessments']
                              if row['frameworkId'] == 'fw-ailit-2026' and row['phase'] == 'entry')
            self.assertEqual(sum(len(row['questionIds']) for row in assessment['banks']), 1)
            sco = next(row for row in contract['scos'] if row['id'] == assessment['scoId'])
            self.assertEqual([row['sourceComponentId'] for row in sco['source']['questions']], [include['sourceComponentId']])
            generated = json.loads(archive.read(sco['launch']['root'] + '/course/en/components.json'))
            native = [row for row in generated if row.get('_component') == 'mcq']
            self.assertEqual(len(native), 1)
            self.assertTrue(native[0]['_items'][0]['_shouldBeSelected'])
            self.assertNotIn(exclude['sourceComponentId'], [row['sourceComponentId'] for row in sco['source']['questions']])
        saved = self.assessment_options(self.db.lxp_export_profiles.find_one({'_id': profile_id})['settings'])
        self.assertEqual(saved['includeQuestions'], [{'identity': include['identity'], 'fingerprint': include['fingerprint']}])
        self.assertEqual(saved['excludeQuestions'], [{'identity': exclude['identity'], 'fingerprint': exclude['fingerprint']}])
        for cid, manifest in manifests.items():
            self.assertEqual(tree_manifest(self.root / 'builds' / cid / 'build'), manifest)

    def test_unchanged_full_form_and_legacy_hash_only_confirm_both_produce_zip(self):
        for legacy in (False, True):
            location = self.prepare_manual()
            response = self.confirm(location) if legacy else self.confirm_full(location)
            with self.archive(response) as archive:
                selection = json.loads(archive.read('assessment-selection.json'))
                self.assertTrue(all('includeQuestions' not in row['options'] and 'excludeQuestions' not in row['options']
                                    for row in selection))

    def test_unsaved_manual_edits_sent_directly_to_confirm_are_refined_not_silently_exported(self):
        location = self.prepare_manual()
        profile_id = self.value(location)['profile_id']
        form, include, _ = self.pinned_form(location)
        response = self.client.post(location.replace('/preview/', '/confirm/'), data=form)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertIn('/lxp/preview/', response.location)
        self.assertNotEqual(response.location, location)
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': profile_id})['revision'], 0)
        self.assertEqual([row['identity'] for row in self.summary(response.location)['questions']], [include['identity']])
        with self.archive(self.confirm_full(response.location)):
            pass

    def test_unknown_duplicate_missing_or_forged_fields_do_not_consume_old_preview(self):
        location = self.prepare_manual()
        original = self.form(location)
        choice = next(key for key in original if key.startswith('choice_'))
        marker = next(key for key in original if key.startswith('manual_'))
        count = 'count_fw-ailit-2026_entry'
        cases = [MultiDict(dict(original, unknown='value')), MultiDict(dict(original, profile_id='another-profile')),
                 MultiDict(dict(original, **{choice: 'force'})),
                 MultiDict(dict(original, **{marker: 'no'})),
                 MultiDict(dict(original, choice_foreign_entry_000000='include')),
                 MultiDict(dict(original, **{count: '0'})),
                 MultiDict(dict(original, preflight_sha256='0' * 64))]
        missing = MultiDict(original)
        missing.pop(marker)
        cases.append(missing)
        for name in ('csrf_token', 'preflight_sha256', choice, marker, count):
            duplicated = MultiDict(original)
            duplicated.add(name, original[name])
            cases.append(duplicated)
        for data in cases:
            with self.subTest(fields=list(data.keys())):
                response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
                self.assertEqual(response.status_code, 400)
                self.assertNotEqual(response.mimetype, 'application/zip')
                self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertEqual(self.client.get(location).status_code, 200)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 1)

    def test_invalid_input_preserves_choices_count_and_disables_confirmation(self):
        location = self.prepare_manual()
        data, include, _ = self.pinned_form(location)
        data['count_fw-ailit-2026_entry'] = '0'
        response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 400)
        document = BeautifulSoup(response.data, 'html.parser')
        self.assertEqual(document.select_one('[name="count_fw-ailit-2026_entry"]')['value'], '0')
        self.assertEqual(document.select_one('[name="' + self.field(include) + '"] option[selected]')['value'], 'include')
        self.assertTrue(document.select_one('[data-confirm-lxp]').has_attr('disabled'))
        self.assertEqual(self.pending(location)['state'], 'pending')
        # Directly bypassing the disabled button must be equally safe.
        self.assertEqual(self.client.post(location.replace('/preview/', '/confirm/'), data=data).status_code, 400)
        self.assertEqual(self.pending(location)['state'], 'pending')

    def test_failed_reprepare_keeps_original_token_and_frozen_selection(self):
        location = self.prepare_manual()
        old_hash = self.value(location)['prepared'].preflight_sha256
        data = self.form(location)
        for row in self.summary(location)['availableQuestions']:
            data[self.field(row)] = 'include'
        response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertEqual(self.value(location)['prepared'].preflight_sha256, old_hash)
        self.assertNotIn('includeQuestions', self.summary(location)['options'])
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 1)
        self.assertEqual(self.client.get(location).status_code, 200)

    def test_refine_rejects_changed_source_or_revoked_access_before_form_validation(self):
        path = self.root / 'builds' / self.ai / 'build/course/de/components.json'
        original = json.loads(path.read_text())
        for change in ('body', 'access'):
            location = self.prepare_manual()
            data = self.form(location)
            data['unknown'] = 'invalid even before parsing'
            try:
                if change == 'body':
                    changed = deepcopy(original)
                    changed[0]['body'] = '<p>Geänderte veröffentlichte Frage</p>'
                    fixtures.save(path, changed)
                else:
                    self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
                response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
                self.assertEqual(response.status_code, 410)
                self.assertNotIn('data-manual-selection', response.data.decode())
                self.assertIsNone(self.pending(location))
            finally:
                fixtures.save(path, original)
                self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': True}})

    def test_revoked_access_cannot_be_reexposed_by_invalid_manual_confirm(self):
        location = self.prepare_manual()
        data = self.form(location)
        data['unknown'] = 'must not render cached private questions'
        self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
        response = self.client.post(location.replace('/preview/', '/confirm/'), data=data)
        self.assertEqual(response.status_code, 410)
        self.assertNotIn('data-manual-selection', response.data.decode())
        self.assertIsNone(self.pending(location))

    def test_csrf_authentication_and_preview_ownership_are_enforced_without_profile_writes(self):
        location = self.prepare_manual()
        data = self.form(location)
        token = location.rsplit('/', 1)[-1]
        for csrf in ('', 'not-a-token'):
            self.assertEqual(self.client.post(location.replace('/preview/', '/refine/'),
                data=dict(data, csrf_token=csrf)).status_code, 400)
            self.assertEqual(self.pending(location)['state'], 'pending')
        self.db.lxp_export_previews.update_one({'_id': token}, {'$set': {'owner': 'another-admin'}})
        response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 410)
        self.assertNotIn('data-manual-selection', response.data.decode())
        self.db.lxp_export_previews.update_one({'_id': token}, {'$set': {'owner': '1'}})
        anonymous = self.module.app.test_client()
        response = anonymous.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)
        self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertTrue(all(profile['revision'] == 0 for profile in self.db.lxp_export_profiles.find()))

    def test_profile_revision_conflict_blocks_refinement_and_forgets_stale_preview(self):
        location = self.prepare_manual()
        profile_id = self.value(location)['profile_id']
        data = self.form(location)
        self.db.lxp_export_profiles.update_one({'_id': profile_id}, {'$set': {'revision': 1}})
        response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 410)
        self.assertIsNone(self.pending(location))
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)

    def test_refined_preview_reconstructs_after_worker_restart_with_exact_hash_and_choices(self):
        location = self.prepare_manual()
        data, include, _ = self.pinned_form(location)
        updated = self.refine(location, data)
        before = self.value(updated)['prepared'].preflight_sha256
        self.module.app.extensions['lxp_previews'].pop(updated.rsplit('/', 1)[-1])['prepared'].close()
        document = self.document(updated)
        self.assertEqual(self.value(updated)['prepared'].preflight_sha256, before)
        self.assertEqual(document.select_one('[name="' + self.field(include) + '"] option[selected]')['value'], 'include')
        self.assertEqual([row['identity'] for row in self.summary(updated)['questions']], [include['identity']])
        with self.archive(self.confirm_full(updated)):
            pass
        self.assertEqual(self.client.get(updated).status_code, 410)

    def test_saved_manual_profile_reload_is_preserved_and_new_offers_are_isolated(self):
        location = self.prepare_manual()
        data, include, _ = self.pinned_form(location)
        updated = self.refine(location, data)
        profile_id = self.value(updated)['profile_id']
        with self.archive(self.confirm_full(updated)):
            pass
        saved = deepcopy(self.db.lxp_export_profiles.find_one({'_id': profile_id}))
        resumed = self.prepare_manual(profile_id=profile_id)
        self.assertEqual(self.assessment_options(self.value(resumed)['settings']), self.assessment_options(saved['settings']))
        self.assertEqual([row['identity'] for row in self.summary(resumed)['questions']], [include['identity']])
        self.assertEqual(self.form(resumed)[self.field(include)], 'include')
        separate = self.prepare_manual()
        self.assertNotEqual(self.value(separate)['profile_id'], profile_id)
        self.assertNotIn('includeQuestions', self.summary(separate)['options'])
        self.assertTrue(all(row['manualChoice'] == 'auto' for row in self.summary(separate)['availableQuestions']))
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': profile_id}), saved)

    def test_changed_saved_question_blocks_reuse_until_explicit_reset_without_losing_other_options(self):
        location = self.prepare_manual(assessment_AILit_entry_present='yes',
            assessment_AILit_entry_showModelAnswer='yes')
        data, include, _ = self.pinned_form(location)
        updated = self.refine(location, data)
        profile_id = self.value(updated)['profile_id']
        with self.archive(self.confirm_full(updated)):
            pass
        saved = deepcopy(self.db.lxp_export_profiles.find_one({'_id': profile_id}))
        old_options = self.assessment_options(saved['settings'])
        path = self.root / 'builds' / self.ai / 'build/course/de/components.json'
        questions = json.loads(path.read_text())
        next(row for row in questions if row['_id'] == include['sourceComponentId'])['body'] = '<p>Eine fachlich überarbeitete Frage</p>'
        fixtures.save(path, questions)
        response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Überarbeitetes Angebot',
            'profile_id': profile_id, 'course_id': [self.dig, self.ai], 'required_course': [self.dig, self.ai]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('LXP-Export gestoppt', response.data.decode())
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': profile_id}), saved)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)
        reset = self.prepare(profile_id=profile_id, reset_manual_selection='yes')
        options = self.assessment_options(self.value(reset)['settings'])
        self.assertEqual(options, {key: value for key, value in old_options.items()
                                   if key not in ('includeQuestions', 'excludeQuestions')})
        with self.archive(self.confirm_full(reset)):
            pass
        self.assertNotIn('includeQuestions', self.assessment_options(
            self.db.lxp_export_profiles.find_one({'_id': profile_id})['settings']))
        self.assertTrue(all(row['_classes'] == 'et at' for row in json.loads(path.read_text())))

    def test_all_mode_exclusion_keeps_original_pool_coverage_and_exports_two_native_questions(self):
        location = self.prepare_manual(assessment_AILit_entry_mode='all', assessment_AILit_entry_questionCount='')
        data = self.form(location)
        self.assertNotIn('count_fw-ailit-2026_entry', data)
        excluded = self.summary(location)['availableQuestions'][0]
        data[self.field(excluded)] = 'exclude'
        updated = self.refine(location, data)
        summary = self.summary(updated)
        self.assertEqual((summary['poolCount'], summary['selectedCount'], summary['eligibleCount']), (3, 2, 2))
        self.assertIsNone(summary['options']['questionCount'])
        self.assertEqual(sum(row['poolCount'] for row in summary['coverage']['domains']), 3)
        with self.archive(self.confirm_full(updated)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            assessment = next(row for row in contract['assessments']
                              if row['frameworkId'] == 'fw-ailit-2026' and row['phase'] == 'entry')
            self.assertEqual(sum(len(bank['questionIds']) for bank in assessment['banks']), 2)

    def test_hash_only_legacy_confirm_rejects_unknown_or_duplicate_fields_without_consuming_token(self):
        location = self.prepare_manual()
        complete = self.form(location)
        original = {name: complete[name] for name in ('csrf_token', 'preflight_sha256')}
        cases = [MultiDict(dict(original, unknown='not-an-allowed-legacy-control'))]
        for name in original:
            duplicated = MultiDict(original)
            duplicated.add(name, original[name])
            cases.append(duplicated)
        for data in cases:
            response = self.client.post(location.replace('/preview/', '/confirm/'), data=data)
            self.assertEqual(response.status_code, 400)
            self.assertNotEqual(response.mimetype, 'application/zip')
            self.assertEqual(self.pending(location)['state'], 'pending')

    def test_unmarked_and_other_phase_questions_cannot_be_injected_into_the_entry_pool(self):
        path = self.root / 'builds' / self.ai / 'build/course/de/components.json'
        questions = json.loads(path.read_text())
        unmarked = dict(deepcopy(questions[1]), _id='unmarked-question', _classes='')
        final_only = dict(deepcopy(questions[2]), _id='final-only-question', _classes='at')
        fixtures.save(path, [*questions, unmarked, final_only])
        location = self.prepare_manual()
        self.assertEqual(self.summary(location)['poolCount'], 3)
        self.assertEqual(self.summary(location, 'final')['poolCount'], 4)
        original = self.form(location)
        for question in (unmarked, final_only):
            identity = {'identity': ['gui-tenant', self.ai, question['_id']]}
            forged = dict(original)
            forged[self.field(identity)] = 'include'
            response = self.client.post(location.replace('/preview/', '/refine/'), data=forged)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertEqual(json.loads(path.read_text()), [*questions, unmarked, final_only])

    def test_non_browser_and_oversized_preview_posts_are_rejected_before_csrf_or_export(self):
        location = self.prepare_manual()
        data = self.form(location)
        endpoint = location.replace('/preview/', '/refine/')
        for response in (self.client.post(endpoint, json=data),
                self.client.post(endpoint, data=dict(data, unknown='x' * (512 * 1024))),
                self.client.post(endpoint, data=data, content_type='multipart/form-data')):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.pending(location)['state'], 'pending')

    def test_source_access_and_profile_races_during_reprepare_discard_both_previews(self):
        import lxp_export
        real_prepare = lxp_export.prepare_multi_framework_export
        path = self.root / 'builds' / self.ai / 'build/course/de/components.json'
        original = json.loads(path.read_text())
        for change in ('body', 'access', 'profile'):
            location = self.prepare_manual()
            data, _, _ = self.pinned_form(location)
            old = self.value(location)
            rebuilt = []

            def change_during_reprepare(*args, **kwargs):
                prepared = real_prepare(*args, **kwargs)
                rebuilt.append(prepared)
                if change == 'body':
                    questions = deepcopy(original)
                    questions[0]['body'] = '<p>Während der Verarbeitung überarbeitet</p>'
                    fixtures.save(path, questions)
                elif change == 'access':
                    self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
                else:
                    self.db.lxp_export_profiles.update_one({'_id': old['profile_id']}, {'$inc': {'revision': 1}})
                return prepared

            try:
                with patch.object(lxp_export, 'prepare_multi_framework_export', side_effect=change_during_reprepare):
                    response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
                with self.subTest(change=change):
                    self.assertEqual(response.status_code, 410)
                    self.assertNotIn('data-manual-selection', response.data.decode())
                    self.assertIsNone(self.pending(location))
                    self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)
                    self.assertTrue(old['prepared']._closed)
                    self.assertEqual(len(rebuilt), 1)
                    self.assertTrue(rebuilt[0]._closed)
            finally:
                fixtures.save(path, original)
                self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': True}})

    def test_failed_refine_claim_never_stores_a_second_preview(self):
        location = self.prepare_manual()
        data, _, _ = self.pinned_form(location)
        old = self.value(location)
        with patch.object(self.db.lxp_export_previews, 'find_one_and_update', return_value=None):
            response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 1)
        self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertFalse(old['prepared']._closed)
        self.assertNotIn('includeQuestions', self.summary(location)['options'])

    def test_failed_old_token_supersede_cas_cleans_up_the_unclaimed_replacement(self):
        location = self.prepare_manual()
        data, _, _ = self.pinned_form(location)
        old = self.value(location)
        old_token = location.rsplit('/', 1)[-1]
        real_delete = self.db.lxp_export_previews.delete_one
        real_store = self.module._lxp_store_preview

        def fail_supersede(query, *args, **kwargs):
            if query.get('_id') == old_token and query.get('state') == 'refining':
                return DeleteResult({'n': 0}, True)
            return real_delete(query, *args, **kwargs)

        with patch.object(self.db.lxp_export_previews, 'delete_one', side_effect=fail_supersede), \
                patch.object(self.module, '_lxp_store_preview', wraps=real_store) as store:
            response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(store.call_count, 1)
        self.assertTrue(store.call_args.args[0]._closed)
        self.assertTrue(old['prepared']._closed)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)
        self.assertFalse(self.module.app.extensions['lxp_previews'])

    def test_new_preview_storage_failure_restores_old_pending_preview_and_releases_new_build(self):
        import lxp_export
        location = self.prepare_manual()
        data, _, _ = self.pinned_form(location)
        old = self.value(location)
        real_prepare = lxp_export.prepare_multi_framework_export
        rebuilt = []

        def capture_prepare(*args, **kwargs):
            prepared = real_prepare(*args, **kwargs)
            rebuilt.append(prepared)
            return prepared

        with patch.object(lxp_export, 'prepare_multi_framework_export', side_effect=capture_prepare), \
                patch.object(self.module, '_lxp_store_preview', side_effect=OSError('isolated storage failure')):
            response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertFalse(old['prepared']._closed)
        self.assertEqual(len(rebuilt), 1)
        self.assertTrue(rebuilt[0]._closed)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 1)

    def test_access_or_profile_change_during_final_zip_build_prevents_download_and_profile_overwrite(self):
        import lxp_export
        real_build = lxp_export.build_multi_framework_export
        for change in ('access', 'profile'):
            location = self.prepare_manual()
            old = self.value(location)
            data = self.form(location)

            def change_after_build(*args, **kwargs):
                stream = real_build(*args, **kwargs)
                if change == 'access':
                    self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
                else:
                    self.db.lxp_export_profiles.update_one({'_id': old['profile_id']},
                        {'$set': {'revision': 1, 'title': 'Anderer bestätigter Export'}})
                return stream

            try:
                with patch.object(lxp_export, 'build_multi_framework_export', side_effect=change_after_build):
                    response = self.client.post(location.replace('/preview/', '/confirm/'), data=data)
                self.assertNotEqual(response.mimetype, 'application/zip')
                self.assertEqual(response.status_code, 302)
                profile = self.db.lxp_export_profiles.find_one({'_id': old['profile_id']})
                self.assertEqual(profile['revision'], 0 if change == 'access' else 1)
                if change == 'profile':
                    self.assertEqual(profile['title'], 'Anderer bestätigter Export')
                self.assertIsNone(self.pending(location))
                self.assertTrue(old['prepared']._closed)
            finally:
                self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': True}})


if __name__ == '__main__':
    unittest.main()
