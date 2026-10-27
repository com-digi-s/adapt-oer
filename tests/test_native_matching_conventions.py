"""Technical regression: native matching accepts alternatives within one row.

The declared fixture reproduces an existing ComDigiS question convention, not
new course content. The native check executes the actual assessment template.
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


def matching():
    return {'_id': 'matching-native', '_component': 'matching', 'body': 'Match',
            '_questionWeight': 2, '_hasItemScoring': False,
            '_items': [{'_options': [{'text': 'Accepted A', '_isCorrect': True},
                                    {'text': 'Accepted B', '_isCorrect': True},
                                    {'text': 'Accepted C', '_isCorrect': True},
                                    {'text': 'Wrong', '_isCorrect': False}]},
                       {'_options': [{'text': 'Wrong', '_isCorrect': False},
                                    {'text': 'Accepted', '_isCorrect': True}]},
                       {'_options': [{'text': 'Accepted', '_isCorrect': True},
                                    {'text': 'Wrong', '_isCorrect': False}]}]}


class NativeMatchingConventionTest(unittest.TestCase):
    def test_alternative_keys_are_one_attainable_credit_per_row(self):
        question = matching()
        original = deepcopy(question)
        self.assertEqual(_validate_question(question), 6)
        self.assertEqual(question, original)
        question['_items'][0]['_options'] = [{'text': 'No accepted key'}]
        with self.assertRaisesRegex(PackageValidationError, 'Matching question'):
            _validate_question(question)

    @unittest.skipUnless(shutil.which('node'), 'Native model regression requires node')
    def test_actual_native_model_accepts_each_authored_alternative(self):
        template = Path(__file__).resolve().parents[1] / 'static/assessment-template.zip'
        with ZipFile(template) as archive:
            runtime = archive.read('adapt/js/adapt.min.js').decode()

        def native_class(name, end_anchor):
            start = runtime.index('class ' + name + ' extends ')
            value = runtime[start:runtime.index(end_anchor, start)].strip()
            self.assertTrue(value.endswith(');'), 'Known native class boundary required')
            return value[:-2].rstrip()

        classes = '\n'.join((native_class('ItemsQuestionModel', 'var MatchingModel ='),
            native_class('MatchingModel', '__AMD("components/adapt-contrib-matching/js/adapt-contrib-matching"')))
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
class Base{constructor(attributes){this.a=attributes;}get(key){return this.a[key];}set(key,value){if(typeof key==='object')Object.assign(this.a,key);else this.a[key]=value;}getChildren(){return this.children;}setChildren(value){this.children=value;}getItem(index){return this.children.models.find(model=>model.get('_index')===index);}}
class Item extends Base{toggleActive(active){this.set('_isActive',active);}toggleHighlighted(active){this.set('_isHighlighted',active);}}
class Collection{constructor(items){this.models=items.map(item=>new Item(item));}filter(fn){return this.models.filter(fn);}forEach(fn){return this.models.forEach(fn);}reduce(fn,initial){return this.models.reduce(fn,initial);}slice(...args){return this.models.slice(...args);}}
const Native=vm.runInNewContext('(()=>{'+CLASSES+'return MatchingModel;})()',{BlendedItemsComponentQuestionModel:Base,MatchingItemModel:Item,Backbone:{Collection}});
const model=new Native(QUESTION);model.setUpItems();
for(const alternative of [0,1,2]){model.setActiveOption(alternative);model.setActiveOption(5);model.setActiveOption(6);assert.equal(model.canSubmit(),true);assert.equal(model.isCorrect(),true,'Every authored alternative in the first row is natively accepted');model.storeUserAnswer();assert.equal(model.get('_userAnswer').filter(Boolean).length,3,'Exactly one option per row can be selected');}
model.setActiveOption(3);assert.equal(model.isCorrect(),false);assert.equal(model.get('_numberOfCorrectAnswers'),2);
'''.replace('CLASSES', json.dumps(classes)).replace('QUESTION', json.dumps(matching()))
        result = subprocess.run(['node', '-'], input=harness, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class NativeMatchingConventionExportTest(unittest.TestCase):
    setUp = export_fixtures.MixedPackageTest.setUp
    write_wrapper = export_fixtures.MixedPackageTest.write_wrapper
    add_source = export_fixtures.MixedPackageTest.add_source
    prepare = export_fixtures.MixedPackageTest.prepare
    export = export_fixtures.MixedPackageTest.export
    change_question = export_fixtures.MixedPackageTest.change_question

    def test_generated_question_preserves_every_key_with_attainable_row_maximum(self):
        values = matching()
        self.change_question(**{k: v for k, v in values.items() if k != '_id'})
        before = [tree_manifest(build) for build in self.builds]
        prepared = self.prepare()
        question = next(q for q in prepared.contract['questions'] if q['component'] == 'matching')
        self.assertEqual(question['maxPoints'], 6)
        with self.export(prepared) as archive:
            assessment = next(s for s in prepared.contract['scos'] if s['role'] == 'entry' and s['frameworkId'] == 'fw-digcomp-22')
            generated = json.loads(archive.read(assessment['launch']['root'] + '/course/en/components.json'))[0]
            self.assertEqual(generated['_items'], values['_items'])
            self.assertEqual(generated['_questionWeight'], 2)
        self.assertEqual(before, [tree_manifest(build) for build in self.builds])
