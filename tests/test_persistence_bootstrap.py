import json
import shutil
import subprocess
import unittest
from zipfile import ZipFile

from adapt_persistence_bootstrap import bootstrap_runtime, START


class PersistenceBootstrapTest(unittest.TestCase):
    def test_real_template_has_one_early_hook_and_rebuild_is_idempotent(self):
        with ZipFile('static/assessment-template.zip') as archive:
            source = archive.read('adapt/js/adapt.min.js')
        result, metadata = bootstrap_runtime(source)
        self.assertEqual(result, source)
        self.assertEqual(source.count(START.encode()), 1)
        self.assertLess(source.index(START.encode()), source.index(b'data$1.on("ready"'))
        self.assertEqual(metadata['input_sha256'], metadata['output_sha256'])

    def test_minified_current_and_formatted_legacy_runtime_markers_supported(self):
        for source in [b'var Adapt$1=__AMD("core/js/adapt",Adapt);', b"const AdaptAlias = __AMD('core/js/adapt', Adapt);"]:
            output, metadata = bootstrap_runtime(source)
            self.assertEqual(output.count(START.encode()), 1)
            self.assertNotEqual(metadata['input_sha256'], metadata['output_sha256'])
            self.assertEqual(bootstrap_runtime(output)[0], output)

    def test_unknown_and_ambiguous_runtimes_rejected(self):
        for source in [b'no known runtime declaration', b'var a=__AMD("core/js/adapt",Adapt);var b=__AMD("core/js/adapt",Adapt);']:
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                bootstrap_runtime(source)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript parser requires node')
    def test_hook_executes_before_data_and_does_not_change_standalone_runtime(self):
        output, _ = bootstrap_runtime(b'var Adapt$1=__AMD("core/js/adapt",Adapt);order.push("data-init");')
        harness = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
const source = SOURCE;
const order = [], Adapt = {};
const window = { parent: { App: { attachAdaptPersistence(adapt, childWindow) { assert.equal(adapt, Adapt); assert.equal(childWindow, window); order.push('hook'); } } } };
vm.runInNewContext(source, { window, order, Adapt, __AMD(name, value) { return value; } });
assert.deepEqual(order, ['hook', 'data-init']);
const standalone = {}; standalone.parent = standalone;
vm.runInNewContext(source, { window: standalone, order: [], Adapt, __AMD(name, value) { return value; } });
'''.replace('SOURCE', json.dumps(output.decode()))
        subprocess.run(['node', '-'], input=harness, text=True, check=True, capture_output=True)
