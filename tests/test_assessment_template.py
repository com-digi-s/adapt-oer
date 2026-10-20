import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from zipfile import ZipFile

from tools.rebuild_assessment_template import rebuild


class AssessmentTemplateTest(unittest.TestCase):
    def test_native_question_text_not_globally_hidden(self):
        with ZipFile('static/assessment-template.zip') as archive:
            css = archive.read('adapt.css').decode()
        self.assertIsNone(re.search(r'(?m)^p\s*\{\s*display:\s*none', css))
        self.assertIsNone(re.search(r'(?m)^\.component__title\s*\{\s*display:\s*none', css))

    def test_results_container_not_a_question_bank(self):
        with ZipFile('static/assessment-template.zip') as archive:
            blocks = json.loads(archive.read('course/en/blocks.json'))
        self.assertNotIn('_assessment', blocks[0])

    def test_rebuild_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'template.zip'
            shutil.copyfile('static/assessment-template.zip', target)
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            rebuild(target)
            self.assertEqual(before, hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
