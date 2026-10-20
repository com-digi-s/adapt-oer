import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
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

    @unittest.skipUnless(shutil.which('node'), 'JavaScript parser requires node')
    def test_result_hook_subscribes_before_any_submit_click_including_restore(self):
        with ZipFile('static/assessment-template.zip') as archive:
            instruction = json.loads(archive.read('course/en/components.json'))[0]['instruction']
        self.assertEqual(instruction, Path('assessment-template/results.html').read_text(encoding='utf-8'))
        source = re.search(r'<script>([\s\S]+)</script>', instruction)[1]
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = SCRIPT;
let calls = 0, click;
const chain = { addClass() { return this; }, text() { return this; }, attr() { return this; }, on() { return this; }, append() { return this; } };
const context = { window: { parent: { App: { adaptHelpers: { getDiagnosticResults() { calls++; } } } }, scrollTo() {} }, document: { body: { scrollHeight: 0 } }, setTimeout() {}, $(selector) { return selector === '.submit-all button' ? { on(event, callback) { click = callback; } } : chain; } };
vm.runInNewContext(script, context);
assert.equal(calls, 1, 'render/restore must subscribe without a potentially disabled button click');
click();
assert.equal(calls, 2, 'first submission keeps the existing callback');
vm.runInNewContext(script, { window: { parent: {} } });
'''.replace('SCRIPT', json.dumps(source))
        subprocess.run(['node', '-'], input=harness, text=True, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
