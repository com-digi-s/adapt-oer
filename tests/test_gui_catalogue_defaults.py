"""Real Flask/export behavior for catalogue-driven, explicit diagnostic intent."""
from io import BytesIO
import json
import re
import unittest
from zipfile import ZipFile

from lxp_catalogue import load_catalogue
import test_gui_multi_framework as fixtures
import test_catalogue_diagnostic_defaults as native_fixtures


class GuiCatalogueDefaultsTest(unittest.TestCase):
    setUpClass = fixtures.MultiFrameworkGuiTest.__dict__['setUpClass']
    tearDownClass = fixtures.MultiFrameworkGuiTest.__dict__['tearDownClass']
    make_course = fixtures.MultiFrameworkGuiTest.make_course
    token = fixtures.MultiFrameworkGuiTest.token
    prepare = fixtures.MultiFrameworkGuiTest.prepare
    confirm = fixtures.MultiFrameworkGuiTest.confirm

    def setUp(self):
        fixtures.MultiFrameworkGuiTest.setUp(self)
        self.catalogue = load_catalogue()
        self.module.app.config['LXP_CATALOGUE'] = self.catalogue
        ai_build = self.root / 'builds' / self.ai / 'build'
        fixtures.save(ai_build / 'course/de/components.json', [{'_id': 'native-question', '_component': 'mcq',
            'body': 'Explicitly authored native question', '_items': [{'text': 'Yes', '_shouldBeSelected': True}, {'text': 'No'}]}])
        native_fixtures.hierarchical_questions(ai_build)

    def selected(self, html, prefix, phase):
        select = re.search(r'<select name="test_' + prefix + '_' + phase + r'">(.*?)</select>', html, re.S)
        self.assertIsNotNone(select)
        option = re.search(r'<option value="([^"]+)"\s+selected', select[1])
        self.assertIsNotNone(option)
        return option[1]

    def test_get_and_post_use_actual_product_catalogue_defaults_for_both_frameworks(self):
        html = self.client.get('/lxp/export').data.decode()
        for prefix in ('DigComp', 'AILit'):
            for phase in ('entry', 'final'):
                self.assertEqual(self.selected(html, prefix, phase), 'require')
        self.assertIn('CSS-Klasse', html)
        self.assertIn('lxp-diagnostics-only', html)
        self.assertIn('nicht verfügbar (ausgeblendet)', html)
        self.assertIn('optional markieren', html)
        self.assertIn('vorhandenen A/B/C-Menüs', html)
        self.assertIn('Die Fragen selbst müssen verfügbar bleiben', html)
        response = self.confirm(self.prepare())  # No phase form overrides.
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
        self.assertEqual(len(contract['assessments']), 4)
        self.assertTrue(all(value['intent'] == 'require' and value['status'] == 'provided'
                            for framework in contract['frameworks'] for value in framework['diagnostics'].values()))

    def test_custom_catalogue_defaults_are_not_replaced_by_prefix_assumptions(self):
        self.catalogue['frameworks'][0]['defaultDiagnosticIntents'] = {'entry': 'omit', 'final': 'ifAvailable'}
        self.catalogue['frameworks'][1]['defaultDiagnosticIntents'] = {'entry': 'ifAvailable', 'final': 'require'}
        html = self.client.get('/lxp/export').data.decode()
        for framework in self.catalogue['frameworks']:
            for phase, intent in framework['defaultDiagnosticIntents'].items():
                self.assertEqual(self.selected(html, framework['prefix'], phase), intent)
        response = self.confirm(self.prepare())
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
        for framework in contract['frameworks']:
            expected = next(value for value in self.catalogue['frameworks'] if value['id'] == framework['id'])
            for phase, intent in expected['defaultDiagnosticIntents'].items():
                self.assertEqual(framework['diagnostics'][phase]['intent'], intent)

    def test_saved_explicit_profile_wins_over_changed_defaults_until_author_changes_it(self):
        self.assertEqual(self.confirm(self.prepare(test_AILit_entry='omit', test_AILit_final='omit')).status_code, 200)
        profile = self.db.lxp_export_profiles.find_one()
        html = self.client.get('/lxp/export', query_string={'profile_id': profile['_id']}).data.decode()
        for phase in ('entry', 'final'):
            self.assertEqual(self.selected(html, 'AILit', phase), 'omit')
        response = self.confirm(self.prepare(profile_id=profile['_id']))
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
        ai = next(value for value in contract['frameworks'] if value['prefix'] == 'AILit')
        self.assertTrue(all(value['intent'] == 'omit' and value['status'] == 'notProvided' for value in ai['diagnostics'].values()))
        response = self.confirm(self.prepare(profile_id=profile['_id'], test_AILit_entry='require', test_AILit_final='require'))
        with ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(len(json.loads(archive.read('lxp-contract.json'))['assessments']), 4)

    def test_required_ailit_missing_questions_or_missing_published_domain_blocks_gui_zip(self):
        directory = self.root / 'builds' / self.ai / 'build/course/de'
        components = json.loads((directory / 'components.json').read_text())
        objects = json.loads((directory / 'contentObjects.json').read_text())
        for missing in ('questions', 'domain'):
            with self.subTest(missing=missing):
                fixtures.save(directory / 'components.json', [] if missing == 'questions' else components)
                values = json.loads(json.dumps(objects))
                if missing == 'domain':
                    next(item for item in values if item['_id'] == 'page-a')['_parentId'] = 'course'
                fixtures.save(directory / 'contentObjects.json', values)
                response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Blocked offer',
                    'course_id': [self.dig, self.ai], 'required_course': [self.dig, self.ai]})
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.mimetype, 'application/zip')
                self.assertIn('required diagnostic has no assigned questions' if missing == 'questions' else 'No explicitly configured diagnostic domain', response.data.decode())
                self.assertEqual(self.db.lxp_export_profiles.count_documents({}), 0)
