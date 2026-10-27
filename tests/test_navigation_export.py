"""Real export/GUI gates for subset navigation and excluded-source evidence."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import unittest
from zipfile import ZipFile

from bson import ObjectId

from lxp_catalogue import PackageValidationError
from lxp_export import build_multi_framework_export
import test_multi_framework_export as export_fixtures
import test_gui_multi_framework as gui_fixtures


class NavigationExportTest(unittest.TestCase):
    write_wrapper = export_fixtures.MixedPackageTest.write_wrapper
    add_source = export_fixtures.MixedPackageTest.add_source
    prepare = export_fixtures.MixedPackageTest.prepare

    def setUp(self):
        export_fixtures.MixedPackageTest.setUp(self)
        dig, ai = self.catalogue['frameworks']
        self.catalogue['pathways'] = [
            {'id': 'path-dig', 'frameworkId': dig['id'], 'label': 'DigComp-Lernweg',
             'courseRefs': [{'originId': 'tenant-a', 'courseId': 'aat-1'}]},
            {'id': 'path-ai', 'frameworkId': ai['id'], 'label': 'AILit-Lernweg',
             'courseRefs': [{'originId': 'tenant-b', 'courseId': 'aat-1'}]},
        ]
        self.catalogue['glossary'] = [
            {'id': 'term-dig', 'frameworkId': dig['id'], 'term': 'Digitaler Begriff',
             'targets': [{'originId': 'tenant-a', 'courseId': 'aat-1', 'contentId': 'same-component'}]},
        ]
        self.complete = self.prepare()  # Register both stable source identities.
        self.inventory = [{'build': build, 'metadata': deepcopy(metadata)}
                          for build, metadata in zip(self.builds, self.metadata)]

    def subset(self, **changes):
        options = {'builds': [self.builds[1]], 'source_metadata': [self.metadata[1]],
                   'navigation_sources': self.inventory}
        options.update(changes)
        return self.prepare(**options)

    @staticmethod
    def build(prepared):
        return build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256)

    def test_subset_build_delivers_only_selected_destinations_and_bound_evidence(self):
        prepared = self.subset()
        excluded = next(item for item in self.complete.contract['scos']
                        if item['role'] == 'learning' and item['frameworkId'] == 'fw-digcomp-22')
        with ZipFile(self.build(prepared)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            provenance = json.loads(archive.read('package-provenance.json'))
            self.assertEqual([item['id'] for item in contract['pathways']], ['path-ai'])
            self.assertEqual(contract['glossary'], [])
            self.assertTrue(all(item['frameworkId'] == 'fw-ailit-2026' for item in contract['scos']))
            self.assertFalse(any(name.startswith(excluded['launch']['root'] + '/') for name in archive.namelist()))
            self.assertEqual(provenance['navigationProjection'], prepared.preview['navigationProjection'])
            self.assertEqual(provenance['navigationSources'], prepared._navigation['evidence'])
            self.assertEqual(provenance['navigationSources'][0]['scoId'], excluded['id'])

    def test_relevant_excluded_json_mutation_blocks_confirmed_export(self):
        prepared = self.subset()
        for relative in prepared._navigation['inputs'][0]['hashes']:
            path = self.builds[0] / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n')
                with self.subTest(relative=relative), self.assertRaisesRegex(PackageValidationError, 'Navigation source changed'):
                    self.build(prepared)
            finally:
                path.write_bytes(original)

    def test_prepared_projection_or_source_evidence_tampering_blocks_export(self):
        mutations = [
            lambda p: p._navigation['projection']['pathways'].update(omitted=999),
            lambda p: p._navigation['evidence'][0].update(publishedTitle='Manipulated evidence'),
            lambda p: p._navigation['evidence'][0]['jsonSha256'].update({'course/de/course.json': '0' * 64}),
        ]
        for mutate in mutations:
            prepared = self.subset()
            mutate(prepared)
            with self.subTest(mutation=mutate), self.assertRaisesRegex(PackageValidationError, 'Prepared contract/implementation changed'):
                self.build(prepared)

    def test_unrelated_excluded_asset_and_runtime_mutation_does_not_block_export(self):
        prepared = self.subset()
        marker = b'EXCLUDED-ASSET-MUST-NOT-BE-EXPORTED'
        (self.builds[0] / 'course/de/assets/test.svg').write_bytes(marker)
        (self.builds[0] / 'adapt/js/adapt.min.js').write_text('unrelated excluded runtime update', encoding='utf-8')
        with ZipFile(self.build(prepared)) as archive:
            self.assertNotIn(marker, [archive.read(name) for name in archive.namelist() if name.endswith('.svg')])

    def test_missing_excluded_inventory_or_broken_content_still_blocks_preparation(self):
        with self.assertRaisesRegex(PackageValidationError, 'unknown or unavailable'):
            self.subset(navigation_sources=[])
        catalogue = deepcopy(self.catalogue)
        catalogue['glossary'][0]['targets'][0]['contentId'] = 'missing-content'
        with self.assertRaisesRegex(PackageValidationError, 'content ID does not exist'):
            self.subset(catalogue=catalogue)

    def test_inventory_order_does_not_change_the_confirmed_preflight(self):
        first = self.subset()
        second = self.subset(navigation_sources=list(reversed(self.inventory)))
        self.assertEqual(first.preflight_sha256, second.preflight_sha256)


class NavigationGuiExportTest(unittest.TestCase):
    make_course = gui_fixtures.MultiFrameworkGuiTest.make_course
    token = gui_fixtures.MultiFrameworkGuiTest.token
    prepare = gui_fixtures.MultiFrameworkGuiTest.prepare
    confirm = gui_fixtures.MultiFrameworkGuiTest.confirm

    @classmethod
    def setUpClass(cls):
        gui_fixtures.MultiFrameworkGuiTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        gui_fixtures.MultiFrameworkGuiTest.tearDownClass.__func__(cls)

    def setUp(self):
        previous_config = self.module.app.config.copy()
        gui_fixtures.MultiFrameworkGuiTest.setUp(self)

        def restore_config():
            self.module.app.config.clear()
            self.module.app.config.update(previous_config)

        def close_previews():
            for value in list(self.module.app.extensions['lxp_previews'].values()):
                value['prepared'].close()
            self.module.app.extensions['lxp_previews'].clear()

        self.addCleanup(restore_config)
        self.addCleanup(close_previews)
        self.module.app.config['LXP_CATALOGUE_STORE_PATH'] = str(self.root / 'catalogue-override.json')
        self.catalogue['pathways'] = [
            {'id': 'path-dig', 'frameworkId': 'fw-digcomp-22', 'label': 'DigComp-Lernweg',
             'courseRefs': [{'originId': 'gui-tenant', 'courseId': self.dig}]},
            {'id': 'path-ai', 'frameworkId': 'fw-ailit-2026', 'label': 'AILit-Lernweg',
             'courseRefs': [{'originId': 'gui-tenant', 'courseId': self.ai}]},
        ]
        self.catalogue['glossary'] = [
            {'id': 'term-dig', 'frameworkId': 'fw-digcomp-22', 'term': 'Digitaler Begriff',
             'targets': [{'originId': 'gui-tenant', 'courseId': self.dig, 'contentId': 'question'}]},
        ]

    def subset(self):
        return self.prepare(course_id=[self.ai], required_course=[self.ai])

    def test_unshared_excluded_reference_cannot_create_preview_or_zip(self):
        self.db.courses.update_one({'_id': ObjectId(self.dig)}, {'$set': {'_isShared': False}})
        response = self.client.post('/lxp/export', data={
            'csrf_token': self.token(), 'title': 'Nicht freigegebener Verweis',
            'course_id': [self.ai], 'required_course': [self.ai],
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertIn('unknown or unavailable', response.data.decode())
        self.assertEqual(self.db.lxp_export_previews.count_documents({}), 0)
        self.assertEqual(self.db.lxp_export_profiles.count_documents({}), 0)

    def test_revoking_excluded_access_after_preview_blocks_confirmation_without_zip(self):
        location = self.subset()
        html = self.client.get(location).data
        import re
        confirmation = {
            'csrf_token': re.search(rb'name="csrf_token" value="([^"]+)"', html)[1].decode(),
            'preflight_sha256': re.search(rb'name="preflight_sha256" value="([^"]+)"', html)[1].decode()}
        self.db.courses.update_one({'_id': ObjectId(self.dig)}, {'$set': {'_isShared': False}})
        self.assertEqual(self.client.get(location).status_code, 410)
        response = self.client.post(location.replace('/preview/', '/confirm/'), data=confirmation)
        self.assertEqual(response.status_code, 410)
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertEqual(self.db.lxp_export_profiles.find_one()['revision'], 0)

    def test_worker_restart_reconstructs_same_subset_hash_and_delivers_one_use_zip(self):
        location = self.subset()
        key = location.rsplit('/', 1)[1]
        previous = self.module.app.extensions['lxp_previews'].pop(key)
        old_hash = previous['prepared'].preflight_sha256
        old_projection = deepcopy(previous['prepared']._navigation['projection'])
        old_evidence = deepcopy(previous['prepared']._navigation['evidence'])
        previous['prepared'].close()
        preview = self.client.get(location)
        self.assertEqual(preview.status_code, 200, preview.data.decode())
        reconstructed = self.module.app.extensions['lxp_previews'][key]['prepared']
        self.assertEqual(reconstructed.preflight_sha256, old_hash)
        self.assertEqual(reconstructed._navigation['projection'], old_projection)
        self.assertEqual(reconstructed._navigation['evidence'], old_evidence)
        self.assertIn(old_hash.encode(), preview.data)
        response = self.confirm(location)
        self.assertEqual(response.mimetype, 'application/zip')
        with ZipFile(BytesIO(response.data)) as archive:
            contract = json.loads(archive.read('lxp-contract.json'))
            self.assertEqual([item['id'] for item in contract['pathways']], ['path-ai'])
            self.assertEqual(contract['glossary'], [])
        self.assertEqual(self.client.get(location).status_code, 410)

    def test_worker_restart_after_excluded_source_change_invalidates_saved_preview(self):
        location = self.subset()
        previous = self.module.app.extensions['lxp_previews'].pop(location.rsplit('/', 1)[1])
        previous['prepared'].close()
        path = self.root / 'builds' / self.dig / 'build/course/de/components.json'
        path.write_bytes(path.read_bytes() + b'\n')
        response = self.client.get(location)
        self.assertEqual(response.status_code, 410)
        self.assertNotEqual(response.mimetype, 'application/zip')
        self.assertIn('neue Vorschau', response.data.decode())

    def test_unrelated_excluded_asset_change_still_allows_gui_confirmation(self):
        location = self.subset()
        asset = self.root / 'builds' / self.dig / 'build/course/de/assets/new.txt'
        asset.parent.mkdir(parents=True)
        asset.write_text('new excluded asset', encoding='utf-8')
        response = self.confirm(location)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')


if __name__ == '__main__':
    unittest.main()
