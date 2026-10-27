"""New authored addresses must reach the actual frozen exporter and GUI."""
from copy import deepcopy
import json
import unittest

import test_multi_framework_export as fixtures
import test_gui_multi_framework as gui_fixtures
from lxp_catalogue import PackageValidationError, catalogue_sha256


class CourseDiscoveryExportTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MixedPackageTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_new_course_is_exported_with_derived_catalogue_and_unchanged_configured_catalogue(self):
        fixture = self.fixture
        original = deepcopy(fixture.catalogue)
        fixture.add_source('ailit-five', 'AILit1.5 – Neue Lerneinheit', 'tenant-b', 'aat-new-five')
        prepared = fixture.prepare()
        self.assertEqual(fixture.catalogue, original)
        self.assertEqual(prepared.contract['package']['configuredCatalogueSha256'], catalogue_sha256(original))
        with fixture.export(prepared) as archive:
            catalogue = json.loads(archive.read('lxp-catalogue.json'))
            contract = json.loads(archive.read('lxp-contract.json'))
            self.assertEqual(catalogue_sha256(catalogue), contract['package']['catalogueSha256'])
            self.assertNotEqual(contract['package']['catalogueSha256'], contract['package']['configuredCatalogueSha256'])
            module = catalogue['frameworks'][1]['modules'][0]
            self.assertIn('5', module['courseNumbers'])
            course = next(sco for sco in contract['scos'] if sco['source'].get('courseId') == 'aat-new-five')
            self.assertEqual(course['number'], '5')
            self.assertTrue(any(q['sourceCourseId'] == course['id'] for q in contract['questions']))

    def test_configured_and_derived_hashes_match_when_no_new_slot_needed(self):
        prepared = self.fixture.prepare()
        package = prepared.contract['package']
        self.assertEqual(package['catalogueSha256'], package['configuredCatalogueSha256'])

    def test_reassignment_is_still_explicit_and_source_identity_survives(self):
        fixture = self.fixture
        first = fixture.prepare()
        before = next(s for s in first.contract['scos'] if s['source'].get('originId') == 'tenant-b')
        title = 'AILit1.5 – Verschobener Kurs'
        path = fixture.builds[1] / 'course/de/course.json'
        source = json.loads(path.read_text())
        source['title'] = title
        fixtures.save(path, source)
        fixture.metadata[1]['currentTitle'] = title
        with self.assertRaisesRegex(PackageValidationError, 'ausdrücklich'):
            fixture.prepare()
        moved = fixture.prepare(confirm_reassignment=True)
        after = next(s for s in moved.contract['scos'] if s['source'].get('originId') == 'tenant-b')
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(after['number'], '5')

    def test_duplicate_new_address_and_unknown_module_are_still_rejected(self):
        fixture = self.fixture
        fixture.add_source('new-five', 'AILit1.5 – Fünf', 'tenant-b', 'new-five')
        fixture.add_source('duplicate', 'AILit1.5 – Doppelt', 'tenant-b', 'duplicate')
        with self.assertRaisesRegex(PackageValidationError, 'Duplicate'):
            fixture.prepare()
        fixture.metadata[-1]['currentTitle'] = 'AILit99.5 – Unbekanntes Modul'
        with self.assertRaises(PackageValidationError):
            fixture.prepare()

    def test_changed_configured_catalogue_invalidates_confirmation(self):
        fixture = self.fixture
        fixture.add_source('new-five', 'AILit1.5 – Fünf', 'tenant-b', 'new-five')
        prepared = fixture.prepare()
        fixture.catalogue['revision'] += '-changed'
        with self.assertRaisesRegex(PackageValidationError, 'Catalogue changed'):
            fixture.export(prepared)


class CourseDiscoveryGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui_fixtures.MultiFrameworkGuiTest.setUpClass()

    @classmethod
    def tearDownClass(cls):
        gui_fixtures.MultiFrameworkGuiTest.tearDownClass()

    def setUp(self):
        self.fixture = gui_fixtures.MultiFrameworkGuiTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_gui_resolves_and_exports_new_address_without_manual_slot(self):
        fixture = self.fixture
        cid = fixture.make_course('3' * 24, 'AILit1.5 – Neue Lerneinheit')
        from bson import ObjectId
        course = fixture.db.courses.find_one({'_id': ObjectId(cid)})
        mapping = fixture.module.course_assignment(course)
        self.assertEqual(mapping['courseNumber'], '5')
        location = fixture.prepare(course_id=[cid], required_course=[cid])
        response = fixture.confirm(location)
        self.assertEqual(response.status_code, 200, response.data[:1000])
        self.assertEqual(response.mimetype, 'application/zip')
