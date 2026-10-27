"""Exercise the real Flask routes and real exporter against isolated Mongo/builds."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import mongomock
from bson import ObjectId


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class MultiFrameworkGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {'DB_HOST': 'localhost', 'MONGODB_PORT': '27017',
            'DB_NAME': 'gui_test', 'SECRET_KEY': 'gui-test-secret', 'USER': 'gui-admin', 'PASS': 'gui-password'})
        cls.environment.start()
        cls.mongo = mongomock.MongoClient('mongodb://localhost/gui_test')
        cls.mongo_patch = patch('pymongo.MongoClient', return_value=cls.mongo)
        cls.mongo_patch.start()
        import app
        cls.module = app

    @classmethod
    def tearDownClass(cls):
        cls.mongo_patch.stop()
        cls.environment.stop()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.module.db
        for collection in self.db.list_collection_names():
            self.db[collection].delete_many({})
        for value in self.module.app.extensions['lxp_previews'].values():
            value['prepared'].close()
        self.module.app.extensions['lxp_previews'].clear()
        from lxp_catalogue import load_catalogue
        self.catalogue = load_catalogue()
        self.wrapper = self.root / 'wrapper.zip'
        # Bind the complete production capability schema. The tiny implementation
        # is only a route/export fixture; genuine browser acceptance is separate.
        scripts = {'js/multiFramework.js': b'export class MultiFrameworkApp { attachAdaptPersistence() {} } // lxp-contract.json frameworkId assessmentId',
                   'js/corePersistence.js': b'// Isolated GUI/export contract fixture.',
                   'js/multiContract.js': b'// Contract module fixture.',
                   'js/multiState.js': b'// State module fixture.',
                   'js/multiBridge.js': b'// Bridge module fixture.',
                   'js/multiAssessment.js': b'// Assessment module fixture.',
                   'index.html': b'<html><script type="module">if (window.CONF.MULTI_FRAMEWORK_CONTRACT) { import("./js/multiFramework.js"); }</script></html>'}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in scripts.items()}
        from lxp_catalogue import canonical_bytes
        capability = {'capability': 'multi-framework', 'version': 1, 'packageContract': 'adapt-lxp-multi-framework@1',
            'persistenceContract': 1, 'launchContext': 'explicit-sco-id', 'implementationFiles': hashes,
            'implementationSha256': hashlib.sha256(canonical_bytes(hashes)).hexdigest()}
        with ZipFile(self.wrapper, 'w') as archive:
            for name, content in scripts.items():
                archive.writestr(name, content)
            archive.writestr('config.json', '{}')
            archive.writestr('adapt-persistence-contract.json', json.dumps({'version': 1,
                'bootstrap': 'parent.App.attachAdaptPersistence', 'adapter': 'js/corePersistence.js'}))
            archive.writestr('adapt-multi-framework-capabilities.json', json.dumps(capability))
        self.module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SECRET_KEY='gui-test-secret',
            BUILDS_DIR=str(self.root / 'builds'), LXP_WRAPPER_ZIP=str(self.wrapper), LXP_WRAPPER_SHA256=None,
            LXP_CATALOGUE=self.catalogue, LXP_ORIGIN_ID='gui-tenant',
            LXP_REGISTRY_PATH=str(self.root / 'registry.json'),
            LXP_ASSESSMENT_TEMPLATE=str(Path('static/assessment-template.zip').resolve()))
        self.dig = self.make_course('1' * 24, 'DigComp1.1 – Echter DigComp-Kurs', questions=True)
        self.ai = self.make_course('2' * 24, 'AILit1.1 – Echter AILit-Kurs')
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True

    def make_course(self, cid, title, *, questions=False, shared=True):
        course_id = ObjectId(cid)
        self.db.courses.insert_one({'_id': course_id, 'title': title, 'displayTitle': title,
            '_isShared': shared, 'tags': []})
        build = self.root / 'builds' / cid / 'build'
        save(build / 'course/config.json', {'_defaultLanguage': 'de'})
        save(build / 'course/de/course.json', {'_id': 'course', 'title': title})
        save(build / 'course/de/contentObjects.json', [{'_id': 'page', '_parentId': 'course', '_type': 'page', 'title': 'Seite'}])
        save(build / 'course/de/articles.json', [{'_id': 'article', '_parentId': 'page'}])
        save(build / 'course/de/blocks.json', [{'_id': 'block', '_parentId': 'article'}])
        save(build / 'course/de/components.json', [{'_id': 'question', '_parentId': 'block', '_component': 'mcq',
            '_classes': 'et at', 'body': 'Echte markierte Frage', '_items': [
                {'text': 'Ja', '_shouldBeSelected': True}, {'text': 'Nein', '_shouldBeSelected': False}], '_onScreen': {}}] if questions else [])
        (build / 'adapt/js').mkdir(parents=True)
        (build / 'adapt/js/adapt.min.js').write_text('var Adapt$1=__AMD("core/js/adapt",Adapt);')
        (build / 'index_lms.html').write_text('<html><body>Original published course</body></html>')
        (build / 'imsmanifest.xml').write_text('<manifest><organizations default="org"><organization identifier="org"><item identifier="item" identifierref="r"/></organization></organizations><resources><resource identifier="r" href="index_lms.html"/></resources></manifest>')
        return cid

    def token(self):
        response = self.client.get('/lxp/export')
        self.assertEqual(response.status_code, 200)
        return re.search(rb'name="csrf_token" value="([^"]+)"', response.data)[1].decode()

    def prepare(self, **extra):
        data = {'csrf_token': self.token(), 'title': 'Gemeinsames Angebot',
            'course_id': [self.dig, self.ai], 'required_course': [self.dig, self.ai]}
        data.update(extra)
        response = self.client.post('/lxp/export', data=data)
        self.assertEqual(response.status_code, 302, response.data.decode())
        self.assertIn('/lxp/preview/', response.location)
        return response.location

    def confirm(self, location):
        preview = self.client.get(location)
        self.assertEqual(preview.status_code, 200, preview.data.decode())
        token = re.search(rb'name="csrf_token" value="([^"]+)"', preview.data)[1].decode()
        fingerprint = re.search(rb'name="preflight_sha256" value="([^"]+)"', preview.data)[1].decode()
        return self.client.post(location.replace('/preview/', '/confirm/'), data={
            'csrf_token': token, 'preflight_sha256': fingerprint})

    def test_real_mixed_preview_one_zip_persisted_package_profile_and_new_offer(self):
        location = self.prepare()
        preview = self.client.get(location).data.decode()
        self.assertIn('DigComp 2.2', preview)
        self.assertIn('AILit 2026', preview)
        self.assertIn('Nicht bereitgestellt (kein Score / kein Bestehen)', preview)
        response = self.confirm(location)
        self.assertEqual(response.status_code, 200, response.data[:1000])
        self.assertEqual(response.mimetype, 'application/zip')
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            self.assertEqual(len(contract['frameworks']), 2)
            self.assertEqual(len(contract['assessments']), 2)
            self.assertEqual(len([s for s in contract['scos'] if s['role'] == 'learning']), 2)
        profile = self.db.lxp_export_profiles.find_one()
        self.assertEqual(profile['package_id'], contract['package']['id'])
        updated = self.confirm(self.prepare(profile_id=profile['_id']))
        with ZipFile(BytesIO(updated.data)) as archive:
            next_contract = json.loads(archive.read('lxp-contract.json'))
        self.assertEqual(next_contract['package']['id'], contract['package']['id'])
        self.assertEqual(next_contract['package']['revision'], '2')
        other = self.confirm(self.prepare())
        with ZipFile(BytesIO(other.data)) as archive:
            self.assertNotEqual(json.loads(archive.read('lxp-contract.json'))['package']['id'], contract['package']['id'])
        self.assertEqual(self.client.post(location.replace('/preview/', '/confirm/'), data={'csrf_token': self.token()}).status_code, 410)

    def test_csrf_required_on_prepare_and_confirmation(self):
        self.assertEqual(self.client.post('/lxp/export', data={'course_id': self.dig}).status_code, 400)
        location = self.prepare()
        self.assertEqual(self.client.post(location.replace('/preview/', '/confirm/')).status_code, 400)
        self.assertEqual(self.confirm(location).status_code, 200)

    def test_unpublished_title_change_and_required_ailit_test_fail_without_zip(self):
        self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'title': 'AILit1.2 – Noch nicht veröffentlicht'}})
        response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Angebot',
            'course_id': [self.ai], 'required_course': [self.ai]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('erneut veröffentlichen', response.data.decode())
        self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'title': 'AILit1.1 – Echter AILit-Kurs'}})
        response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Angebot',
            'course_id': [self.ai], 'required_course': [self.ai], 'test_AILit_entry': 'require'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('required diagnostic has no assigned questions', response.data.decode())
        self.assertEqual(self.db.lxp_export_profiles.count_documents({}), 0)

    def test_changed_source_catalogue_wrapper_or_access_invalidates_confirmation(self):
        for change in ('title', 'build', 'catalogue', 'wrapper', 'shared'):
            with self.subTest(change=change):
                location = self.prepare()
                if change == 'title':
                    self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'title': 'AILit1.1 – Unveröffentlicht'}})
                elif change == 'build':
                    (self.root / 'builds' / self.ai / 'build/index_lms.html').write_text('changed')
                elif change == 'catalogue':
                    self.catalogue['revision'] += '-changed'
                elif change == 'wrapper':
                    with ZipFile(self.wrapper, 'a') as archive:
                        archive.writestr('changed.txt', 'changed')
                else:
                    self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_isShared': False}})
                response = self.confirm(location)
                self.assertEqual(response.status_code, 302)
                self.assertNotEqual(response.mimetype, 'application/zip')
                self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'title': 'AILit1.1 – Echter AILit-Kurs', '_isShared': True}})

    def test_filters_preserve_shared_base_and_numeric_order_before_pagination_flat_pages_no_dedup(self):
        self.catalogue['frameworks'][1]['modules'][0]['courseNumbers'] += ['10']
        self.make_course('3' * 24, 'AILit1.10 – Zehn')
        self.make_course('4' * 24, 'AILit1.2 – Zwei')
        hidden = self.make_course('5' * 24, 'AILit1.3 – Nicht freigegeben', shared=False)
        response = self.client.get('/admin/coursesview/?flt0_1=AILit&page_size=2')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Nicht freigegeben', response.data.decode())
        self.assertLess(response.data.decode().index('Echter AILit-Kurs'), response.data.decode().index('Zwei'))
        self.assertNotIn('Zehn', response.data.decode())
        view = next(v for v in self.module.admin._views if isinstance(v, self.module.CourseView))
        with self.module.app.test_request_context('/admin/coursesview/'):
            count, rows = view.get_list(0, None, False, None, [(1, 'prefix', 'AILit')], page_size=2)
        self.assertEqual(count, 3)
        self.assertEqual([row['course_number'] for row in rows], ['1', '2'])
        for ident in ('a' * 24, 'b' * 24):
            self.db.contentobjects.insert_one({'_id': ObjectId(ident), '_courseId': ObjectId(self.ai),
                '_parentId': ObjectId(self.ai), '_type': 'page', 'title': 'Gleiche Seite', '_sortOrder': 0})
        duplicate = self.make_course('7' * 24, 'AILit1.1 – Gleicher Platz andere Quelle')
        self.db.contentobjects.insert_one({'_id': ObjectId('d' * 24), '_courseId': ObjectId(duplicate),
            '_parentId': ObjectId(duplicate), '_type': 'page', 'title': 'Gleiche Seite', '_sortOrder': 0})
        contents = next(v for v in self.module.admin._views if isinstance(v, self.module.ContentsView))
        with self.module.app.test_request_context('/admin/contentobjectsview/'):
            count, rows = contents.get_list(0, None, False, None, [], page_size=20)
        self.assertEqual(count, 3)
        self.assertEqual(len(rows), 3)
        self.assertLess(len(set(row['taxonomy_string'] for row in rows)), len(rows))
        self.assertTrue(all(row['taxonomy_string'].startswith('AILit1.1.') for row in rows))

    def test_prefixed_standalone_assembly_fails_explicitly_instead_of_9_9_relabelling(self):
        unit = ObjectId('a' * 24)
        self.db.contentobjects.insert_one({'_id': unit, '_courseId': ObjectId(self.ai), '_parentId': ObjectId(self.ai), '_type': 'page'})
        with self.assertRaisesRegex(ValueError, 'Präfixierte oder gemischte'):
            self.module.build_assembled_course_zip([str(unit)])

    def test_optional_learning_flags_and_empty_required_scope(self):
        response = self.confirm(self.prepare(required_course=[self.dig]))
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
        learning = {sco['source']['courseId']: sco for sco in contract['scos'] if sco['role'] == 'learning'}
        self.assertTrue(learning[self.dig]['requiredForCompletion'])
        self.assertFalse(learning[self.ai]['requiredForCompletion'])
        response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'title': 'Kein Pflichtkurs',
            'course_id': [self.dig, self.ai]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('mindestens ein Lernkurs muss verpflichtend sein', response.data.decode())

    def test_known_source_binding_keeps_version_and_native_identity(self):
        from lxp_multi_export import register_native_source
        register_native_source(self.module.app.config['LXP_REGISTRY_PATH'], origin_id='native-test', course_id='stable-native-id')
        self.db.courses.update_one({'_id': ObjectId(self.ai)}, {'$set': {'_lxpSource': {
            'kind': 'native-registered', 'originId': 'native-test', 'courseId': 'stable-native-id'}}})
        response = self.confirm(self.prepare())
        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(response.data)) as archive:
            course = next(s for s in json.loads(archive.read('lxp-contract.json'))['scos'] if s['role'] == 'learning' and s['frameworkId'] == 'fw-ailit-2026')
        self.assertEqual(course['source']['kind'], 'native-registered')
        self.assertEqual(course['source']['courseId'], 'stable-native-id')
        other = deepcopy(self.catalogue['frameworks'][1])
        other.update(id='fw-ailit-next', version='next')
        for module in other['modules']:
            module['id'] += '-next'
        self.catalogue['frameworks'].append(other)
        assignment = self.module.course_assignment(self.db.courses.find_one({'_id': ObjectId(self.ai)}))
        self.assertEqual(assignment['version'], '2026')

    def test_legacy_quiz_action_never_returns_partial_zip_for_prefixed_sources(self):
        question = ObjectId('c' * 24)
        self.db.components.insert_one({'_id': question, '_courseId': ObjectId(self.ai),
            '_component': 'mcq', '_classes': 'et at'})
        response = self.client.post('/admin/questionsview/action/', data={'action': 'batch_download', 'rowid': [str(question)]}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('alte Einzelquiz-Export unterstützt keine Kompetenzrahmen', response.data.decode())
        self.assertNotEqual(response.mimetype, 'application/zip')

    def test_worker_restart_reconstructs_exact_server_preflight_and_still_one_use(self):
        location = self.prepare()
        value = self.module.app.extensions['lxp_previews'].pop(location.rsplit('/', 1)[1])
        old_hash = value['prepared'].preflight_sha256
        value['prepared'].close()
        self.assertIn(old_hash.encode(), self.client.get(location).data)
        self.assertEqual(self.confirm(location).status_code, 200)
        self.assertEqual(self.client.get(location).status_code, 410)

    def test_explicit_known_numeric_legacy_import_works_without_json_edits(self):
        numeric = self.make_course('6' * 24, '1.2 Bekannte veröffentlichte Altquelle', questions=True)
        response = self.confirm(self.prepare(course_id=[numeric], required_course=[numeric], legacy_import='yes'))
        self.assertEqual(response.status_code, 200)
        from lxp_multi_export import get_source_binding
        binding = get_source_binding(self.module.app.config['LXP_REGISTRY_PATH'], origin_id='gui-tenant', course_id=numeric)
        self.assertTrue(binding['legacyApproved'])
        self.assertEqual(self.module.course_assignment(self.db.courses.find_one({'_id': ObjectId(numeric)}))['prefix'], 'DigComp')

    def test_reused_profile_deselect_all_does_not_restore_previous_selection(self):
        self.assertEqual(self.confirm(self.prepare()).status_code, 200)
        profile = self.db.lxp_export_profiles.find_one()
        response = self.client.post('/lxp/export', data={'csrf_token': self.token(), 'profile_id': profile['_id'],
            'title': 'Auswahl entfernt', 'required_course': [self.dig, self.ai]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('mindestens einen gültigen veröffentlichten Kurs', response.data.decode())


if __name__ == '__main__':
    unittest.main()
