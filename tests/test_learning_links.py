"""Local Markdown reading recommendations resolve to selected framework pages."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from zipfile import ZipFile

from lxp_catalogue import PackageValidationError
from lxp_multi_export import tree_manifest, validate_product_contract
import test_multi_framework_export as fixtures


class LearningLinksTest(unittest.TestCase):
    setUp = fixtures.MixedPackageTest.setUp
    write_wrapper = fixtures.MixedPackageTest.write_wrapper
    add_source = fixtures.MixedPackageTest.add_source
    prepare = fixtures.MixedPackageTest.prepare
    export = fixtures.MixedPackageTest.export

    def recommendation(self, *, core=False, metadata=None):
        # The wrapper must explicitly declare this opt-in runtime feature.
        with ZipFile(self.wrapper) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        capability = json.loads(files['adapt-multi-framework-capabilities.json'])
        capability['learningLinks'] = 1
        files['adapt-multi-framework-capabilities.json'] = json.dumps(capability).encode()
        with ZipFile(self.wrapper, 'w') as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        path = 'course/de/assets/weiterlernen.md'
        values = {'framework': 'DigComp', 'frameworkVersion': '2.2', 'course': '1.1', 'pageId': 'page',
                  'legacyPageId': 'original-page'}
        values.update(metadata or {})
        text = '---\n' + ''.join('lxp:' + key + ': ' + json.dumps(value) + '\n' for key, value in values.items())
        (self.builds[1] / path).write_text(text + '---\n\n# Vollständige lokale Lerneinheit\n')
        file = self.builds[1] / 'course/de/components.json'
        components = json.loads(file.read_text())
        if core:
            components[0]['body'] += '<a href="' + path + '" target="_blank" download>Weiterlernen</a>'
        else:
            components[0]['_resources'] = [{'_source': path, '_kind': 'link', '_downloadName': 'weiterlernen.md'}]
            components[0]['body'] += '<a href="#" data-markledge-resource="0">Weiterlernen</a>'
        fixtures.save(file, components)
        return path

    def test_full_and_core_use_same_exact_metadata_without_mutating_source_or_learning_json(self):
        for core in (False, True):
            with self.subTest(core=core):
                resource = self.recommendation(core=core)
                originals = [tree_manifest(path) for path in self.builds]
                prepared = self.prepare()
                links = prepared.contract['learningLinks']
                self.assertEqual(len(links), 1)
                source = next(s for s in prepared.contract['scos'] if s['role'] == 'learning' and s['frameworkId'] == 'fw-ailit-2026')
                target = next(s for s in prepared.contract['scos'] if s['role'] == 'learning' and s['frameworkId'] == 'fw-digcomp-22')
                self.assertEqual({k: links[0][k] for k in ('sourceScoId', 'resourcePath', 'targetScoId', 'contentId')},
                    {'sourceScoId': source['id'], 'resourcePath': resource, 'targetScoId': target['id'], 'contentId': 'page'})
                with self.export(prepared) as archive:
                    delivered = archive.read(source['launch']['root'] + '/course/de/components.json')
                    self.assertEqual(delivered, (self.builds[1] / 'course/de/components.json').read_bytes())
                self.assertEqual(originals, [tree_manifest(path) for path in self.builds])

    def test_missing_target_fails_preflight_instead_of_emitting_a_dead_download(self):
        self.recommendation()
        with self.assertRaisesRegex(PackageValidationError, 'fehlt in der Kursauswahl'):
            self.prepare(builds=self.builds[1:], source_metadata=self.metadata[1:])

    def test_unrelated_malformed_external_link_does_not_become_a_local_recommendation_error(self):
        self.recommendation(core=True)
        file = self.builds[1] / 'course/de/components.json'
        values = json.loads(file.read_text())
        values[0]['body'] += '<a href="https://]https://www.nist.gov/example">Historical external source</a>'
        fixtures.save(file, values)
        self.assertEqual(len(self.prepare().contract['learningLinks']), 1)

    def test_local_unsafe_recommendation_paths_still_fail_closed(self):
        from lxp_learning_links import _resource
        for target in ('../outside.md', '/absolute.md', 'course/%2fescape.md'):
            with self.subTest(target=target), self.assertRaises(PackageValidationError):
                _resource(target)

    def test_legacy_id_is_not_a_fallback_and_unavailable_pages_fail(self):
        self.recommendation(metadata={'pageId': 'old-page'})
        with self.assertRaisesRegex(PackageValidationError, 'alte IDs werden nicht geraten'):
            self.prepare()
        self.recommendation()
        file = self.builds[0] / 'course/de/contentObjects.json'
        pages = json.loads(file.read_text())
        pages[0]['_isAvailable'] = False
        fixtures.save(file, pages)
        with self.assertRaisesRegex(PackageValidationError, 'nicht verfügbar'):
            self.prepare()

    def test_framework_version_and_duplicate_known_keys_fail_closed(self):
        self.recommendation(metadata={'frameworkVersion': 'future'})
        with self.assertRaisesRegex(PackageValidationError, 'fehlt in der Kursauswahl'):
            self.prepare()
        path = self.recommendation()
        file = self.builds[1] / path
        file.write_text(file.read_text().replace('---\n\n#', 'lxp:pageId: "other"\n---\n\n#'))
        with self.assertRaisesRegex(PackageValidationError, 'Doppeltes'):
            self.prepare()

    def test_old_wrapper_rejected_only_when_learning_links_are_needed(self):
        self.recommendation()
        self.write_wrapper()
        with self.assertRaisesRegex(PackageValidationError, 'aktuellen Wrapper'):
            self.prepare()

    def test_unknown_metadata_does_not_create_a_destination_or_execute_yaml(self):
        path = self.recommendation()
        file = self.builds[1] / path
        file.write_text('---\nlxp:unlistedKey: !!python/object:danger\nschema:name: "Ordinary attachment"\n---\n# Material\n')
        self.write_wrapper()  # Ordinary materials still work with old wrappers.
        self.assertEqual(self.prepare().contract['learningLinks'], [])
        self.recommendation()
        file.write_text(file.read_text().replace('lxp:pageId: "page"\n', ''))
        with self.assertRaisesRegex(PackageValidationError, 'aktuelle Seiten-ID'):
            self.prepare()

    def test_query_and_fragment_keep_the_physical_resource_identity_and_bad_encoding_is_clear(self):
        resource = self.recommendation(core=True)
        file = self.builds[1] / 'course/de/components.json'
        values = json.loads(file.read_text())
        values[0]['body'] = values[0]['body'].replace(resource, resource + '?download=1#section')
        fixtures.save(file, values)
        self.assertEqual(self.prepare().contract['learningLinks'][0]['resourcePath'], resource)
        (self.builds[1] / resource).write_bytes(b'\xff\xfe\x00')
        with self.assertRaisesRegex(PackageValidationError, 'UTF-8-Text'):
            self.prepare()

    def test_delivered_mapping_cannot_disagree_with_bound_resource_metadata(self):
        self.recommendation()
        prepared = self.prepare()
        with self.export(prepared) as archive:
            root = self.root / 'delivered'
            archive.extractall(root)
        contract = json.loads((root / 'lxp-contract.json').read_text())
        self.assertTrue(validate_product_contract(contract, root))
        bad = deepcopy(contract)
        bad['learningLinks'][0]['contentId'] = 'nonexistent'
        with self.assertRaisesRegex(PackageValidationError, 'Weiterlern-Verknüpfungen'):
            validate_product_contract(bad, root)
        bad = deepcopy(contract)
        bad['learningLinks'][0]['resourceSha256'] = '0' * 64
        with self.assertRaisesRegex(PackageValidationError, 'Weiterlern-Verknüpfungen'):
            validate_product_contract(bad, root)


if __name__ == '__main__':
    unittest.main()
