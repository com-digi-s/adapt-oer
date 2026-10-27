"""Technical regression for native empty-single-answer slider ranges.

The declared fixture reproduces the ComDigiS range convention, not new course
content. Native-model checks execute the actual shipped assessment template.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from zipfile import ZipFile

from lxp_catalogue import PackageValidationError
from lxp_multi_export import _validate_question, tree_manifest
import test_multi_framework_export as export_fixtures


def slider():
    return {'_id': 'slider-native', '_component': 'slider', 'body': 'Estimate',
            '_questionWeight': 2, '_scaleStart': 1, '_scaleEnd': 110,
            '_scaleStep': 1, '_correctAnswer': '',
            '_correctRange': {'_bottom': 0, '_top': 100}}


class NativeSliderConventionTest(unittest.TestCase):
    def test_empty_answer_uses_nonempty_visible_range_without_rewriting_it(self):
        question = slider()
        original = deepcopy(question)
        self.assertEqual(_validate_question(question), 2)
        self.assertEqual(question, original)
        for answer in (None, ''):
            for bottom, top in ((-100, 2), (100, 200), (1, 1), (-100, 1000)):
                question.update(_correctAnswer=answer, _correctRange={'_bottom': bottom, '_top': top})
                self.assertEqual(_validate_question(question), 2)

    def test_malformed_or_unattainable_ranges_and_explicit_bad_answers_fail(self):
        for value in ({'_bottom': 111, '_top': 200}, {'_bottom': -100, '_top': 0},
                      {'_bottom': 2, '_top': 1}, {'_bottom': 1.1, '_top': 1.9},
                      {'_bottom': False, '_top': 100}, {'_bottom': 0, '_top': float('inf')},
                      {'_bottom': 1}, None):
            with self.subTest(value=value), self.assertRaisesRegex(PackageValidationError, 'correct answer/range'):
                _validate_question({**slider(), '_correctRange': value})
        for answer in ('not-a-number', False, 0, 111):
            with self.subTest(answer=answer), self.assertRaisesRegex(PackageValidationError, 'not on its native scale'):
                _validate_question({**slider(), '_correctAnswer': answer})

    @unittest.skipUnless(shutil.which('node'), 'Native model regression requires node')
    def test_actual_native_model_selects_visible_range_and_grades_real_values(self):
        template = Path(__file__).resolve().parents[1] / 'static/assessment-template.zip'
        with ZipFile(template) as archive:
            runtime = archive.read('adapt/js/adapt.min.js').decode()
        start = runtime.index('class SliderModel extends ')
        value = runtime[start:runtime.index('__AMD("components/adapt-contrib-slider/js/adapt-contrib-slider"', start)].strip()
        self.assertTrue(value.endswith(');'), 'Known native class boundary required')
        native_class = value[:-2].rstrip()
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
class Base{constructor(attributes){this.a=attributes;}get(key){return this.a[key];}set(key,value){if(typeof key==='object')Object.assign(this.a,key);else this.a[key]=value;}}
const Native=vm.runInNewContext('(()=>{'+CLASS+'return SliderModel;})()',{QuestionModel$1:Base,Adapt$1:{config:{get(){return 'ltr';}}}});
const model=new Native(QUESTION);model.setupModelItems();const items=model.get('_items');assert.equal(items.length,110);assert.equal(items.filter(item=>item.correct).length,100);assert.equal(items[0].value,1);assert.equal(items[0].correct,true);assert.equal(items.at(-1).correct,false);
for(const [value,correct,score] of [[100,true,2],[101,false,0]]){items.forEach(item=>item.selected=item.value===value);assert.equal(model.isCorrect(),correct);model.setScore();assert.equal(model.get('_score'),score);}
assert.equal(model.get('_correctAnswer'),'');assert.equal(model.get('_correctRange')._bottom,0);assert.equal(model.get('_correctRange')._top,100,'Authored accepted range remains untouched');
'''.replace('CLASS', json.dumps(native_class)).replace('QUESTION', json.dumps(slider()))
        result = subprocess.run(['node', '-'], input=harness, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class NativeSliderConventionExportTest(unittest.TestCase):
    setUp = export_fixtures.MixedPackageTest.setUp
    write_wrapper = export_fixtures.MixedPackageTest.write_wrapper
    add_source = export_fixtures.MixedPackageTest.add_source
    prepare = export_fixtures.MixedPackageTest.prepare
    export = export_fixtures.MixedPackageTest.export
    change_question = export_fixtures.MixedPackageTest.change_question

    def test_generated_question_preserves_empty_single_answer_and_original_range(self):
        values = slider()
        self.change_question(**{k: v for k, v in values.items() if k != '_id'})
        before = [tree_manifest(build) for build in self.builds]
        prepared = self.prepare()
        question = next(q for q in prepared.contract['questions'] if q['component'] == 'slider')
        self.assertEqual(question['maxPoints'], 2)
        with self.export(prepared) as archive:
            assessment = next(s for s in prepared.contract['scos'] if s['role'] == 'entry' and s['frameworkId'] == 'fw-digcomp-22')
            generated = json.loads(archive.read(assessment['launch']['root'] + '/course/en/components.json'))[0]
            self.assertEqual(generated['_correctAnswer'], '')
            self.assertEqual(generated['_correctRange'], {'_bottom': 0, '_top': 100})
            self.assertEqual((generated['_scaleStart'], generated['_scaleEnd']), (1, 110))
        self.assertEqual(before, [tree_manifest(build) for build in self.builds])
