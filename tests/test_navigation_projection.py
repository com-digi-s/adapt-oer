"""Subset projection must distinguish excluded courses from broken references.

Use real small published builds and the normal preparation/source registry, not
mocked source models. Only this module's TestCase is collected by discovery.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from lxp_catalogue import PackageValidationError
from lxp_navigation import project_navigation, source_key, verify_navigation_inputs
import test_multi_framework_export as fixtures


class NavigationProjectionTest(unittest.TestCase):
    write_wrapper = fixtures.MixedPackageTest.write_wrapper
    add_source = fixtures.MixedPackageTest.add_source
    prepare = fixtures.MixedPackageTest.prepare

    def setUp(self):
        fixtures.MixedPackageTest.setUp(self)
        self.prepared = self.prepare()
        self.sources = self.prepared._sources
        self.inventory = [{'build': build, 'metadata': deepcopy(metadata)}
                          for build, metadata in zip(self.builds, self.metadata)]
        self.source_registry = json.loads(self.registry.read_text(encoding='utf-8'))
        self.dig = next(item for item in self.sources if item['metadata']['originId'] == 'tenant-a')
        self.ai = next(item for item in self.sources if item['metadata']['originId'] == 'tenant-b')
        self.navigation_catalogue = deepcopy(self.catalogue)
        self.navigation_catalogue['pathways'] = [
            {'id': 'path-dig', 'frameworkId': self.dig['sco']['frameworkId'], 'label': 'Digitaler Lernweg',
             'courseRefs': [self.reference(self.dig)]},
            {'id': 'path-ai', 'frameworkId': self.ai['sco']['frameworkId'], 'label': 'KI-Lernweg',
             'courseIds': [self.ai['sco']['id']]},
        ]
        self.navigation_catalogue['glossary'] = [
            {'id': 'term-dig', 'frameworkId': self.dig['sco']['frameworkId'], 'term': 'Digitaler Begriff',
             'definition': '<p>Definition bleibt unverändert.</p>',
             'targets': [{**self.reference(self.dig), 'contentId': 'same-component'}]},
            {'id': 'term-ai', 'frameworkId': self.ai['sco']['frameworkId'], 'term': 'KI-Begriff',
             'targets': [{'scoId': self.ai['sco']['id'], 'contentId': 'page'}]},
        ]

    @staticmethod
    def reference(source):
        return {key: source['metadata'][key] for key in ('originId', 'courseId')}

    def project(self, catalogue=None, selected=None, available='default', registry=None):
        return project_navigation(
            self.navigation_catalogue if catalogue is None else catalogue,
            self.sources if selected is None else selected,
            self.inventory if available == 'default' else available,
            self.source_registry if registry is None else registry,
        )

    def test_complete_selection_preserves_all_destinations_and_labels(self):
        result = self.project()
        self.assertEqual(result['pathways'], [
            {'id': 'path-dig', 'frameworkId': self.dig['sco']['frameworkId'], 'label': 'Digitaler Lernweg',
             'courseIds': [self.dig['sco']['id']]},
            {'id': 'path-ai', 'frameworkId': self.ai['sco']['frameworkId'], 'label': 'KI-Lernweg',
             'courseIds': [self.ai['sco']['id']]},
        ])
        self.assertEqual(result['glossary'][0]['targets'], [{'scoId': self.dig['sco']['id'], 'contentId': 'same-component'}])
        self.assertEqual(result['glossary'][0]['definition'], '<p>Definition bleibt unverändert.</p>')
        self.assertEqual(result['glossary'][1]['targets'], [{'scoId': self.ai['sco']['id'], 'contentId': 'page'}])
        for kind in ('pathways', 'glossary'):
            self.assertEqual(result['projection'][kind], {'original': 2, 'retained': 2, 'shortened': 0, 'omitted': 0})
        self.assertEqual(result['projection']['details'], [])
        self.assertEqual(result['inputs'], [])
        self.assertEqual(result['evidence'], [])

    def test_ai_only_selection_omits_known_digcomp_pathway_and_term(self):
        result = self.project(selected=[self.ai])
        self.assertEqual([item['id'] for item in result['pathways']], ['path-ai'])
        self.assertEqual([item['id'] for item in result['glossary']], ['term-ai'])
        for kind in ('pathways', 'glossary'):
            self.assertEqual(result['projection'][kind], {'original': 2, 'retained': 1, 'shortened': 0, 'omitted': 1})
        self.assertEqual({item['id'] for item in result['projection']['details']}, {'path-dig', 'term-dig'})
        self.assertTrue(all(item['status'] == 'omitted' for item in result['projection']['details']))
        self.assertEqual(len(result['inputs']), 1, 'One excluded source is frozen only once for both references')
        self.assertEqual(result['inputs'][0]['original'], self.builds[0])
        evidence = result['evidence'][0]
        self.assertEqual(evidence['originId'], 'tenant-a')
        self.assertEqual(evidence['scoId'], self.dig['sco']['id'])
        self.assertEqual(evidence['publishedTitle'], self.metadata[0]['currentTitle'])
        self.assertEqual(len(evidence['jsonSha256']), 6)
        verify_navigation_inputs(result['inputs'])

    def test_same_framework_multi_target_pathway_and_term_are_shortened(self):
        self.add_source('digcomp-two', 'DigComp1.2 – Zweiter digitaler Kurs', 'tenant-a', 'aat-2')
        prepared = self.prepare()
        sources = prepared._sources
        second = next(item for item in sources if item['metadata']['courseId'] == 'aat-2')
        inventory = [{'build': build, 'metadata': deepcopy(metadata)}
                     for build, metadata in zip(self.builds, self.metadata)]
        registry = json.loads(self.registry.read_text(encoding='utf-8'))
        catalogue = deepcopy(self.navigation_catalogue)
        catalogue['pathways'][0]['courseRefs'].append(self.reference(second))
        catalogue['glossary'][0]['targets'].append({'scoId': second['sco']['id'], 'contentId': 'article'})
        selected = [item for item in sources if item['metadata']['courseId'] != 'aat-2']
        result = self.project(catalogue, selected, inventory, registry)
        self.assertEqual(result['pathways'][0]['courseIds'], [self.dig['sco']['id']])
        self.assertEqual(result['glossary'][0]['targets'], [{'scoId': self.dig['sco']['id'], 'contentId': 'same-component'}])
        self.assertEqual([item['id'] for item in result['pathways']], ['path-dig', 'path-ai'])
        for kind in ('pathways', 'glossary'):
            self.assertEqual(result['projection'][kind], {'original': 2, 'retained': 2, 'shortened': 1, 'omitted': 0})
        self.assertEqual(len(result['inputs']), 1)
        self.assertEqual(result['inputs'][0]['metadata']['courseId'], 'aat-2')
        self.assertEqual({(item['originalCount'], item['retainedCount'], item['status'])
                          for item in result['projection']['details']}, {(2, 1, 'shortened')})

    def test_projection_deepcopies_without_mutating_catalogue_inventory_or_registry(self):
        before = deepcopy(self.navigation_catalogue)
        inventory_before = deepcopy(self.inventory)
        registry_before = deepcopy(self.source_registry)
        result = self.project(selected=[self.ai])
        result['pathways'][0]['label'] = 'Changed result'
        result['glossary'][0]['targets'].clear()
        result['inputs'][0]['metadata']['originId'] = 'Changed evidence'
        self.assertEqual(self.navigation_catalogue, before)
        self.assertEqual(self.inventory, inventory_before)
        self.assertEqual(self.source_registry, registry_before)

    def test_unknown_source_or_sco_is_never_treated_as_intentionally_excluded(self):
        for reference in [{'originId': 'unknown', 'courseId': 'absent'}, 'sco-never-registered']:
            for kind in ('pathways', 'glossary'):
                catalogue = deepcopy(self.navigation_catalogue)
                if kind == 'pathways':
                    catalogue[kind][0]['courseRefs'] = [reference]
                else:
                    catalogue[kind][0]['targets'] = [({'scoId': reference} if isinstance(reference, str) else reference)
                                                     | {'contentId': 'same-component'}]
                with self.subTest(reference=reference, kind=kind), self.assertRaisesRegex(
                        PackageValidationError, 'unknown or unavailable'):
                    self.project(catalogue, selected=[self.ai])

    def test_foreign_framework_target_is_rejected_even_when_excluded(self):
        for kind in ('pathways', 'glossary'):
            catalogue = deepcopy(self.navigation_catalogue)
            if kind == 'pathways':
                catalogue[kind][1]['courseIds'] = [self.dig['sco']['id']]
            else:
                catalogue[kind][1]['targets'] = [{'scoId': self.dig['sco']['id'], 'contentId': 'same-component'}]
            with self.subTest(kind=kind), self.assertRaisesRegex(PackageValidationError, 'foreign-framework'):
                self.project(catalogue, selected=[self.ai])

    def test_missing_content_id_is_rejected_for_selected_and_excluded_courses(self):
        for selected in (self.sources, [self.ai]):
            for content_id in ('does-not-exist', None, 1):
                catalogue = deepcopy(self.navigation_catalogue)
                catalogue['glossary'][0]['targets'][0]['contentId'] = content_id
                with self.subTest(selected_count=len(selected), content_id=content_id), self.assertRaisesRegex(
                        PackageValidationError, 'content ID does not exist'):
                    self.project(catalogue, selected=selected)

    def test_excluded_reference_requires_explicit_inventory_even_when_registry_knows_it(self):
        self.assertIn(source_key(self.dig['metadata']), self.source_registry['sources'])
        for inventory in (None, [], [self.inventory[1]]):
            with self.subTest(inventory=inventory), self.assertRaisesRegex(PackageValidationError, 'unknown or unavailable'):
                self.project(selected=[self.ai], available=inventory)

    def test_complete_selection_does_not_need_excluded_source_inventory(self):
        self.assertEqual(len(self.project(available=None)['pathways']), 2)

    def test_all_six_relevant_json_snapshots_invalidate_confirmation_on_change(self):
        result = self.project(selected=[self.ai])
        relative_paths = result['inputs'][0]['hashes']
        self.assertEqual(set(relative_paths), {'course/config.json', 'course/de/course.json',
                                             'course/de/contentObjects.json', 'course/de/articles.json',
                                             'course/de/blocks.json', 'course/de/components.json'})
        for relative in relative_paths:
            path = self.builds[0] / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n')
                with self.subTest(relative=relative), self.assertRaisesRegex(PackageValidationError, 'changed since preview'):
                    verify_navigation_inputs(result['inputs'])
            finally:
                path.write_bytes(original)
        verify_navigation_inputs(result['inputs'])

    def test_missing_snapshot_json_invalidates_confirmation(self):
        result = self.project(selected=[self.ai])
        path = self.builds[0] / 'course/de/components.json'
        original = path.read_bytes()
        try:
            path.unlink()
            with self.assertRaisesRegex(PackageValidationError, 'changed since preview'):
                verify_navigation_inputs(result['inputs'])
        finally:
            path.write_bytes(original)

    def test_unrelated_assets_and_runtime_in_excluded_course_do_not_block(self):
        result = self.project(selected=[self.ai])
        (self.builds[0] / 'course/de/assets/test.svg').write_text('<svg>replaced excluded asset</svg>', encoding='utf-8')
        (self.builds[0] / 'adapt/js/adapt.min.js').write_text('updated excluded runtime', encoding='utf-8')
        (self.builds[0] / 'index_lms.html').write_text('updated excluded entry', encoding='utf-8')
        verify_navigation_inputs(result['inputs'])
        projected_again = self.project(selected=[self.ai])
        self.assertEqual(projected_again['projection'], result['projection'])
        self.assertEqual(projected_again['evidence'], result['evidence'])

    def test_duplicate_navigation_ids_are_rejected(self):
        for kind in ('pathways', 'glossary'):
            catalogue = deepcopy(self.navigation_catalogue)
            catalogue[kind].append(deepcopy(catalogue[kind][0]))
            with self.subTest(kind=kind), self.assertRaisesRegex(PackageValidationError, 'duplicate'):
                self.project(catalogue)

    def test_invalid_navigation_metadata_and_collection_shapes_are_rejected(self):
        for kind, label_key, refs_key in [('pathways', 'label', 'courseRefs'), ('glossary', 'term', 'targets')]:
            mutations = [lambda c: c.update({kind: {}}), lambda c: c[kind].__setitem__(0, None),
                         lambda c: c[kind][0].update(id=''), lambda c: c[kind][0].update(frameworkId='missing'),
                         lambda c: c[kind][0].update({label_key: '   '}),
                         lambda c: c[kind][0].update({refs_key: []}),
                         lambda c: c[kind][0].update({refs_key: 'not-a-list'})]
            for mutate in mutations:
                catalogue = deepcopy(self.navigation_catalogue)
                mutate(catalogue)
                with self.subTest(kind=kind, mutation=mutate), self.assertRaises(PackageValidationError):
                    self.project(catalogue)

    def test_invalid_or_duplicate_inventory_metadata_is_rejected(self):
        inventories = [
            {}, [{'metadata': self.metadata[0]}], [{'build': self.builds[0], 'metadata': None}],
            [{'build': self.builds[0], 'metadata': {'originId': ' ', 'courseId': 'aat-1'}}],
            [self.inventory[0], self.inventory[0]],
        ]
        for inventory in inventories:
            with self.subTest(inventory=inventory), self.assertRaises(PackageValidationError):
                self.project(selected=[self.ai], available=inventory)

    def test_ambiguous_selected_build_and_registered_sco_identity_are_rejected(self):
        inventory = deepcopy(self.inventory)
        inventory[0]['build'] = self.builds[1]
        with self.assertRaisesRegex(PackageValidationError, 'Ambiguous selected'):
            self.project(available=inventory)
        registry = deepcopy(self.source_registry)
        registry['sources'][source_key(self.ai['metadata'])]['scoId'] = self.dig['sco']['id']
        with self.assertRaisesRegex(PackageValidationError, 'Ambiguous registered'):
            self.project(registry=registry)

    def test_unauthorized_native_excluded_source_is_rejected(self):
        inventory = deepcopy(self.inventory)
        inventory[0]['metadata']['kind'] = 'native-registered'
        with self.assertRaisesRegex(PackageValidationError, 'not explicitly authorized'):
            self.project(selected=[self.ai], available=inventory)

    def test_excluded_source_cannot_claim_another_aat_course_id(self):
        fixtures.save(self.builds[0] / 'course/config.json', {'_defaultLanguage': 'de', '_courseId': 'someone-else'})
        with self.assertRaisesRegex(PackageValidationError, 'authenticated AAT source ID'):
            self.project(selected=[self.ai])

    def test_excluded_source_assignment_change_requires_explicit_confirmation(self):
        fixtures.save(self.builds[0] / 'course/de/course.json', {'_id': 'course', 'title': 'DigComp1.2 – Changed address'})
        with self.assertRaisesRegex(PackageValidationError, 'unconfirmed assignment change'):
            self.project(selected=[self.ai])


if __name__ == '__main__':
    unittest.main()
