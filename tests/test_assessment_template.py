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
    @unittest.skipUnless(shutil.which('node'), 'Native model regression requires node')
    def test_native_textinput_normalized_indices_restore_same_score_and_all_wrong_is_zero(self):
        with ZipFile('static/assessment-template.zip') as archive:
            runtime = archive.read('adapt/js/adapt.min.js').decode()
        start = runtime.index('    class TextInputModel extends QuestionModel$1 {')
        end = runtime.index('    var TextInputModel$1', start)
        native_class = runtime[start:end]
        for filename in ('textinput-is-correct.js', 'textinput-specific-answers.js'):
            self.assertIn(Path('assessment-template', filename).read_text().rstrip(), native_class)
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
class Base{constructor(attributes){this.a=attributes;}get(key){return this.a[key];}set(key,value){if(typeof key==='object')Object.assign(this.a,key);else this.a[key]=value;}setQuestionAsSubmitted(){}markQuestion(){this.set('_isCorrect',this.isCorrect());}setupFeedback(){}}
const Native=vm.runInNewContext('(()=>{'+CLASS+'return TextInputModel;})()',{QuestionModel$1:Base});
const items=[{_index:0,_answers:['Ärger'],userAnswer:'ärger!'},{_index:1,_answers:['Quelle'],userAnswer:'wrong'}];
const model=new Native({_items:items,_allowsAnyCase:true,_allowsPunctuation:true,_questionWeight:4,_isSubmitted:true});
model.storeUserAnswer();model.markQuestion();model.setScore();assert.deepEqual(Array.from(model.get('_userAnswer')),[0,-1]);assert.equal(model.get('_score'),2);assert.equal(items[0]._answers[0],'Ärger','Authored accepted answer is not rewritten');
delete items[0].userAnswer;delete items[1].userAnswer;model.restoreUserAnswers();assert.equal(items[0].userAnswer,'Ärger');assert.equal(model.get('_score'),2,'Native restore must preserve the real partial score');
items[0].userAnswer='wrong';items[1].userAnswer='also wrong';model.storeUserAnswer();model.markQuestion();model.setScore();assert.equal(model.get('_score'),0,'Native all-wrong grading is finite zero, not stale/NaN');assert.equal(model.get('_numberOfCorrectAnswers'),0);
'''.replace('CLASS', json.dumps(native_class.strip()))
        result = subprocess.run(['node', '-'], input=harness, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_unknown_native_textinput_method_is_not_silently_patched(self):
        with ZipFile('static/assessment-template.zip') as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        runtime = entries['adapt/js/adapt.min.js'].decode()
        known = 'item._answerIndex = answers.findIndex(answer => this.cleanupUserAnswer(answer) === this.cleanupUserAnswer(userAnswer));'
        self.assertEqual(runtime.count(known), 1)
        entries['adapt/js/adapt.min.js'] = runtime.replace(known, 'item._answerIndex = -1;', 1).encode()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'unknown-template.zip'
            with ZipFile(target, 'w') as archive:
                for name, value in entries.items():
                    archive.writestr(name, value)
            with self.assertRaisesRegex(ValueError, 'Unknown native TextInputModel.markSpecificAnswers'):
                rebuild(target)

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

    @unittest.skipUnless(shutil.which('node'), 'JavaScript parser requires node')
    def test_submit_all_rechecks_real_can_submit_after_component_render(self):
        source = Path('assessment-template/submit-all-component-ready.js').read_text(encoding='utf-8').rstrip()
        with ZipFile('static/assessment-template.zip') as archive:
            self.assertIn(source, archive.read('adapt/js/adapt.min.js').decode())
        harness = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
const method = vm.runInNewContext('({' + SOURCE + '})');
const views = [], deferred = [], bindings = [];
let answered = false, enabled = false;
const article = { get(key) { return key === '_id' ? 'article' : undefined; } };
const controller = { model: { get(key) { return key === '_componentViews' ? views : { model: article }; } }, onInteraction() { deferred.push(() => { enabled = views.every(view => view.canSubmit()); }); } };
for (const type of ['mcq', 'textinput']) {
  const binder = { on(event) { bindings.push(event); } };
  const view = { $el: { hasClass() { return true; }, find() { return binder; }, on(event) { bindings.push(event); } }, model: { get() { return type; }, findAncestor() { return article; } }, canSubmit() { return answered; } };
  method.onComponentViewRendered.call(controller, view);
}
assert.equal(deferred.length, 2);
assert.equal(enabled, false);
answered = true; // The genuine Core restore hook runs before deferred checks.
for (const callback of deferred) callback();
assert.equal(enabled, true, 'restored complete input must enable SubmitAll without a fake DOM click');
assert.deepEqual(bindings.sort(), ['change.submitAll', 'click.submitAll change.submitAll dragstop.submitAll']);
'''.replace('SOURCE', json.dumps(source))
        subprocess.run(['node', '-'], input=harness, text=True, check=True, capture_output=True)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript parser requires node')
    def test_last_real_drag_event_rechecks_submit_all_without_extra_click_and_cleans_handlers(self):
        source = Path('assessment-template/submit-all-component-ready.js').read_text(encoding='utf-8').rstrip()
        cleanup = Path('assessment-template/submit-all-event-cleanup.js').read_text(encoding='utf-8').rstrip()
        with ZipFile('static/assessment-template.zip') as archive:
            self.assertIn(cleanup, archive.read('adapt/js/adapt.min.js').decode())
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const methods=vm.runInNewContext('({'+SOURCE+CLEANUP+'})');
const views=[],pending=[],listeners=new Map(),removed=[];let allDropSlotsFilled=false,enabled=false;
const article={get(){return 'article';}};
const controller={model:{get(key){return key==='_componentViews'?views:{model:article};}},onInteraction(){pending.push(()=>{enabled=views.every(view=>view.canSubmit());});}};
const root={hasClass(){return true;},on(events,callback){for(const name of events.split(' '))listeners.set(name,callback);},off(namespace){removed.push('root:'+namespace);},find(){return{off(namespace){removed.push('input:'+namespace);}};}};
const view={$el:root,model:{get(){return 'infai-dragndrop';},findAncestor(){return article;}},canSubmit(){return allDropSlotsFilled;}};
methods.onComponentViewRendered.call(controller,view);pending.shift()();assert.equal(enabled,false);
assert.ok(listeners.has('dragstop.submitAll'));assert.ok(listeners.has('change.submitAll'));
allDropSlotsFilled=true;listeners.get('dragstop.submitAll').call(controller);assert.equal(enabled,false,'Native event remains deferred, not a manufactured model/grade update');
pending.shift()();assert.equal(enabled,true,'The last actual native drop must enable submit without an extra click');
methods.removeEventListeners.call(controller);assert.deepEqual(removed,['input:.submitAll','root:.submitAll']);
'''.replace('SOURCE', json.dumps(source)).replace('CLEANUP', json.dumps(cleanup))
        subprocess.run(['node', '-'], input=harness, text=True, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
