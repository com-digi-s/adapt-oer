"""Real database exceptions and authorization races must never expose a ZIP.

The database is isolated mongomock; only the failing driver call is replaced by
a genuine PyMongo exception type. Every generated build is the real exporter.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from bson import ObjectId
from pymongo.errors import OperationFailure

import test_manual_assessment_gui as fixtures


class ManualAssessmentFailureGuardsTest(unittest.TestCase):
    setUpClass = fixtures.ManualAssessmentGuiTest.__dict__['setUpClass']
    tearDownClass = fixtures.ManualAssessmentGuiTest.__dict__['tearDownClass']
    setUp = fixtures.ManualAssessmentGuiTest.setUp
    make_course = fixtures.ManualAssessmentGuiTest.make_course
    token = fixtures.ManualAssessmentGuiTest.token
    prepare = fixtures.ManualAssessmentGuiTest.prepare
    prepare_manual = fixtures.ManualAssessmentGuiTest.prepare_manual
    value = fixtures.ManualAssessmentGuiTest.value
    summary = fixtures.ManualAssessmentGuiTest.summary
    document = fixtures.ManualAssessmentGuiTest.document
    form = fixtures.ManualAssessmentGuiTest.form
    field = fixtures.ManualAssessmentGuiTest.field
    pinned_form = fixtures.ManualAssessmentGuiTest.pinned_form
    pending = fixtures.ManualAssessmentGuiTest.pending

    def capture_prepare(self, rebuilt):
        import lxp_export
        original = lxp_export.prepare_multi_framework_export

        def capture(*args, **kwargs):
            prepared = original(*args, **kwargs)
            rebuilt.append(prepared)
            self.addCleanup(prepared.close)
            return prepared

        return patch.object(lxp_export, 'prepare_multi_framework_export', side_effect=capture)

    def assert_private_failure_response(self, response, status):
        self.assertEqual(response.status_code, status, response.data.decode())
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertNotIn(b'data-manual-selection', response.data)
        self.assertNotIn(b'data-question-identity', response.data)
        self.assertNotIn(b'PRIVATE_DATABASE_DIAGNOSTIC', response.data)

    def test_transient_real_mongo_insert_failure_restores_old_preview_and_closes_new_build(self):
        location = self.prepare_manual()
        data, _, _ = self.pinned_form(location)
        old = self.value(location)
        profile_before = deepcopy(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}))
        real_insert = self.db.lxp_export_previews.insert_one
        calls, rebuilt = [], []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OperationFailure('PRIVATE_DATABASE_DIAGNOSTIC: transient insert failure')
            return real_insert(*args, **kwargs)

        with self.capture_prepare(rebuilt), patch.object(self.db.lxp_export_previews, 'insert_one', side_effect=fail_once):
            response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assert_private_failure_response(response, 503)
        self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertEqual(len(rebuilt), 1)
        self.assertTrue(rebuilt[0]._closed)
        self.assertFalse(old['prepared']._closed)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 1)
        self.assertEqual(list(self.module.app.extensions['lxp_previews']), [location.rsplit('/', 1)[-1]])
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}), profile_before)
        self.assertEqual(self.client.get(location).status_code, 200, 'Transient failure must not consume the old snapshot')

    def test_persistently_failing_insert_and_restore_do_not_leak_the_new_build_or_question_data(self):
        location = self.prepare_manual()
        data, _, _ = self.pinned_form(location)
        old = self.value(location)
        profile_before = deepcopy(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}))
        rebuilt = []
        failure = OperationFailure('PRIVATE_DATABASE_DIAGNOSTIC: writes unavailable')
        with self.capture_prepare(rebuilt), \
                patch.object(self.db.lxp_export_previews, 'insert_one', side_effect=failure), \
                patch.object(self.db.lxp_export_previews, 'update_one', side_effect=failure):
            response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
        self.assert_private_failure_response(response, 503)
        self.assertEqual(len(rebuilt), 1)
        self.assertTrue(rebuilt[0]._closed)
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}), profile_before)
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 1)
        self.assertEqual(list(self.module.app.extensions['lxp_previews']), [location.rsplit('/', 1)[-1]])
        # With restoration impossible, only the pre-existing frozen snapshot
        # remains. Its ordinary expiry still releases it; no new build survives.
        old['created'] = 0
        self.db.lxp_export_previews.update_one({'_id': location.rsplit('/', 1)[-1]}, {'$set': {'created': 0}})
        self.module._lxp_prune_previews()
        self.assertTrue(old['prepared']._closed)
        self.assertFalse(self.module.app.extensions['lxp_previews'])
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)

    def test_storage_failure_after_access_revocation_returns_no_frozen_questions_and_next_read_forgets_snapshot(self):
        location = self.prepare_manual()
        data, _, _ = self.pinned_form(location)
        old = self.value(location)
        profile_before = deepcopy(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}))
        rebuilt = []

        def revoke_and_fail(*args, **kwargs):
            self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
            raise OperationFailure('PRIVATE_DATABASE_DIAGNOSTIC: insert unavailable')

        try:
            with self.capture_prepare(rebuilt), patch.object(self.db.lxp_export_previews, 'insert_one', side_effect=revoke_and_fail):
                response = self.client.post(location.replace('/preview/', '/refine/'), data=data)
            self.assert_private_failure_response(response, 503)
            self.assertTrue(rebuilt[0]._closed)
            self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}), profile_before)
            denied = self.client.get(location)
            self.assert_private_failure_response(denied, 410)
            self.assertIsNone(self.pending(location))
            self.assertTrue(old['prepared']._closed)
        finally:
            self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': True}})

    def test_source_revocation_during_invalid_parse_is_rechecked_for_refine_and_confirm(self):
        real_settings = self.module._lxp_manual_settings
        for endpoint in ('refine', 'confirm'):
            location = self.prepare_manual()
            data, _, _ = self.pinned_form(location)
            data[next(name for name in data if name.startswith('choice_'))] = 'invalid-choice'
            old = self.value(location)
            profile_before = deepcopy(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}))

            def revoke_during_parse(value):
                self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
                return real_settings(value)

            try:
                with patch.object(self.module, '_lxp_manual_settings', side_effect=revoke_during_parse):
                    response = self.client.post(location.replace('/preview/', '/' + endpoint + '/'), data=data)
                with self.subTest(endpoint=endpoint):
                    self.assert_private_failure_response(response, 410)
                    self.assertIsNone(self.pending(location))
                    self.assertTrue(old['prepared']._closed)
                    self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}), profile_before)
            finally:
                self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': True}})

    def test_profile_revision_race_during_invalid_parse_is_rechecked_for_refine_and_confirm(self):
        real_settings = self.module._lxp_manual_settings
        for endpoint in ('refine', 'confirm'):
            location = self.prepare_manual()
            data, _, _ = self.pinned_form(location)
            data[next(name for name in data if name.startswith('choice_'))] = 'invalid-choice'
            old = self.value(location)
            external_profiles = []

            def external_export_during_parse(value):
                self.db.lxp_export_profiles.update_one({'_id': old['profile_id']},
                    {'$inc': {'revision': 1}, '$set': {'title': 'Anderer bestätigter Export'}})
                external_profiles.append(deepcopy(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']})))
                return real_settings(value)

            with patch.object(self.module, '_lxp_manual_settings', side_effect=external_export_during_parse):
                response = self.client.post(location.replace('/preview/', '/' + endpoint + '/'), data=data)
            with self.subTest(endpoint=endpoint):
                self.assert_private_failure_response(response, 410)
                self.assertIsNone(self.pending(location))
                self.assertTrue(old['prepared']._closed)
                self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}), external_profiles[0])

    def test_legacy_unknown_confirmation_field_stays_a_normal_400_without_build_or_profile_write(self):
        import lxp_export
        location = self.prepare_manual()
        complete = self.form(location)
        data = {name: complete[name] for name in ('csrf_token', 'preflight_sha256')}
        data['legacy_unknown_field'] = 'not accepted'
        old = self.value(location)
        profile_before = deepcopy(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}))
        with patch.object(lxp_export, 'build_multi_framework_export') as build, \
                patch.object(lxp_export, 'prepare_multi_framework_export') as prepare:
            response = self.client.post(location.replace('/preview/', '/confirm/'), data=data)
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertIn(b'Unbekannte', response.data)
        self.assertEqual(self.pending(location)['state'], 'pending')
        self.assertFalse(old['prepared']._closed)
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': old['profile_id']}), profile_before)
        build.assert_not_called()
        prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
