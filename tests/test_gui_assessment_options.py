"""Real GUI/profile/preflight gates for bounded author-configured diagnostics."""
from io import BytesIO
import json
import unittest
from zipfile import ZipFile

from bs4 import BeautifulSoup

import test_gui_multi_framework as fixtures
import test_gui_catalogue_defaults as catalogue_fixtures


class GuiAssessmentOptionsTest(unittest.TestCase):
    setUpClass = fixtures.MultiFrameworkGuiTest.__dict__['setUpClass']
    tearDownClass = fixtures.MultiFrameworkGuiTest.__dict__['tearDownClass']
    setUp = catalogue_fixtures.GuiCatalogueDefaultsTest.setUp
    make_course = fixtures.MultiFrameworkGuiTest.make_course
    token = fixtures.MultiFrameworkGuiTest.token
    prepare = fixtures.MultiFrameworkGuiTest.prepare
    confirm = fixtures.MultiFrameworkGuiTest.confirm

    def document(self, response=None):
        return BeautifulSoup((response or self.client.get('/lxp/export')).data, 'html.parser')

    def test_new_form_has_bounded_per_phase_defaults_and_original_marking_controls(self):
        doc = self.document()
        for prefix, phase, count in [('DigComp', 'entry', '24'), ('DigComp', 'final', '39'),
                                     ('AILit', 'entry', '12'), ('AILit', 'final', '12')]:
            name = f'assessment_{prefix}_{phase}_'
            self.assertEqual(doc.select_one(f'input[name="{name}present"]')['value'], 'yes')
            self.assertEqual(doc.select_one(f'input[name="{name}questionCount"]')['value'], count)
            self.assertEqual(doc.select_one(f'select[name="{name}mode"] option[selected]')['value'], 'balanced')
            self.assertTrue(doc.select_one(f'input[name="{name}equalSampling"]').has_attr('checked'))
            self.assertTrue(doc.select_one(f'input[name="{name}showMarking"]').has_attr('checked'))
            self.assertFalse(doc.select_one(f'input[name="{name}showModelAnswer"]').has_attr('checked'))
        self.assertIsNotNone(doc.select_one('select[name="assessment_DigComp_entry_mode"] option[value="reference"]'))
        self.assertIsNone(doc.select_one('select[name="assessment_AILit_entry_mode"] option[value="reference"]'))

    def test_confirmed_preview_saves_typed_options_and_exports_only_the_selected_questions(self):
        location = self.prepare(assessment_AILit_entry_present='yes', assessment_AILit_entry_mode='balanced',
            assessment_AILit_entry_questionCount='1', assessment_AILit_entry_showModelAnswer='yes',
            assessment_AILit_final_present='yes', assessment_AILit_final_mode='balanced',
            assessment_AILit_final_questionCount='2', assessment_AILit_final_equalSampling='yes',
            assessment_AILit_final_showMarking='yes')
        preview = self.client.get(location)
        self.assertEqual(preview.status_code, 200)
        doc = self.document(preview)
        entry = doc.select_one('[data-assessment-selection="fw-ailit-2026-entry"]')
        self.assertIsNotNone(entry)
        self.assertIn('1 von 3 verfügbaren Fragen ausgewählt', entry.get_text())
        self.assertIn('Nicht geprüft', entry.get_text())
        self.assertEqual(len(entry.select('details li')), 1)
        response = self.confirm(location)
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
        profile = self.db.lxp_export_profiles.find_one()
        options = profile['settings']['assessment_options']['AILit']
        self.assertEqual(options['entry'], {'mode': 'balanced', 'questionCount': 1,
            'equalSampling': False, 'showMarking': False, 'showModelAnswer': True})
        self.assertEqual(options['final'], {'mode': 'balanced', 'questionCount': 2,
            'equalSampling': True, 'showMarking': True, 'showModelAnswer': False})
        for assessment in contract['assessments']:
            if assessment['frameworkId'] == 'fw-ailit-2026':
                self.assertEqual(sum(len(bank['questionIds']) for bank in assessment['banks']),
                                 1 if assessment['phase'] == 'entry' else 2)
        # Hidden marker absent: existing scripts/partial POSTs preserve the
        # explicit saved choice rather than turning every checkbox back on.
        location = self.prepare(profile_id=profile['_id'])
        resumed = self.module.app.extensions['lxp_previews'][location.rsplit('/', 1)[-1]]
        self.assertEqual(resumed['settings']['assessment_options']['AILit'], options)
        response = self.confirm(location)
        self.assertEqual(response.status_code, 200)
        doc = self.document(self.client.get('/lxp/export', query_string={'profile_id': profile['_id']}))
        self.assertEqual(doc.select_one('input[name="assessment_AILit_entry_questionCount"]')['value'], '1')
        self.assertFalse(doc.select_one('input[name="assessment_AILit_entry_showMarking"]').has_attr('checked'))
        self.assertTrue(doc.select_one('input[name="assessment_AILit_entry_showModelAnswer"]').has_attr('checked'))

    def test_all_mode_is_explicit_and_saved_instead_of_an_implicit_unlimited_default(self):
        location = self.prepare(assessment_AILit_entry_mode='all', assessment_AILit_entry_questionCount='')
        value = self.module.app.extensions['lxp_previews'][location.rsplit('/', 1)[-1]]
        self.assertIsNone(value['settings']['assessment_options']['AILit']['entry']['questionCount'])
        self.assertEqual(self.confirm(location).status_code, 200)
        profile = self.db.lxp_export_profiles.find_one()
        doc = self.document(self.client.get('/lxp/export', query_string={'profile_id': profile['_id']}))
        self.assertEqual(doc.select_one('select[name="assessment_AILit_entry_mode"] option[selected]')['value'], 'all')
        self.assertEqual(doc.select_one('input[name="assessment_AILit_entry_questionCount"]')['value'], '')

    def test_invalid_controls_fail_before_preflight_or_profile_creation(self):
        invalid = [
            {'assessment_AILit_entry_questionCount': '0'},
            {'assessment_AILit_entry_questionCount': '-1'},
            {'assessment_AILit_entry_questionCount': '1.5'},
            {'assessment_AILit_entry_questionCount': 'yes'},
            {'assessment_AILit_entry_questionCount': ['1', '2']},
            {'assessment_AILit_entry_mode': 'random-unbound'},
            {'assessment_AILit_entry_mode': 'reference'},
            {'assessment_AILit_entry_showMarking': 'perhaps'},
            {'assessment_AILit_entry_showMarking': ['yes', 'no']},
            {'assessment_AILit_entry_showModelAnswer': 'true'},
            {'assessment_AILit_entry_present': 'no'},
            {'assessment_AILit_entry_unrecognized': 'yes'},
            {'assessment_Unknown_entry_mode': 'all'},
            {'assessment_AILit_unknown_mode': 'all'},
        ]
        for extra in invalid:
            with self.subTest(extra=extra):
                response = self.client.post('/lxp/export', data={'csrf_token': self.token(),
                    'title': 'Blocked test configuration', 'course_id': [self.dig, self.ai],
                    'required_course': [self.dig, self.ai], **extra})
                self.assertEqual(response.status_code, 200)
                self.assertIn('LXP-Export gestoppt', response.data.decode())
                self.assertEqual(self.db.lxp_export_profiles.count_documents({}), 0)
                self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)

    def test_switching_from_saved_all_to_reference_does_not_require_an_unused_count(self):
        profile = {'settings': {'assessment_options': {'DigComp': {
            phase: {'mode': 'all', 'questionCount': None, 'equalSampling': True,
                    'showMarking': True, 'showModelAnswer': False}
            for phase in ('entry', 'final')}}}}
        data = {f'assessment_DigComp_{phase}_{key}': value for phase in ('entry', 'final')
            for key, value in (('present', 'yes'), ('mode', 'reference'), ('questionCount', ''))}
        with self.module.app.test_request_context('/lxp/export', method='POST', data=data):
            options = self.module._lxp_assessment_options(self.catalogue['frameworks'], profile)
        self.assertEqual(options['DigComp']['entry']['questionCount'], 24)
        self.assertEqual(options['DigComp']['final']['questionCount'], 39)
        with self.module.app.test_request_context('/lxp/export', method='POST', data={
                'assessment_DigComp_entry_mode': 'balanced'}):
            with self.assertRaisesRegex(ValueError, 'positive ganze Fragenanzahl'):
                self.module._lxp_assessment_options(self.catalogue['frameworks'], profile)

    def test_settings_change_invalidates_an_existing_frozen_preview(self):
        location = self.prepare(assessment_AILit_entry_questionCount='1')
        token = location.rsplit('/', 1)[-1]
        value = self.module.app.extensions['lxp_previews'][token]
        value['settings']['assessment_options']['AILit']['entry']['questionCount'] = 2
        response = self.confirm(location)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertEqual(self.db.lxp_export_profiles.find_one()['revision'], 0)


if __name__ == '__main__':
    unittest.main()
