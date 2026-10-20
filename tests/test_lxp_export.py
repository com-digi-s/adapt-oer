from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxp_export import package_courses, PackageValidationError


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class PublishedPackageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.build = self.root / 'published'
        self.wrapper = self.root / 'wrapper.zip'
        self.template = Path('static/assessment-template.zip')
        self.config = {'SCOS': {'M0': {'id': 'M0'}, 'MX': {'id': 'MX'}, 'M4': {'id': 'M4', 'label': 'Module four', 'courses': [{'id': '1', 'label': 'Other'}, {'id': '2', 'label': 'OLD LABEL'}]}, 'M1': {'id': 'M1', 'courses': [{'id': '1'}]}}}
        with ZipFile(self.wrapper, 'w') as archive:
            archive.writestr('index.html', '<html></html>')
            archive.writestr('config.json', json.dumps(self.config))
        save(self.build / 'course/config.json', {'_defaultLanguage': 'de'})
        save(self.build / 'course/de/course.json', {'_id': 'course', 'title': '4.2 Actual pilot course'})
        self.data = {
            'contentObjects': [{'_id': 'page', '_parentId': 'course', '_type': 'page', 'title': 'Page'}],
            'articles': [{'_id': 'article', '_parentId': 'page', 'title': 'Article'}],
            'blocks': [{'_id': 'block', '_parentId': 'article'}],
            'components': [{'_id': 'q', '_parentId': 'block', '_component': 'mcq', '_classes': 'et at', 'body': '<p>Visible stem</p><img src="course/de/assets/picture.svg">', '_items': [{'text': 'Yes', '_shouldBeSelected': True}, {'text': 'No', '_shouldBeSelected': False}], '_onScreen': {}}],
        }
        assets = self.build / 'course/de/assets'
        assets.mkdir(parents=True)
        (assets / 'picture.svg').write_text('<svg/>')
        self.write_data()

    def write_data(self):
        for name, values in self.data.items():
            save(self.build / ('course/de/' + name + '.json'), values)

    def export(self, **kwargs):
        return ZipFile(package_courses([self.build], self.wrapper, self.template, **kwargs))

    def test_real_published_language_flat_mapping_and_both_tests(self):
        with self.export() as archive:
            self.assertIn('scos/M4/2/course/de/course.json', archive.namelist())
            report = json.loads(archive.read('package-provenance.json'))
            self.assertEqual(report['tests'], {'M0': {'questions': 1, 'banks': [421]}, 'MX': {'questions': 1, 'banks': [421]}})
            self.assertEqual(report['courses'][0]['questions'][0]['domain'], 'flat-course-default:Verstehen')
            config = json.loads(archive.read('config.json'))
            self.assertEqual(config['SCOS']['M4']['courses'][1]['label'], '4.2 Actual pilot course')
            self.assertFalse(config['SCOS']['M4']['disabled'])
            self.assertTrue(config['SCOS']['M4']['courses'][0]['disabled'])
            self.assertTrue(config['SCOS']['M1']['disabled'])
            question = json.loads(archive.read('scos/M0/course/en/components.json'))[0]
            self.assertIn('course/en/assets/course1/course/picture.svg', question['body'])
            self.assertIn('scos/M0/course/en/assets/course1/course/picture.svg', archive.namelist())
            blocks = json.loads(archive.read('scos/M0/course/en/blocks.json'))
            self.assertEqual(blocks[0]['_assessment']['_quizBankID'], 421)
            self.assertNotIn('_assessment', blocks[-1])

    def test_does_not_modify_published_build_or_wrapper(self):
        def hashes():
            return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob('*') if path.is_file()}
        before = hashes()
        with self.export():
            pass
        self.assertEqual(hashes(), before)

    def test_title_only_prompt_becomes_visible_instead_of_silent_blank(self):
        question = self.data['components'][0]
        question.pop('body')
        question['title'] = 'Visible title-only stem'
        question['displayTitle'] = ''
        self.write_data()
        with self.export() as archive:
            question = json.loads(archive.read('scos/M0/course/en/components.json'))[0]
            self.assertEqual(question['displayTitle'], 'Visible title-only stem')

    def test_nested_domain_mapping_uses_meaning_not_sort_order(self):
        self.data['contentObjects'][0]['_parentId'] = 'domain'
        self.data['contentObjects'].append({'_id': 'domain', '_parentId': 'course', '_type': 'menu', 'title': 'B Anwenden', '_sortOrder': 0})
        self.write_data()
        with self.export() as archive:
            report = json.loads(archive.read('package-provenance.json'))
            self.assertEqual(report['tests']['M0']['banks'], [422])

    def test_missing_tests_unknown_domains_and_unknown_framework_fail_closed(self):
        self.data['components'][0]['_classes'] = 'et'
        self.write_data()
        with self.assertRaisesRegex(PackageValidationError, 'Abschlusstest contains no'):
            self.export()
        self.data['components'][0]['_classes'] = 'et at'
        self.data['contentObjects'][0]['_parentId'] = 'domain'
        self.data['contentObjects'].append({'_id': 'domain', '_parentId': 'course', 'title': 'Unmapped domain'})
        self.write_data()
        with self.assertRaisesRegex(PackageValidationError, 'Unknown diagnostic domain'):
            self.export()
        save(self.build / 'course/de/course.json', {'title': 'AILit1.1 Unknown framework'})
        with self.assertRaisesRegex(PackageValidationError, 'LXP mapping missing'):
            self.export()

    def test_unsupported_tagged_component_is_not_silently_dropped(self):
        self.data['components'][0]['_component'] = 'thirdPartyQuestion'
        self.write_data()
        with self.assertRaisesRegex(PackageValidationError, 'Unsupported diagnostic component'):
            self.export()

    def test_multiple_questions_in_one_block_become_independent_question_blocks(self):
        question = deepcopy(self.data['components'][0])
        question['_id'] = 'q2'
        question['_component'] = 'textinput'
        question['_items'] = [{'answers': ['answer']}]
        self.data['components'].append(question)
        self.write_data()
        with self.export() as archive:
            blocks = json.loads(archive.read('scos/M0/course/en/blocks.json'))
            components = json.loads(archive.read('scos/M0/course/en/components.json'))
            self.assertEqual(len(blocks), 3)
            self.assertNotEqual(components[0]['_parentId'], components[1]['_parentId'])
            self.assertEqual(components[1]['_component'], 'textinput')

    def test_graphic_mcq_uses_available_runtime_without_losing_option_image(self):
        question = self.data['components'][0]
        question['_component'] = 'gmcq'
        question['_items'][0]['_graphic'] = {'src': 'course/de/assets/picture.svg', 'alt': 'First option'}
        self.write_data()
        with self.export() as archive:
            question = json.loads(archive.read('scos/M0/course/en/components.json'))[0]
            self.assertEqual(question['_component'], 'mcq')
            self.assertIn('<img src="course/en/assets/course1/course/picture.svg" alt="First option">', question['_items'][0]['text'])
            self.assertTrue(question['_items'][0]['_shouldBeSelected'])

    def test_wrapper_sha_mismatch_and_path_traversal_fail_closed(self):
        with self.assertRaisesRegex(PackageValidationError, 'SHA-256 mismatch'):
            self.export(expected_wrapper_sha256='0' * 64)
        with ZipFile(self.wrapper, 'a') as archive:
            archive.writestr('../escape.txt', 'unsafe')
        with self.assertRaisesRegex(PackageValidationError, 'Unsafe ZIP entry'):
            self.export()
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_empty_correct_mcq_and_duplicate_course_mapping_fail_closed(self):
        self.data['components'][0]['_items'][0]['_shouldBeSelected'] = False
        self.write_data()
        with self.assertRaisesRegex(PackageValidationError, 'MCQ has no correct answer'):
            self.export()
        self.data['components'][0]['_items'][0]['_shouldBeSelected'] = True
        self.write_data()
        with self.assertRaisesRegex(PackageValidationError, 'Duplicate selected LXP course mapping'):
            package_courses([self.build, self.build], self.wrapper, self.template)


if __name__ == '__main__':
    unittest.main()
