"""Root-app catalogue forms must actually reach listings, previews and ZIPs.

The existing GUI fixture is imported as a module, not inherited or exposed as
another discoverable TestCase. Its small wrapper proves route/export wiring;
it is not a claim of browser or LMS runtime acceptance.
"""
from copy import deepcopy
from io import BytesIO
import json
import unittest
from zipfile import ZipFile

from bson import ObjectId
from bs4 import BeautifulSoup

from lxp_catalogue import PackageValidationError, catalogue_sha256
from lxp_multi_export import get_source_binding, tree_manifest
import test_gui_multi_framework as fixtures


class CatalogueAdminExportTest(unittest.TestCase):
    setUpClass = fixtures.MultiFrameworkGuiTest.__dict__['setUpClass']
    tearDownClass = fixtures.MultiFrameworkGuiTest.__dict__['tearDownClass']
    setUp = fixtures.MultiFrameworkGuiTest.setUp
    make_course = fixtures.MultiFrameworkGuiTest.make_course
    token = fixtures.MultiFrameworkGuiTest.token
    prepare = fixtures.MultiFrameworkGuiTest.prepare
    confirm = fixtures.MultiFrameworkGuiTest.confirm

    def document(self, path='/lxp/export', **query):
        response = self.client.get(path, query_string=query)
        self.assertEqual(response.status_code, 200, response.data.decode())
        return BeautifulSoup(response.data, 'html.parser')

    def admin_form(self, action, framework_id=None):
        selector = '[data-catalogue-form="' + action + '"]'
        if framework_id:
            selector += '[data-framework-id="' + framework_id + '"]'
        form = self.document('/lxp/catalogue').select_one(selector)
        self.assertIsNotNone(form, selector)
        values = {}
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
            self.assertNotIn(element['name'], values)
            values[element['name']] = value
        return values

    def admin_save(self, action, framework_id=None, **changes):
        values = self.admin_form(action, framework_id)
        values.update(changes)
        response = self.client.post('/lxp/catalogue', data=values)
        self.assertEqual(response.status_code, 303, response.data.decode())
        return response

    def framework(self, prefix, version):
        return next(row for row in self.module.lxp_catalogue()['frameworks']
                    if (row['prefix'], row['version']) == (prefix, version))

    def register_framework(self, prefix='NewMedia', version='2027', *, count=12,
                           equal=True, marking=True, model=False):
        self.admin_save('add_framework', prefix=prefix, version=version, label=prefix + ' ' + version)
        framework = self.framework(prefix, version)
        identity = framework['id']
        self.admin_save('add_module', identity, number='1', label='Medienkompetenz')
        self.admin_save('add_domain', identity, label='Verstehen', source_titles='Einführung')
        framework = self.framework(prefix, version)
        domain = framework['domains'][0]['id']
        values = self.admin_form('edit_framework', identity)
        values['default_domain'] = domain  # Explicit choice for the flat fixture.
        for phase in ('entry', 'final'):
            values[phase + '_questionCount'] = str(count)
            for key, enabled in (('equalSampling', equal), ('showMarking', marking), ('showModelAnswer', model)):
                if enabled:
                    values[phase + '_' + key] = 'yes'
                else:
                    values.pop(phase + '_' + key, None)
        response = self.client.post('/lxp/catalogue', data=values)
        self.assertEqual(response.status_code, 303, response.data.decode())
        return self.framework(prefix, version)

    def archive(self, location):
        response = self.confirm(location)
        self.assertEqual(response.status_code, 200, response.data[:1000])
        self.assertEqual(response.mimetype, 'application/zip')
        return ZipFile(BytesIO(response.data))

    def binding(self, course_id):
        return get_source_binding(self.module.app.config['LXP_REGISTRY_PATH'],
                                  origin_id='gui-tenant', course_id=course_id)

    def control(self, document, name):
        element = document.select_one('[name="' + name + '"]')
        self.assertIsNotNone(element, name)
        if element.name == 'select':
            option = element.select_one('option[selected]') or element.select_one('option')
            return option.get('value', option.text)
        if element.get('type') == 'checkbox':
            return element.has_attr('checked')
        return element.get('value', '')

    def test_html_registration_immediately_maps_new_course_and_delivers_real_zip_without_mutating_sources(self):
        base_catalogue = deepcopy(self.catalogue)
        with self.archive(self.prepare()) as archive:
            previous_contract = json.loads(archive.read('lxp-contract.json'))
        original_bindings = {cid: deepcopy(self.binding(cid)) for cid in (self.dig, self.ai)}
        media = self.make_course('3' * 24, 'NewMedia1.1 – Neuer Medienkurs', questions=True)
        documents = list(self.db.courses.find().sort('_id'))
        manifests = {cid: tree_manifest(self.root / 'builds' / cid / 'build') for cid in (self.dig, self.ai, media)}
        source = self.db.courses.find_one({'_id': ObjectId(media)})
        with self.assertRaisesRegex(PackageValidationError, 'Rahmenpräfix'):
            self.module.course_assignment(source)
        before = self.document()
        row = before.select_one('input[name="course_id"][value="' + media + '"]').find_parent('tr')
        self.assertIn('Nicht zugeordnet', row.text)
        self.assertIsNotNone(before.select_one('a[href="/lxp/catalogue"]'))

        self.admin_save('add_framework', prefix='NewMedia', version='2027', label='Neue Medienkompetenz')
        identity = self.framework('NewMedia', '2027')['id']
        with self.assertRaisesRegex(PackageValidationError, 'Katalogplatz'):
            self.module.course_assignment(source)
        self.admin_save('add_module', identity, number='1', label='Medien verstehen')
        self.assertEqual(self.module.course_assignment(source)['frameworkId'], identity)
        self.admin_save('add_domain', identity, label='Verstehen', source_titles='Einführung')
        framework = self.framework('NewMedia', '2027')
        self.assertEqual(framework['modules'][0]['courseNumbers'], [])
        self.assertNotIn('defaultDomainId', framework)
        rejected = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Noch keine Domäne',
            'course_id': [media], 'required_course': [media]})
        self.assertEqual(rejected.status_code, 200)
        self.assertIn('No explicitly configured diagnostic domain', rejected.data.decode())
        self.assertNotEqual(rejected.mimetype, 'application/zip')
        self.admin_save('edit_framework', identity, default_domain=framework['domains'][0]['id'])
        framework = self.framework('NewMedia', '2027')

        listing = self.document('/admin/coursesview/')
        self.assertIn('NewMedia', listing.text)
        after = self.document()
        row = after.select_one('input[name="course_id"][value="' + media + '"]').find_parent('tr')
        self.assertIn('NewMedia / 2027', row.text)
        self.assertNotIn('Nicht zugeordnet', row.text)
        self.assertEqual(self.control(after, 'assessment_NewMedia_entry_questionCount'), '12')
        self.assertEqual(self.control(after, 'assessment_NewMedia_final_questionCount'), '12')
        for cid, binding in original_bindings.items():
            self.assertEqual(self.binding(cid), binding, 'Admin forms must not rewrite source registrations')

        with self.archive(self.prepare(course_id=[self.dig, media], required_course=[self.dig, media])) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            selections = json.loads(archive.read('assessment-selection.json'))
            packaged = json.loads(archive.read('lxp-catalogue.json'))
            self.assertEqual({row['prefix'] for row in contract['frameworks']}, {'DigComp', 'NewMedia'})
            new_course = next(row for row in contract['scos'] if row['source'].get('courseId') == media)
            old_course = next(row for row in contract['scos'] if row['source'].get('courseId') == self.dig)
            previous = next(row for row in previous_contract['scos'] if row['source'].get('courseId') == self.dig)
            self.assertEqual(old_course['id'], previous['id'])
            self.assertEqual((new_course['frameworkId'], new_course['moduleId']),
                             (identity, framework['modules'][0]['id']))
            self.assertEqual(new_course['number'], '1')
            self.assertEqual(len(contract['assessments']), 4)
            for summary in (row for row in selections if row['frameworkId'] == identity):
                self.assertEqual(summary['options'], {'mode': 'balanced', 'questionCount': 12,
                    'equalSampling': True, 'showMarking': True, 'showModelAnswer': False})
                self.assertEqual((summary['poolCount'], summary['selectedCount']), (1, 1))
            delivered_module = next(row for row in packaged['frameworks'] if row['id'] == identity)['modules'][0]
            self.assertEqual(delivered_module['courseNumbers'], ['1'])
            self.assertEqual(contract['package']['configuredCatalogueSha256'], catalogue_sha256(self.module.lxp_catalogue()))
            for name in ('course', 'contentObjects', 'articles', 'blocks', 'components'):
                relative = 'course/de/' + name + '.json'
                self.assertEqual(archive.read(new_course['launch']['root'] + '/' + relative),
                                 (self.root / 'builds' / media / 'build' / relative).read_bytes())
        self.assertEqual(self.framework('NewMedia', '2027')['modules'][0]['courseNumbers'], [])
        self.assertEqual(self.catalogue, base_catalogue)
        self.assertEqual(list(self.db.courses.find().sort('_id')), documents)
        for cid, manifest in manifests.items():
            self.assertEqual(tree_manifest(self.root / 'builds' / cid / 'build'), manifest)
        for cid, binding in original_bindings.items():
            self.assertEqual(self.binding(cid), binding)

    def test_admin_save_invalidates_hot_and_reconstructed_pending_previews(self):
        for reconstructed in (False, True):
            location = self.prepare()
            token = location.rsplit('/', 1)[-1]
            value = self.module.app.extensions['lxp_previews'][token]
            profile_id = value['profile_id']
            self.admin_save('edit_framework', 'fw-ailit-2026', label='AILit – geänderter Katalog ' + str(reconstructed))
            if reconstructed:
                self.module.app.extensions['lxp_previews'].pop(token)['prepared'].close()
                response = self.client.get(location)
                self.assertEqual(response.status_code, 410)
            else:
                response = self.confirm(location)
                self.assertEqual(response.status_code, 302)
                self.assertIn('Katalog', self.client.get(response.location).data.decode())
            self.assertNotEqual(response.mimetype, 'application/zip')
            self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': profile_id})['revision'], 0)
            self.assertIsNone(self.db.lxp_export_previews.find_one({'_id': token}))

    def test_new_framework_learning_course_needs_no_invented_domains_or_questions(self):
        self.admin_save('add_framework', prefix='Reading', version='2027', label='Lesekompetenz')
        framework = self.framework('Reading', '2027')
        self.assertEqual(framework['defaultDiagnosticIntents'], {'entry': 'ifAvailable', 'final': 'ifAvailable'})
        self.admin_save('add_module', framework['id'], number='1', label='Lesen')
        course = self.make_course('4' * 24, 'Reading1.1 – Ein Lernkurs ohne Test')
        with self.archive(self.prepare(course_id=[course], required_course=[course])) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            self.assertEqual(len(contract['scos']), 1)
            self.assertEqual(contract['assessments'], [])
            self.assertEqual(contract['domains'], [])
            self.assertEqual(contract['frameworks'][0]['diagnostics']['entry']['status'], 'notProvided')

    def test_gui_rejects_control_characters_in_names_before_saving(self):
        before = deepcopy(self.module.lxp_catalogue())
        values = self.admin_form('add_framework')
        values.update(prefix='Control', version='2027', label='Ungültig\nName')
        response = self.client.post('/lxp/catalogue', data=values)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.module.lxp_catalogue(), before)

    def test_saved_explicit_profile_options_win_over_gui_admin_defaults(self):
        location = self.prepare(assessment_DigComp_entry_present='yes', assessment_DigComp_entry_mode='balanced',
            assessment_DigComp_entry_questionCount='1', assessment_DigComp_entry_showModelAnswer='yes',
            assessment_DigComp_final_questionCount='1')
        with self.archive(location):
            pass
        profile = self.db.lxp_export_profiles.find_one()
        original_options = deepcopy(profile['settings']['assessment_options']['DigComp'])
        self.admin_save('edit_framework', 'fw-digcomp-22', entry_questionCount='5', final_questionCount='9')
        fresh = self.document()
        self.assertEqual(self.control(fresh, 'assessment_DigComp_entry_questionCount'), '5')
        self.assertEqual(self.control(fresh, 'assessment_DigComp_final_questionCount'), '9')
        saved = self.document(profile_id=profile['_id'])
        self.assertEqual(self.control(saved, 'assessment_DigComp_entry_questionCount'), '1')
        self.assertEqual(self.control(saved, 'assessment_DigComp_final_questionCount'), '1')
        self.assertFalse(self.control(saved, 'assessment_DigComp_entry_showMarking'))
        self.assertTrue(self.control(saved, 'assessment_DigComp_entry_showModelAnswer'))
        next_location = self.prepare(profile_id=profile['_id'])
        value = self.module.app.extensions['lxp_previews'][next_location.rsplit('/', 1)[-1]]
        self.assertEqual(value['settings']['assessment_options']['DigComp'], original_options)
        with self.archive(next_location) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            self.assertEqual(contract['package']['id'], profile['package_id'])
            self.assertEqual(contract['package']['revision'], '2')
        self.assertEqual(self.db.lxp_export_profiles.find_one({'_id': profile['_id']})['settings']['assessment_options']['DigComp'],
                         original_options)

    def test_get_selected_version_shows_matching_defaults_and_actual_selected_dropdown(self):
        self.register_framework('NewMedia', '2027', count=3)
        future = self.register_framework('NewMedia', '2028', count=7, equal=False, marking=False, model=True)
        doc = self.document(version_NewMedia='2028')
        self.assertEqual(len(doc.select('select[name="version_NewMedia"]')), 1)
        self.assertEqual(self.control(doc, 'version_NewMedia'), '2028')
        self.assertEqual(self.control(doc, 'assessment_NewMedia_entry_questionCount'), '7')
        self.assertFalse(self.control(doc, 'assessment_NewMedia_entry_equalSampling'))
        self.assertFalse(self.control(doc, 'assessment_NewMedia_entry_showMarking'))
        self.assertTrue(self.control(doc, 'assessment_NewMedia_entry_showModelAnswer'))
        with self.module.app.test_request_context('/lxp/export?version_NewMedia=2028'):
            controls = self.module._lxp_framework_controls(self.module.lxp_catalogue()['frameworks'], None)
        chosen = next(row for row in controls if row['prefix'] == 'NewMedia')
        self.assertEqual(chosen['id'], future['id'])

    def test_same_prefix_versions_export_separately_preserve_bindings_and_never_merge(self):
        first = self.register_framework('NewMedia', '2027', count=3)
        future = self.register_framework('NewMedia', '2028', count=7, equal=False, marking=False, model=True)
        future_course = self.make_course('3' * 24, 'NewMedia1.1 – Zukunftskurs', questions=True)
        old_course = self.make_course('4' * 24, 'NewMedia1.2 – Kurs der ersten Fassung', questions=True)
        for cid, version, framework, count in ((future_course, '2028', future, 7), (old_course, '2027', first, 3)):
            location = self.prepare(course_id=[cid], required_course=[cid], version_NewMedia=version)
            value = self.module.app.extensions['lxp_previews'][location.rsplit('/', 1)[-1]]
            self.assertEqual(value['settings']['assessment_options']['NewMedia']['entry']['questionCount'], count)
            with self.archive(location) as archive:
                contract = json.loads(archive.read('lxp-contract.json'))
                self.assertEqual([(row['id'], row['version']) for row in contract['frameworks']], [(framework['id'], version)])
                self.assertTrue(all(row['frameworkId'] == framework['id'] for row in contract['scos']))
                selection = json.loads(archive.read('assessment-selection.json'))
                self.assertTrue(all(row['options']['questionCount'] == count for row in selection))
            self.assertEqual(self.binding(cid)['binding']['frameworkId'], framework['id'])
        before = {cid: deepcopy(self.binding(cid)) for cid in (future_course, old_course)}
        bound_doc = self.document(course_id=future_course)
        # No explicit choice: the blank option truthfully means "existing
        # binding", while the controls themselves use that bound version.
        self.assertEqual(self.control(bound_doc, 'version_NewMedia'), '')
        self.assertEqual(bound_doc.select_one('[name="version_NewMedia"] option[value=""]').text,
                         'Bestehende Bindung: NewMedia / 2028')
        self.assertEqual(self.control(bound_doc, 'assessment_NewMedia_entry_questionCount'), '7')
        future_profile = self.db.lxp_export_profiles.find_one({'course_ids': [future_course]})
        self.assertIsNotNone(future_profile)
        saved_doc = self.document(profile_id=future_profile['_id'])
        self.assertEqual(self.control(saved_doc, 'version_NewMedia'), '2028')
        self.assertEqual(self.control(saved_doc, 'assessment_NewMedia_entry_questionCount'), '7')
        self.db.lxp_export_profiles.update_one({'_id': future_profile['_id']},
            {'$set': {'settings.framework_versions': {}}})
        legacy_profile_doc = self.document(profile_id=future_profile['_id'])
        self.assertEqual(self.control(legacy_profile_doc, 'version_NewMedia'), '')
        self.assertEqual(legacy_profile_doc.select_one('[name="version_NewMedia"] option[value=""]').text,
                         'Bestehende Bindung: NewMedia / 2028')
        self.assertEqual(self.control(legacy_profile_doc, 'assessment_NewMedia_entry_questionCount'), '7')
        metadata = self.module._lxp_metadata([self.db.courses.find_one({'_id': ObjectId(future_course)})])
        for profile in (None, future_profile):
            with self.module.app.test_request_context('/lxp/export'):
                controls = self.module._lxp_framework_controls(self.module.lxp_catalogue()['frameworks'], profile, metadata)
            versions = [row for row in controls if row['prefix'] == 'NewMedia']
            self.assertEqual([row['id'] for row in versions], [future['id']])
        for selection, versions, expected_error in (([future_course, old_course], {}, 'Two versions'),
                ([future_course], {'version_NewMedia': '2027'}, 'migration')):
            response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Kein Versionsmix',
                'course_id': selection, 'required_course': selection, **versions})
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected_error, response.data.decode())
            self.assertNotEqual(response.mimetype, 'application/zip')
            self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)
        self.assertEqual({cid: self.binding(cid) for cid in before}, before)


if __name__ == '__main__':
    unittest.main()
