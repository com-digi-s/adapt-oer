"""The GUI may only expose shared, genuinely editable AAT course records."""
from unittest.mock import patch
import unittest

import mongomock
from bson import ObjectId
from bs4 import BeautifulSoup

import test_gui_multi_framework as fixtures


class PublicationCatalogueGuiTest(unittest.TestCase):
    setUpClass = fixtures.MultiFrameworkGuiTest.__dict__['setUpClass']
    tearDownClass = fixtures.MultiFrameworkGuiTest.__dict__['tearDownClass']
    make_course = fixtures.MultiFrameworkGuiTest.make_course
    token = fixtures.MultiFrameworkGuiTest.token
    prepare = fixtures.MultiFrameworkGuiTest.prepare
    confirm = fixtures.MultiFrameworkGuiTest.confirm

    def setUp(self):
        fixtures.MultiFrameworkGuiTest.setUp(self)
        previous = self.module.app.config.get('AUTHORING_DOMAIN')
        self.addCleanup(self.module.app.config.__setitem__, 'AUTHORING_DOMAIN', previous)
        self.module.app.config['AUTHORING_DOMAIN'] = 'https://authoring.example/'
        self.course_view = next(view for view in self.module.admin._views
                                if isinstance(view, self.module.CourseView))

    def ghost_reference(self):
        identifier = ObjectId('3' * 24)
        self.db.lxp_export_courses_test.insert_one({'_id': identifier,
            'title': 'DigComp1.2 – Ghost reference', 'displayTitle': 'Ghost reference',
            '_isShared': True, 'tags': [], '_lxpSource': {'kind': 'native-registered'}})
        return str(identifier)

    def test_only_actual_shared_aat_courses_in_list_and_export(self):
        self.make_course('4' * 24, 'DigComp1.3 – Private AAT draft', shared=False)
        self.ghost_reference()
        expected = {self.dig, self.ai}
        self.assertEqual(self.module.lxp_courses().name, 'courses')
        self.assertEqual(self.course_view.coll.name, 'courses')
        self.assertEqual(self.course_view.endpoint, 'coursesview')
        for endpoint in ('/admin/coursesview/', '/lxp/export'):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            doc = BeautifulSoup(response.data, 'html.parser')
            links = doc.select('a[data-aat-course-id]')
            self.assertEqual({link['data-aat-course-id'] for link in links}, expected)
            for link in links:
                self.assertEqual(link['href'], 'https://authoring.example/#editor/' + link['data-aat-course-id'])
                self.assertEqual(link['target'], '_blank')
                self.assertIn('noopener', link['rel'])
            self.assertNotIn('Ghost reference', doc.get_text())
            self.assertNotIn('Private AAT draft', doc.get_text())
            self.assertNotIn('nicht im AAT bearbeitbar', doc.get_text())
            self.assertIsNone(doc.select_one('[data-source-kind="native-registered"]'))
        form = BeautifulSoup(self.client.get('/lxp/export').data, 'html.parser')
        self.assertEqual({item['value'] for item in form.select('input[name=course_id]')}, expected)

    def test_obsolete_collection_setting_cannot_select_foreign_course_inventory(self):
        self.ghost_reference()
        with patch.dict(self.module.app.config, {'LXP_COURSES_COLLECTION': 'lxp_export_courses_test'}):
            self.assertEqual(self.module.lxp_courses().name, 'courses')
            doc = BeautifulSoup(self.client.get('/lxp/export').data, 'html.parser')
            self.assertEqual({item['value'] for item in doc.select('input[name=course_id]')}, {self.dig, self.ai})

    def test_ghost_reference_cannot_be_exported_by_forged_post(self):
        ghost = self.ghost_reference()
        response = self.client.post('/lxp/export', data={'csrf_token': self.token(),
            'title': 'Rejected offer', 'course_id': [self.ai, ghost], 'required_course': [self.ai, ghost]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('nicht mehr freigegeben oder nicht zugänglich', response.data.decode())
        self.assertEqual(self.db.lxp_export_profiles.count_documents({}), 0)

    def test_unshared_actual_course_remains_unauthorized(self):
        hidden = self.make_course('4' * 24, 'DigComp1.3 – Private AAT draft', shared=False)
        with self.assertRaisesRegex(ValueError, 'nicht mehr freigegeben'):
            self.module._lxp_selected([self.ai, hidden])

    def test_navigation_reads_same_actual_shared_source_ids(self):
        self.ghost_reference()
        sources = self.module._lxp_navigation_sources()
        self.assertEqual({row['metadata']['courseId'] for row in sources}, {self.dig, self.ai})
        self.assertTrue(all(row['metadata']['kind'] == 'aat-published' for row in sources))

    def test_current_database_handle_is_resolved_each_time(self):
        database = mongomock.MongoClient().isolated_authoring_test
        with patch.object(self.module, 'db', database):
            self.assertIs(self.module.lxp_courses().database, database)

    def test_edit_link_label_is_escaped(self):
        rendered = str(self.module.CourseView.editor_formatter(None, None,
            {'_id': ObjectId(self.dig), 'displayTitle': '<img src=x onerror=alert(1)>'}, 'displayTitle'))
        self.assertNotIn('<img', rendered)
        self.assertIn('&lt;img', rendered)
        self.assertIn('/#editor/' + self.dig, rendered)

    def test_export_routes_still_require_login(self):
        other = self.module.app.test_client()
        self.assertEqual(other.get('/lxp/export').status_code, 302)

    def test_missing_authoring_address_does_not_break_list_or_selection(self):
        with patch.dict(self.module.app.config, {'AUTHORING_DOMAIN': None}):
            for endpoint in ('/admin/coursesview/', '/lxp/export'):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                doc = BeautifulSoup(response.data, 'html.parser')
                self.assertIsNone(doc.select_one('a[data-aat-course-id]'))
