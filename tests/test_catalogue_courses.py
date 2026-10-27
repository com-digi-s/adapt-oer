"""Pure course discovery: real catalogue rules, no mocked source identities."""
from copy import deepcopy
import unittest

from lxp_catalogue import PackageValidationError, catalogue_sha256, load_catalogue, normalize_title
from lxp_catalogue_courses import derive_course_catalogue, normalize_course_title


class CatalogueCourseDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = load_catalogue()

    def module(self, catalogue, prefix='AILit', number='1'):
        framework = next(item for item in catalogue['frameworks'] if item['prefix'] == prefix)
        return next(item for item in framework['modules'] if item['number'] == number)

    def test_new_course_slots_are_derived_not_added_to_configuration_or_bound_ids(self):
        before = deepcopy(self.catalogue)
        binding = {'frameworkId': 'fw-ailit-2026', 'prefix': 'AILit', 'version': '2026',
                   'moduleNumber': '1', 'courseNumber': '1'}
        saved_binding = deepcopy(binding)
        derived = derive_course_catalogue(self.catalogue, [
            {'title': 'AILit1.5 – Neuer Kurs'}, {'title': 'AILit1.10 – Zehn'},
            {'title': 'AILit1.1 – Bestehend', 'binding': binding}])
        self.assertEqual(self.module(derived)['courseNumbers'], ['1', '2', '3', '4', '5', '10'])
        self.assertEqual(normalize_title('AILit1.5 – Neuer Kurs', derived)['courseNumber'], '5')
        self.assertNotEqual(catalogue_sha256(derived), catalogue_sha256(self.catalogue))
        self.assertEqual(self.module(derived)['id'], self.module(before)['id'])
        self.assertEqual(self.catalogue, before)
        self.assertEqual(binding, saved_binding)
        with self.assertRaises(PackageValidationError):
            normalize_title('AILit1.5 – Neuer Kurs', self.catalogue)

    def test_input_order_does_not_change_derived_catalogue(self):
        requests = [{'title': title} for title in ('AILit1.100 – Hundert', 'DigComp5.10 – Zehn',
                                                  'AILit1.5 – Fünf', 'AILit1.10 – Zehn')]
        self.assertEqual(derive_course_catalogue(self.catalogue, requests),
                         derive_course_catalogue(self.catalogue, list(reversed(requests))))
        self.assertEqual(self.module(derive_course_catalogue(self.catalogue, requests))['courseNumbers'],
                         ['1', '2', '3', '4', '5', '10', '100'])

    def test_existing_courses_or_empty_request_do_not_change_configured_catalogue(self):
        self.assertEqual(derive_course_catalogue(self.catalogue, []), self.catalogue)
        self.assertEqual(derive_course_catalogue(self.catalogue,
            [{'title': 'AILit1.1 – Bestehend'}, {'title': 'DigComp5.2 – Bestehend'}]), self.catalogue)

    def test_gui_single_title_resolves_new_slot_with_existing_module_identity(self):
        mapping = normalize_course_title('AILit1.5 – Neuer Kurs', self.catalogue)
        self.assertEqual((mapping['frameworkId'], mapping['moduleId'], mapping['courseNumber'], mapping['label']),
                         ('fw-ailit-2026', 'mod-ailit2026-1', '5', 'Neuer Kurs'))

    def test_registered_empty_module_acquires_only_selected_course_addresses(self):
        module = {'id': 'mod-ailit2026-2', 'number': '2', 'label': 'Neues Modul', 'courseNumbers': []}
        self.catalogue['frameworks'][1]['modules'].append(module)
        before = deepcopy(self.catalogue)
        self.assertEqual(load_catalogue(self.catalogue), before)
        derived = derive_course_catalogue(self.catalogue, [{'title': 'AILit2.10 – Zehn'},
                                                         {'title': 'AILit2.5 – Fünf'}])
        self.assertEqual(self.module(derived, number='2'), dict(module, courseNumbers=['5', '10']))
        self.assertEqual(self.module(derived)['courseNumbers'], ['1', '2', '3', '4'])
        self.assertEqual(self.catalogue, before)
        mapping = normalize_title('AILit2.10 – Zehn', derived)
        self.assertEqual((mapping['moduleId'], mapping['moduleLabel']), (module['id'], module['label']))

    def test_new_prefix_must_be_registered_and_retains_its_explicit_module_identity(self):
        with self.assertRaises(PackageValidationError):
            normalize_course_title('Media1.10 – Neuer Kurs', self.catalogue)
        self.catalogue['frameworks'].append({
            'id': 'fw-media-1', 'prefix': 'Media', 'version': '1', 'label': 'Medienkompetenz',
            'modules': [{'id': 'mod-media-1', 'number': '1', 'label': 'Medien verstehen', 'courseNumbers': []}],
            'domains': [],
        })
        before = deepcopy(self.catalogue)
        mapping = normalize_course_title('Media1.10 – Neuer Kurs', self.catalogue)
        self.assertEqual((mapping['frameworkId'], mapping['prefix'], mapping['version'], mapping['moduleId']),
                         ('fw-media-1', 'Media', '1', 'mod-media-1'))
        self.assertEqual(mapping['courseNumber'], '10')
        self.assertEqual(mapping['moduleLabel'], 'Medien verstehen')
        self.assertEqual(self.catalogue, before)
        for title in ('media1.10 – Falsche Schreibung', 'Media2.1 – Unbekanntes Modul'):
            with self.subTest(title=title), self.assertRaises(PackageValidationError):
                normalize_course_title(title, self.catalogue)

    def test_unknown_framework_module_invalid_title_and_duplicate_addresses_fail(self):
        for title in ('AILti1.5 – Tippfehler', 'AILit2.1 – Unbekanntes Modul', 'AILit1.05 – Null',
                      'AILit01.5 – Null', 'AILit1.5.1 – Extra', 'AILit0.5 – Null',
                      'AILit1.5 – ', '1.5 – Nicht freigegebener Altimport'):
            with self.subTest(title=title), self.assertRaises(PackageValidationError):
                derive_course_catalogue(self.catalogue, [{'title': title}])
        with self.assertRaisesRegex(PackageValidationError, 'Duplicate selected'):
            derive_course_catalogue(self.catalogue,
                [{'title': 'AILit1.5 – Einer'}, {'title': 'AILit1.5 – Anderer'}])

    def test_explicit_legacy_mode_derives_only_digcomp_course_numbers(self):
        derived = derive_course_catalogue(self.catalogue, [{'title': '5.10 – Bestehender Altkurs'}], legacy_import=True)
        self.assertEqual(self.module(derived, 'DigComp', '5')['courseNumbers'], ['1', '2', '10'])
        self.assertEqual(self.module(derived)['courseNumbers'], ['1', '2', '3', '4'])

    def test_version_choice_remains_explicit_and_existing_binding_is_not_migrated(self):
        other = deepcopy(self.catalogue['frameworks'][1])
        other.update(id='fw-ailit-future', version='future')
        for module in other['modules']:
            module['id'] += '-future'
        self.catalogue['frameworks'].append(other)
        with self.assertRaisesRegex(PackageValidationError, 'eindeutig'):
            derive_course_catalogue(self.catalogue, [{'title': 'AILit1.5 – Mehrdeutig'}])
        binding = {'prefix': 'AILit', 'version': '2026', 'frameworkId': 'fw-ailit-2026'}
        derived = derive_course_catalogue(self.catalogue, [{'title': 'AILit1.5 – Gebunden', 'binding': binding}])
        future = next(item for item in derived['frameworks'] if item['version'] == 'future')
        self.assertEqual(future['modules'][0]['courseNumbers'], ['1', '2', '3', '4'])
        self.assertEqual(self.module(derived)['courseNumbers'], ['1', '2', '3', '4', '5'])
        with self.assertRaisesRegex(PackageValidationError, 'migration'):
            derive_course_catalogue(self.catalogue, [{'title': 'AILit1.5 – Gebunden', 'binding': binding}],
                                    framework_versions={'AILit': 'future'})

    def test_prefix_change_does_not_inherit_the_old_frameworks_version(self):
        mapping = normalize_course_title('DigComp5.10 – Neu zugeordnet', self.catalogue,
            binding={'prefix': 'AILit', 'version': '2026', 'frameworkId': 'fw-ailit-2026'})
        self.assertEqual((mapping['prefix'], mapping['version']), ('DigComp', '2.2'))

    def test_malformed_discovery_requests_fail_cleanly_without_mutation(self):
        before = deepcopy(self.catalogue)
        for requests in (None, {}, ['AILit1.5 – String'], [{'title': 5}], [{'title': 'AILit1.5 – Test', 'binding': []}],
                         [{'title': 'AILit1.5 – Test', 'unknown': True}]):
            with self.subTest(requests=requests), self.assertRaises(PackageValidationError):
                derive_course_catalogue(self.catalogue, requests)
        for options in ({'framework_versions': []}, {'framework_versions': {'Typo': '1'}}, {'legacy_import': 'yes'}):
            with self.subTest(options=options), self.assertRaises(PackageValidationError):
                derive_course_catalogue(self.catalogue, [{'title': 'AILit1.5 – Test'}], **options)
        self.assertEqual(self.catalogue, before)


class CatalogueRegistrationValidationTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = load_catalogue()

    def test_course_numbers_are_an_explicit_list_even_for_empty_registered_modules(self):
        candidate = deepcopy(self.catalogue)
        candidate['frameworks'][0]['modules'][0]['courseNumbers'] = []
        self.assertEqual(load_catalogue(candidate), candidate)
        for value in (None, '1', ('1',), {}, [1], [True], ['01'], ['0'], ['1', '1'], [[]], [{}]):
            candidate = deepcopy(self.catalogue)
            candidate['frameworks'][0]['modules'][0]['courseNumbers'] = value
            with self.subTest(value=value), self.assertRaises(PackageValidationError):
                load_catalogue(candidate)
        del candidate['frameworks'][0]['modules'][0]['courseNumbers']
        with self.assertRaises(PackageValidationError):
            load_catalogue(candidate)

    def test_only_registered_ascii_letter_prefixes_and_exactly_spelled_versions_are_accepted(self):
        for prefix in ('Media2', '2Media', 'Média', 'Мedia', 'Media ', ' Media', 'Media_Tools', 'Media-Tools', ''):
            candidate = deepcopy(self.catalogue)
            candidate['frameworks'][0]['prefix'] = prefix
            with self.subTest(prefix=prefix), self.assertRaises(PackageValidationError):
                load_catalogue(candidate)
        duplicate = deepcopy(self.catalogue['frameworks'][1])
        duplicate.update(id='fw-ailit-2027', version='2027', modules=[])
        self.catalogue['frameworks'].append(duplicate)
        self.assertEqual(load_catalogue(self.catalogue), self.catalogue)
        duplicate['prefix'] = 'ailit'
        with self.assertRaisesRegex(PackageValidationError, 'letter case'):
            load_catalogue(self.catalogue)

    def test_framework_module_and_domain_ids_are_safe_ascii_identifiers(self):
        for entity in ('framework', 'module', 'domain'):
            for identity in (None, [], 12, '1unsafe', 'bad.id', 'bad:id', 'bad/id', 'bad id', 'ä-id', 'ok\n'):
                candidate = deepcopy(self.catalogue)
                framework = candidate['frameworks'][0]
                target = framework if entity == 'framework' else framework[entity + 's'][0]
                target['id'] = identity
                with self.subTest(entity=entity, identity=identity), self.assertRaises(PackageValidationError):
                    load_catalogue(candidate)
        candidate = deepcopy(self.catalogue)
        candidate['frameworks'][0]['id'] = 'Framework_2-X'
        candidate['frameworks'][0]['modules'][0]['id'] = 'Module_2-X'
        candidate['frameworks'][0]['domains'][0]['id'] = 'Domain_2-X'
        candidate['frameworks'][0]['defaultDomainId'] = 'Domain_2-X'
        self.assertEqual(load_catalogue(candidate), candidate)

    def test_existing_one_letter_framework_module_and_domain_ids_do_not_require_migration(self):
        candidate = deepcopy(self.catalogue)
        framework = candidate['frameworks'][0]
        framework['id'] = 'F'
        framework['modules'][0]['id'] = 'M'
        framework['domains'][0]['id'] = 'D'
        framework['defaultDomainId'] = 'D'
        self.assertEqual(load_catalogue(candidate), candidate)
        mapping = normalize_course_title('DigComp1.10 – Neuer Kurs', candidate)
        self.assertEqual((mapping['frameworkId'], mapping['moduleId']), ('F', 'M'))
        self.assertEqual(candidate['frameworks'][0]['modules'][0]['courseNumbers'], ['1', '2', '3'])

    def test_invalid_catalogue_container_types_fail_with_validation_errors(self):
        changes = [lambda value: value.update(version=True), lambda value: value.update(revision=True),
                   lambda value: value.update(frameworks=[None]),
                   lambda value: value['frameworks'][0].update(modules={}),
                   lambda value: value['frameworks'][0].update(modules=[None]),
                   lambda value: value['frameworks'][0].update(domains={}),
                   lambda value: value['frameworks'][0].update(domains=[None]),
                   lambda value: value['frameworks'][0].update(defaultDomainId=[])]
        for change in changes:
            candidate = deepcopy(self.catalogue)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(PackageValidationError):
                load_catalogue(candidate)


if __name__ == '__main__':
    unittest.main()
