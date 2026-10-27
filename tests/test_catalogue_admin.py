"""Real login, CSRF, HTML forms and durable CAS store, without importing app.py."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from bs4 import BeautifulSoup
from flask import Flask
from flask_login import LoginManager
from werkzeug.datastructures import MultiDict

from lxp_catalogue import IDENTIFIER, load_catalogue
from lxp_catalogue_admin import MAX_FORM_BYTES, blueprint
from lxp_catalogue_courses import normalize_course_title
from lxp_catalogue_store import read_snapshot, save_catalogue
from users import User


class CatalogueAdminTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = User('1', 'single-admin', 'test-only-password')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = load_catalogue()
        self.original = deepcopy(self.base)
        self.store = self.root / 'instance' / 'catalogue.json'
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'),
                         instance_path=str(self.root / 'instance'))
        self.app.config.update(TESTING=True, SECRET_KEY='catalogue-admin-test-secret',
            WTF_CSRF_ENABLED=True, LXP_CATALOGUE=self.base, LXP_CATALOGUE_STORE_PATH=str(self.store))
        manager = LoginManager(self.app)
        manager.login_view = 'login'
        manager.user_loader(lambda identity: self.admin if identity == '1' else None)
        self.app.add_url_rule('/login', endpoint='login', view_func=lambda: 'Login')
        self.app.add_url_rule('/courses', endpoint='coursesview.index_view', view_func=lambda: 'Courses')
        self.app.add_url_rule('/lxp/export', endpoint='lxp_export_setup', view_func=lambda: 'Export')
        self.app.register_blueprint(blueprint)
        self.client = self.app.test_client()
        self.login()

    def login(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True

    def snapshot(self):
        path = self.app.config.get('LXP_CATALOGUE_STORE_PATH')
        if path is None:
            path = Path(self.app.instance_path) / 'lxp-catalogue.json'
        return read_snapshot(self.base, path)

    def document(self, response=None):
        response = self.client.get('/lxp/catalogue') if response is None else response
        self.assertEqual(response.status_code, 200, response.data.decode())
        return BeautifulSoup(response.data, 'html.parser')

    def form(self, action, framework_id=None):
        selector = '[data-catalogue-form="' + action + '"]'
        if framework_id:
            selector += '[data-framework-id="' + framework_id + '"]'
        form = self.document().select_one(selector)
        self.assertIsNotNone(form, selector)
        data = {}
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
            self.assertNotIn(element['name'], data)
            data[element['name']] = value
        return data

    def post(self, action, framework_id=None, **changes):
        form = self.form(action, framework_id)
        form.update(changes)
        return self.client.post('/lxp/catalogue', data=form)

    def test_page_exposes_real_defaults_and_only_safe_edit_controls_without_writes(self):
        document = self.document()
        for identity, entry, final in [('fw-digcomp-22', '24', '39'), ('fw-ailit-2026', '12', '12')]:
            form = document.select_one('[data-catalogue-form="edit_framework"][data-framework-id="' + identity + '"]')
            self.assertEqual(form.select_one('[name="entry_questionCount"]')['value'], entry)
            self.assertEqual(form.select_one('[name="final_questionCount"]')['value'], final)
            self.assertTrue(form.select_one('[name="entry_equalSampling"]').has_attr('checked'))
            self.assertTrue(form.select_one('[name="entry_showMarking"]').has_attr('checked'))
            self.assertFalse(form.select_one('[name="entry_showModelAnswer"]').has_attr('checked'))
        self.assertIn('technische Zuordnungsregeln', document.text)
        self.assertFalse(document.select('[name*="sourceTitlePatterns"]'))
        self.assertFalse(document.select('[name*="delete"]'))
        self.assertFalse(self.store.exists())
        self.assertEqual(self.base, self.original)

    def test_login_is_required_for_reads_and_writes(self):
        form = self.form('add_framework')
        self.client = self.app.test_client()
        for response in (self.client.get('/lxp/catalogue'), self.client.post('/lxp/catalogue', data=form)):
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login', response.location)
        self.assertFalse(self.store.exists())

    def test_real_csrf_is_required_and_rejects_other_sessions_token(self):
        original = self.form('add_framework')
        original.update(prefix='Media', version='1', label='Medienkompetenz')
        for token in (None, '', 'not-a-csrf-token'):
            form = dict(original)
            if token is None:
                form.pop('csrf_token')
            else:
                form['csrf_token'] = token
            response = self.client.post('/lxp/catalogue', data=form)
            self.assertEqual(response.status_code, 400)
            self.assertIn('Formular', response.data.decode())
        self.client = self.app.test_client()
        self.login()
        self.assertEqual(self.client.post('/lxp/catalogue', data=original).status_code, 400)
        self.assertFalse(self.store.exists())

    def test_edit_round_trip_updates_labels_titles_defaults_and_keeps_existing_rules_and_ids(self):
        form = self.form('edit_framework', 'fw-digcomp-22')
        form.update(label='Digital kompetent', default_domain='', entry_intent='ifAvailable', final_intent='omit',
            entry_questionCount='6', final_questionCount='8')
        form['module_label_mod-digcomp22-1'] = 'Informationen finden'
        form['domain_label_verstehen'] = 'Wissen und verstehen'
        form['domain_titles_verstehen'] = 'A – Wissen\nVerstehen\n'
        form.pop('entry_equalSampling')
        form.pop('entry_showMarking')
        form['entry_showModelAnswer'] = 'yes'
        response = self.client.post('/lxp/catalogue', data=form)
        self.assertEqual(response.status_code, 303, response.data.decode())
        saved = self.snapshot()['catalogue']['frameworks'][0]
        self.assertEqual(saved['id'], 'fw-digcomp-22')
        self.assertEqual((saved['prefix'], saved['version'], saved['label']), ('DigComp', '2.2', 'Digital kompetent'))
        self.assertEqual(saved['modules'][0]['id'], 'mod-digcomp22-1')
        self.assertEqual(saved['modules'][0]['label'], 'Informationen finden')
        self.assertEqual(saved['domains'][0]['sourceTitles'], ['A – Wissen', 'Verstehen'])
        self.assertEqual(saved['domains'][0]['sourceTitlePatterns'], self.original['frameworks'][0]['domains'][0]['sourceTitlePatterns'])
        self.assertEqual(saved['defaultDiagnosticIntents'], {'entry': 'ifAvailable', 'final': 'omit'})
        self.assertEqual(saved['defaultAssessmentOptions']['entry'], {'mode': 'balanced', 'questionCount': 6,
            'equalSampling': False, 'showMarking': False, 'showModelAnswer': True})
        self.assertNotIn('defaultDomainId', saved)
        self.assertEqual(self.base, self.original)
        self.assertEqual(len(list(self.store.with_name('catalogue.json.backups').glob('*.json'))), 1)
        updated = self.document()
        self.assertIn('Digital kompetent', updated.text)

    def test_add_framework_module_and_domain_with_no_fake_course_slot_or_domain_inference(self):
        response = self.post('add_framework', prefix='Media', version='1', label='Medienkompetenz')
        self.assertEqual(response.status_code, 303, response.data.decode())
        framework = self.snapshot()['catalogue']['frameworks'][-1]
        identity = framework['id']
        self.assertTrue(IDENTIFIER.fullmatch(identity))
        self.assertEqual((framework['modules'], framework['domains']), ([], []))
        self.assertNotIn('defaultDomainId', framework)
        for options in framework['defaultAssessmentOptions'].values():
            self.assertEqual(options, {'mode': 'balanced', 'questionCount': 12, 'equalSampling': True,
                                       'showMarking': True, 'showModelAnswer': False})
        self.assertEqual(self.post('add_module', identity, number='1', label='Medien verstehen').status_code, 303)
        self.assertEqual(self.post('add_domain', identity, label='Gestalten', source_titles='D – Gestalten\nKreativ arbeiten').status_code, 303)
        framework = self.snapshot()['catalogue']['frameworks'][-1]
        self.assertEqual(framework['id'], identity)
        self.assertEqual(framework['modules'][0]['courseNumbers'], [])
        self.assertTrue(IDENTIFIER.fullmatch(framework['modules'][0]['id']))
        self.assertTrue(IDENTIFIER.fullmatch(framework['domains'][0]['id']))
        self.assertEqual(framework['domains'][0]['sourceTitles'], ['D – Gestalten', 'Kreativ arbeiten'])
        self.assertNotIn('defaultDomainId', framework)
        mapping = normalize_course_title('Media1.10 – Mein Kurs', self.snapshot()['catalogue'])
        self.assertEqual((mapping['frameworkId'], mapping['moduleId'], mapping['courseNumber']),
                         (identity, framework['modules'][0]['id'], '10'))
        self.assertEqual(self.base, self.original)

    def test_new_framework_version_keeps_all_existing_identities(self):
        self.assertEqual(self.post('add_framework', prefix='AILit', version='2027', label='AILit 2027').status_code, 303)
        catalogue = self.snapshot()['catalogue']
        self.assertEqual(catalogue['frameworks'][:2], self.original['frameworks'])
        self.assertEqual(catalogue['frameworks'][-1]['prefix'], 'AILit')
        self.assertEqual(catalogue['frameworks'][-1]['version'], '2027')
        self.assertNotEqual(catalogue['frameworks'][-1]['id'], catalogue['frameworks'][1]['id'])

    def test_stale_browser_form_cannot_overwrite_and_requires_explicit_reload(self):
        stale = self.form('edit_framework', 'fw-ailit-2026')
        current = self.snapshot()
        changed = deepcopy(current['catalogue'])
        changed['frameworks'][1]['label'] = 'Anderes Fenster'
        save_catalogue(self.base, self.store, changed, current['sha256'])
        before = self.store.read_bytes()
        stale['label'] = 'Veraltete Eingabe'
        response = self.client.post('/lxp/catalogue', data=stale)
        self.assertEqual(response.status_code, 409)
        document = BeautifulSoup(response.data, 'html.parser')
        self.assertIn('nicht gespeichert', document.text)
        self.assertIn('anderen Fenster', document.text)
        self.assertIsNotNone(document.select_one('a[href="/lxp/catalogue"]'))
        self.assertFalse(document.select('form'))
        self.assertEqual(self.store.read_bytes(), before)
        self.assertIn('Anderes Fenster', self.document().text)

    def test_existing_identity_fields_are_immutable_and_no_delete_action_is_accepted(self):
        base = self.form('edit_framework', 'fw-digcomp-22')
        changes = [{'prefix': 'Other'}, {'version': '3'}, {'module_number_mod-digcomp22-1': '99'},
                   {'module_id_mod-digcomp22-1': 'mod-changed'}, {'domain_id_verstehen': 'changed-domain'},
                   {'action': 'delete_framework'}, {'sourceTitlePatterns': '^Anything'}]
        for change in changes:
            form = dict(base, **change)
            with self.subTest(change=change):
                self.assertEqual(self.client.post('/lxp/catalogue', data=form).status_code, 400)
        self.assertFalse(self.store.exists())

    def test_concurrent_module_or_domain_addition_is_also_a_conflict_not_a_refreshed_edit_form(self):
        for addition in ('module', 'domain'):
            stale = self.form('edit_framework', 'fw-ailit-2026')
            current = self.snapshot()
            candidate = deepcopy(current['catalogue'])
            framework = candidate['frameworks'][1]
            if addition == 'module':
                framework['modules'].append({'id': 'mod-added', 'number': '2', 'label': 'New module', 'courseNumbers': []})
            else:
                framework['domains'].append({'id': 'dom-added', 'label': 'New domain', 'sourceTitles': ['D – Neu']})
            save_catalogue(self.base, self.store, candidate, current['sha256'])
            before = self.store.read_bytes()
            stale['label'] = 'Alter Formularstand'
            response = self.client.post('/lxp/catalogue', data=stale)
            with self.subTest(addition=addition):
                self.assertEqual(response.status_code, 409)
                self.assertFalse(BeautifulSoup(response.data, 'html.parser').select('form'))
                self.assertNotIn('expected_sha256', response.data.decode())
                self.assertEqual(self.store.read_bytes(), before)

    def test_unknown_missing_duplicate_fields_and_malformed_boolean_values_are_rejected(self):
        base = self.form('edit_framework', 'fw-ailit-2026')
        cases = []
        for change in ({'unknown': 'value'}, {'entry_showMarking': 'true'}, {'entry_intent': 'random'},
                       {'entry_mode': 'random'}, {'default_domain': 'unknown'}):
            cases.append(MultiDict(dict(base, **change)))
        missing = MultiDict(base)
        missing.pop('label')
        cases.append(missing)
        for key in ('action', 'csrf_token', 'expected_sha256', 'entry_questionCount', 'entry_equalSampling'):
            duplicate = MultiDict(base)
            duplicate.add(key, base[key])
            cases.append(duplicate)
        for form in cases:
            with self.subTest(form=list(form.items(multi=True))):
                self.assertEqual(self.client.post('/lxp/catalogue', data=form).status_code, 400)
        self.assertFalse(self.store.exists())

    def test_invalid_counts_preserve_input_on_error_without_saving(self):
        base = self.form('edit_framework', 'fw-ailit-2026')
        for count in ('', '0', '-1', '01', '1.5', '10001', 'a'):
            form = dict(base, entry_questionCount=count)
            response = self.client.post('/lxp/catalogue', data=form)
            self.assertEqual(response.status_code, 400)
            doc = BeautifulSoup(response.data, 'html.parser')
            target = doc.select_one('[data-catalogue-form="edit_framework"][data-framework-id="fw-ailit-2026"]')
            self.assertEqual(target.select_one('[name="entry_questionCount"]')['value'], count)
        self.assertFalse(self.store.exists())

    def test_all_mode_and_public_reference_keep_their_existing_meaning(self):
        form = self.form('edit_framework', 'fw-ailit-2026')
        form.update(entry_mode='all', entry_questionCount='')
        self.assertEqual(self.client.post('/lxp/catalogue', data=form).status_code, 303)
        self.assertIsNone(self.snapshot()['catalogue']['frameworks'][1]['defaultAssessmentOptions']['entry']['questionCount'])
        self.assertEqual(self.post('edit_framework', 'fw-ailit-2026', entry_mode='reference', entry_questionCount='24').status_code, 400)
        self.assertEqual(self.post('edit_framework', 'fw-digcomp-22', entry_mode='reference', entry_questionCount='25').status_code, 400)
        response = self.post('edit_framework', 'fw-digcomp-22', entry_mode='reference', entry_questionCount='24',
                             final_mode='reference', final_questionCount='39')
        self.assertEqual(response.status_code, 303, response.data.decode())
        defaults = self.snapshot()['catalogue']['frameworks'][0]['defaultAssessmentOptions']
        self.assertEqual((defaults['entry']['questionCount'], defaults['final']['questionCount']), (24, 39))
        self.assertEqual(defaults['entry']['mode'], 'reference')

    def test_invalid_new_prefixes_versions_and_modules_do_not_write(self):
        for prefix in ('Media2', 'Média', ' Media', 'Media ', 'AILIT', 'Media-Tools'):
            response = self.post('add_framework', prefix=prefix, version='1', label='Media')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post('add_framework', prefix='AILit', version='2026', label='Duplicate').status_code, 400)
        self.assertEqual(self.post('add_framework', prefix='Media', version='', label='Media').status_code, 400)
        for number in ('1', '01', '0', '-1', '1.5', '1000000000000'):
            response = self.post('add_module', 'fw-ailit-2026', number=number, label='Duplicate or invalid')
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.store.exists())

    def test_domains_use_literal_titles_escape_html_and_preserve_ailit_no_fallback(self):
        response = self.post('add_domain', 'fw-ailit-2026', label='<script>alert(1)</script>', source_titles='^D.*$')
        self.assertEqual(response.status_code, 303)
        framework = self.snapshot()['catalogue']['frameworks'][1]
        domain = framework['domains'][-1]
        self.assertEqual(domain['sourceTitles'], ['^D.*$'])
        self.assertNotIn('sourceTitlePatterns', domain)
        self.assertNotIn('defaultDomainId', framework)
        html = self.client.get('/lxp/catalogue').data.decode()
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertEqual(self.post('edit_framework', framework['id'], default_domain=domain['id']).status_code, 303)
        self.assertEqual(self.snapshot()['catalogue']['frameworks'][1]['defaultDomainId'], domain['id'])

    def test_oversized_or_non_browser_form_requests_are_refused_without_writes(self):
        base = self.form('add_framework')
        base.update(prefix='Media', version='1', label='Media')
        self.assertEqual(self.client.post('/lxp/catalogue', data=dict(base, label='x' * MAX_FORM_BYTES)).status_code, 400)
        self.assertEqual(self.client.post('/lxp/catalogue', json=base).status_code, 400)
        self.assertEqual(self.client.post('/lxp/catalogue', data=base, content_type='multipart/form-data').status_code, 400)
        self.assertEqual(self.client.post('/lxp/catalogue', data=base, environ_overrides={'CONTENT_LENGTH': ''}).status_code, 400)
        self.assertFalse(self.store.exists())

    def test_default_instance_path_is_used_without_editing_configured_catalogue(self):
        self.app.config.pop('LXP_CATALOGUE_STORE_PATH')
        response = self.post('add_framework', prefix='Media', version='1', label='Media')
        self.assertEqual(response.status_code, 303)
        self.assertTrue((Path(self.app.instance_path) / 'lxp-catalogue.json').is_file())
        self.assertFalse(self.store.exists())
        self.assertEqual(self.base, self.original)

    def test_broken_override_does_not_fall_back_to_editable_default_catalogue(self):
        self.store.parent.mkdir()
        self.store.write_text('{broken', encoding='utf-8')
        response = self.client.get('/lxp/catalogue')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BeautifulSoup(response.data, 'html.parser').select('form'))
        self.assertEqual(self.store.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
