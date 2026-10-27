"""Hidden diagnostic articles must not change the actual learning experience."""
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from adapt_persistence_bootstrap import bootstrap_runtime
import lxp_multi_export as exporter
from lxp_catalogue import PackageValidationError, canonical_bytes
from lxp_diagnostics_articles import learning_source_view, restore_removed_nodes
from lxp_export import published_course
import test_multi_framework_export as fixtures


class DiagnosticsOnlyArticlesTest(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.MixedPackageTest(methodName='runTest')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.build = self.fx.builds[1]
        self.language_dir = self.build / 'course/de'
        self.fx.catalogue['frameworks'][1].pop('defaultDomainId', None)
        self.fx.catalogue['frameworks'][1]['domains'] = [
            {'id': 'domain-' + name, 'label': name, 'sourceTitles': [name + ' – echter Bereich']}
            for name in 'ABC']
        course = self.read('course')
        course['_latestTrackingId'] = 47
        self.write('course', course)
        objects, articles, blocks, components = [], [], [], []
        for index, name in enumerate('ABC'):
            objects.extend([{'_id': 'domain-' + name, '_parentId': 'course', '_type': 'menu', 'title': name + ' – echter Bereich'},
                            {'_id': 'page-' + name, '_parentId': 'domain-' + name, '_type': 'page', 'title': 'Lernseite ' + name}])
            articles.append({'_id': 'learning-article-' + name, '_parentId': 'page-' + name})
            blocks.append({'_id': 'learning-block-' + name, '_parentId': 'learning-article-' + name, '_trackingId': 11 + index})
            components.append({'_id': 'learning-' + name, '_parentId': 'learning-block-' + name,
                               '_component': 'text', 'body': 'Unveränderter Lerninhalt ' + name})
        for collection, values in (('contentObjects', objects), ('articles', articles), ('blocks', blocks), ('components', components)):
            self.write(collection, values)
        self.original = published_course(self.build)

    def read(self, name):
        return json.loads((self.language_dir / (name + '.json')).read_text())

    def write(self, name, value):
        fixtures.save(self.language_dir / (name + '.json'), value)

    def add_helpers(self):
        articles, blocks, components = self.read('articles'), self.read('blocks'), self.read('components')
        for name in 'ABC':
            articles.append({'_id': 'diagnostic-article-' + name, '_parentId': 'page-' + name,
                             '_classes': 'author-extra lxp-diagnostics-only', '_isAvailable': False, '_isOptional': True})
            for number in range(4):
                suffix = name + '-' + str(number)
                block_id = 'diagnostic-block-' + suffix
                blocks.append({'_id': block_id, '_parentId': 'diagnostic-article-' + name, '_trackingId': 100 + len(blocks)})
                components.append({'_id': 'diagnostic-question-' + suffix, '_parentId': block_id,
                                   '_component': 'mcq', '_classes': 'et at', '_isAvailable': True,
                                   'instruction': '<p>Eigenständig beantwortbare Frage ' + suffix + '</p>',
                                   '_items': [{'text': 'Akzeptierte Antwort', '_shouldBeSelected': True}, {'text': 'Andere Antwort'}]})
        self.write('articles', articles)
        self.write('blocks', blocks)
        self.write('components', components)
        return published_course(self.build)

    def ail_sco(self, prepared):
        return next(s for s in prepared.contract['scos'] if s['role'] == 'learning' and s['source']['originId'] == 'tenant-b')

    def test_only_explicit_helper_subtrees_disappear_and_original_fingerprint_is_exact(self):
        before = exporter.learning_compatibility_fingerprint(self.original)
        raw = self.add_helpers()
        snapshot = deepcopy(raw)
        learning, removed = learning_source_view(raw)
        self.assertEqual(learning['data'], self.original['data'])
        self.assertEqual(learning['course'], self.original['course'])
        self.assertEqual(learning['course']['_latestTrackingId'], 47)
        self.assertEqual(exporter.learning_compatibility_fingerprint(raw), before)
        self.assertEqual(raw, snapshot)
        self.assertEqual({name: len(values) for name, values in removed.items()}, {'articles': 3, 'blocks': 12, 'components': 12})
        self.assertEqual(restore_removed_nodes(learning, removed), raw)
        markerless = published_course(self.fx.builds[0])
        self.assertIs(learning_source_view(markerless)[0], markerless)

    def test_learning_changes_still_change_fingerprint_no_generic_volatile_ignores(self):
        raw = self.add_helpers()
        before = exporter.learning_compatibility_fingerprint(raw)
        for collection, field, value in (('components', 'body', 'Changed learning prompt'),
                                         ('articles', '_isOptional', True), ('blocks', '_trackingId', 999),
                                         ('contentObjects', '_isAvailable', False)):
            changed = deepcopy(raw)
            changed['data'][collection][0][field] = value
            self.assertNotEqual(before, exporter.learning_compatibility_fingerprint(changed))
        changed = deepcopy(raw)
        changed['course']['_latestTrackingId'] = 48
        self.assertNotEqual(before, exporter.learning_compatibility_fingerprint(changed))
        changed = deepcopy(raw)
        changed['data']['components'][-1]['instruction'] = 'Only diagnostic question changed'
        self.assertEqual(before, exporter.learning_compatibility_fingerprint(changed))

    def test_marker_requires_article_explicit_hidden_optional_and_real_page(self):
        raw = self.add_helpers()
        for field, value in (('_isAvailable', True), ('_isAvailable', None), ('_isAvailable', 0),
                             ('_isOptional', False), ('_isOptional', None), ('_isOptional', 1)):
            bad = deepcopy(raw)
            bad['data']['articles'][-1][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(PackageValidationError, 'unavailable and optional'):
                exporter.learning_compatibility_fingerprint(bad)
        for collection in ('contentObjects', 'blocks', 'components'):
            bad = deepcopy(raw)
            bad['data'][collection][0]['_classes'] = 'lxp-diagnostics-only'
            with self.subTest(collection=collection), self.assertRaisesRegex(PackageValidationError, 'only on an article'):
                exporter.learning_compatibility_fingerprint(bad)
        bad = deepcopy(raw)
        bad['course']['_classes'] = 'lxp-diagnostics-only'
        with self.assertRaisesRegex(PackageValidationError, 'only on an article'):
            exporter.learning_compatibility_fingerprint(bad)
        for parent in ('missing', 'domain-A', 'diagnostic-block-A-0'):
            bad = deepcopy(raw)
            bad['data']['articles'][-1]['_parentId'] = parent
            with self.subTest(parent=parent), self.assertRaisesRegex(PackageValidationError, 'existing learning page'):
                exporter.learning_compatibility_fingerprint(bad)

    def test_marker_does_not_accept_broken_descendant_or_duplicate_ids(self):
        raw = self.add_helpers()
        bad = deepcopy(raw)
        bad['data']['components'][-1]['_parentId'] = 'diagnostic-article-C'
        with self.assertRaisesRegex(PackageValidationError, 'malformed descendant'):
            exporter.learning_compatibility_fingerprint(bad)
        bad = deepcopy(raw)
        bad['data']['components'][-1]['_id'] = 'learning-A'
        with self.assertRaisesRegex(PackageValidationError, 'unique explicit source IDs'):
            exporter.learning_compatibility_fingerprint(bad)

    def test_real_export_preserves_learning_delivers_both_diagnostics_and_exact_provenance(self):
        self.add_helpers()
        source_manifests = [exporter.tree_manifest(build) for build in self.fx.builds]
        baseline = exporter.learning_compatibility_fingerprint(self.original)
        prepared = self.fx.prepare()
        sco = self.ail_sco(prepared)
        self.assertEqual(sco['source']['componentFingerprint'], baseline)
        questions = [q for q in prepared.contract['questions'] if q['sourceCourseId'] == sco['id']]
        self.assertEqual(len(questions), 12)
        self.assertEqual({q['sourceComponentId'] for q in questions}, {'diagnostic-question-' + name + '-' + str(n) for name in 'ABC' for n in range(4)})
        for assessment in (a for a in prepared.contract['assessments'] if a['frameworkId'] == sco['frameworkId']):
            self.assertEqual({b['domainId']: len(b['questionIds']) for b in assessment['banks']}, {'domain-A': 4, 'domain-B': 4, 'domain-C': 4})
        with self.fx.export(prepared) as archive:
            report = next(c for c in json.loads(archive.read('package-provenance.json'))['courses'] if c['scoId'] == sco['id'])
            changes = [c for c in report['copyChanges'] if c['kind'] == 'diagnostic-only-article-removal']
            self.assertEqual(len(changes), 3)
            for name in ('contentObjects', 'articles', 'blocks', 'components', 'course'):
                path = 'course/de/' + name + '.json'
                actual = json.loads(archive.read(sco['launch']['root'] + '/' + path))
                self.assertEqual(actual, self.original['course'] if name == 'course' else self.original['data'][name])
                if name in {'contentObjects', 'course'}:
                    self.assertEqual(archive.read(sco['launch']['root'] + '/' + path), (self.build / path).read_bytes())
            for change in changes:
                self.assertEqual(change['input_sha256'], source_manifests[1][change['path']])
                self.assertEqual(base64.b64decode(change['input_bytes_base64'], validate=True), (self.build / change['path']).read_bytes())
                self.assertEqual(change['output_sha256'], hashlib.sha256(archive.read(sco['launch']['root'] + '/' + change['path'])).hexdigest())
                self.assertEqual(change['removedIds'], [entry['node']['_id'] for entry in change['removedNodes']])
            self.assertEqual(set(report['copyChanges'][0]), {'path', 'kind', 'version', 'input_sha256', 'output_sha256'})
            for assessment in (a for a in prepared.contract['assessments'] if a['frameworkId'] == sco['frameworkId']):
                test_sco = next(s for s in prepared.contract['scos'] if s['id'] == assessment['scoId'])
                generated = json.loads(archive.read(test_sco['launch']['root'] + '/course/en/components.json'))
                self.assertEqual(len([q for q in generated if q['_component'] == 'mcq']), 12)
        self.assertEqual(source_manifests, [exporter.tree_manifest(build) for build in self.fx.builds])

    def test_no_untagged_questions_or_guessed_domains_are_selected(self):
        self.add_helpers()
        components = self.read('components')
        components[-1]['_classes'] = ''
        self.write('components', components)
        prepared = self.fx.prepare()
        sco = self.ail_sco(prepared)
        self.assertEqual(len([q for q in prepared.contract['questions'] if q['sourceCourseId'] == sco['id']]), 11)
        objects = self.read('contentObjects')
        objects[0]['title'] = 'Not an explicitly configured ABC domain'
        self.write('contentObjects', objects)
        with self.assertRaisesRegex(PackageValidationError, 'explicitly configured diagnostic domain'):
            self.fx.prepare()

    def test_marker_without_hiding_fails_preflight_even_when_diagnostics_omitted(self):
        self.add_helpers()
        articles = self.read('articles')
        articles[-1]['_isAvailable'] = True
        self.write('articles', articles)
        with self.assertRaisesRegex(PackageValidationError, 'unavailable and optional'):
            self.fx.prepare(diagnostics={'AILit': {'entry': 'omit', 'final': 'omit'}})

    def test_helper_implementation_and_raw_source_are_bound_to_frozen_confirmation(self):
        self.add_helpers()
        prepared = self.fx.prepare()
        self.assertIn('lxp_diagnostics_articles.py', exporter._generator_files())
        self.assertTrue(all('lxp_diagnostics_articles.py' in s['source']['generatorFiles'] for s in prepared.contract['scos'] if s['role'] != 'learning'))
        real_hash = exporter.file_sha256
        def changed_helper(path):
            return '0' * 64 if Path(path).name == 'lxp_diagnostics_articles.py' else real_hash(path)
        with patch.object(exporter, 'file_sha256', side_effect=changed_helper):
            with self.assertRaisesRegex(PackageValidationError, 'implementation changed'):
                self.fx.export(prepared)
        components = self.read('components')
        components[-1]['instruction'] = 'Changed after preview, even though learning fingerprint stays equal'
        self.write('components', components)
        with self.assertRaisesRegex(PackageValidationError, 'Published source changed since preview'):
            self.fx.export(prepared)

    def test_already_bootstrapped_source_has_no_fictitious_copy_change(self):
        self.add_helpers()
        runtime = self.build / 'adapt/js/adapt.min.js'
        once, _ = bootstrap_runtime(runtime.read_bytes())
        twice, metadata = bootstrap_runtime(once)
        self.assertEqual(once, twice)
        self.assertEqual(metadata['input_sha256'], metadata['output_sha256'])
        runtime.write_bytes(twice)
        prepared = self.fx.prepare()
        sco = self.ail_sco(prepared)
        with self.fx.export(prepared) as archive:
            self.assertEqual(archive.read(sco['launch']['root'] + '/adapt/js/adapt.min.js'), once)
            report = next(c for c in json.loads(archive.read('package-provenance.json'))['courses'] if c['scoId'] == sco['id'])
            self.assertEqual({c['kind'] for c in report['copyChanges']}, {'diagnostic-only-article-removal'})
            self.assertEqual(len(report['copyChanges']), 3)

    def test_tampered_removal_provenance_or_delivered_json_is_rejected(self):
        self.add_helpers()
        prepared = self.fx.prepare()
        sco = self.ail_sco(prepared)
        destination = self.fx.root / 'delivered'
        with self.fx.export(prepared) as archive:
            archive.extractall(destination)
        provenance_path = destination / 'package-provenance.json'
        original = json.loads(provenance_path.read_text())
        for mutation in ('removedId', 'sourceHash', 'position', 'marker', 'missingChange', 'malformedBytes', 'missingBytes'):
            provenance = deepcopy(original)
            report = next(c for c in provenance['courses'] if c['scoId'] == sco['id'])
            change = next(c for c in report['copyChanges'] if c['kind'] == 'diagnostic-only-article-removal')
            if mutation == 'removedId':
                change['removedIds'][0] = 'invented-id'
            elif mutation == 'sourceHash':
                change['source_semantic_sha256'] = '0' * 64
            elif mutation == 'position':
                change['removedNodes'][0]['index'] = 99999
            elif mutation == 'marker':
                change['removedNodes'][0]['node']['_classes'] = ''
            elif mutation == 'malformedBytes':
                change['input_bytes_base64'] = 'not-valid-base64!'
            elif mutation == 'missingBytes':
                change.pop('input_bytes_base64')
            else:
                report['copyChanges'].remove(change)
            fixtures.save(provenance_path, provenance)
            with self.subTest(mutation=mutation), self.assertRaises(PackageValidationError):
                exporter.validate_product_contract(prepared.contract, destination)
        fixtures.save(provenance_path, original)
        path = destination / sco['launch']['root'] / 'course/de/components.json'
        components = json.loads(path.read_text())
        components[0]['body'] = 'Undeclared modified learning content'
        fixtures.save(path, components)
        with self.assertRaisesRegex(PackageValidationError, 'file manifest differs'):
            exporter.validate_product_contract(prepared.contract, destination)

    def test_removed_answer_key_plus_recomputed_semantic_hash_cannot_change_source_authority(self):
        self.add_helpers()
        prepared = self.fx.prepare()
        sco = self.ail_sco(prepared)
        destination = self.fx.root / 'answer-key-attack'
        with self.fx.export(prepared) as archive:
            archive.extractall(destination)
        provenance_path = destination / 'package-provenance.json'
        provenance = json.loads(provenance_path.read_text())
        report = next(c for c in provenance['courses'] if c['scoId'] == sco['id'])
        change = next(c for c in report['copyChanges'] if c['path'] == 'course/de/components.json')
        question = change['removedNodes'][0]['node']
        question['_items'][0]['_shouldBeSelected'] = False
        question['_items'][1]['_shouldBeSelected'] = True
        removals = {Path(c['path']).stem: c['removedNodes'] for c in report['copyChanges'] if c['kind'] == 'diagnostic-only-article-removal'}
        reconstructed = restore_removed_nodes(published_course(destination / sco['launch']['root']), removals)
        change['source_semantic_sha256'] = hashlib.sha256(canonical_bytes(reconstructed['data']['components'])).hexdigest()
        fixtures.save(provenance_path, provenance)
        with self.assertRaisesRegex(PackageValidationError, 'reconstructed nodes differ from original source JSON bytes'):
            exporter.validate_product_contract(prepared.contract, destination)

    def test_modified_original_bytes_and_input_hash_cannot_override_source_file_manifest(self):
        self.add_helpers()
        prepared = self.fx.prepare()
        sco = self.ail_sco(prepared)
        destination = self.fx.root / 'source-bytes-attack'
        with self.fx.export(prepared) as archive:
            archive.extractall(destination)
        provenance_path = destination / 'package-provenance.json'
        provenance = json.loads(provenance_path.read_text())
        report = next(c for c in provenance['courses'] if c['scoId'] == sco['id'])
        change = next(c for c in report['copyChanges'] if c['path'] == 'course/de/components.json')
        original = json.loads(base64.b64decode(change['input_bytes_base64'], validate=True))
        index = change['removedNodes'][0]['index']
        original[index]['_items'][0]['_shouldBeSelected'] = False
        original[index]['_items'][1]['_shouldBeSelected'] = True
        change['removedNodes'][0]['node'] = deepcopy(original[index])
        changed_bytes = (json.dumps(original, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        change['input_bytes_base64'] = base64.b64encode(changed_bytes).decode('ascii')
        change['input_sha256'] = hashlib.sha256(changed_bytes).hexdigest()
        change['source_semantic_sha256'] = hashlib.sha256(canonical_bytes(original)).hexdigest()
        fixtures.save(provenance_path, provenance)
        with self.assertRaisesRegex(PackageValidationError, 'input SHA-256 differs from original file manifest'):
            exporter.validate_product_contract(prepared.contract, destination)


if __name__ == '__main__':
    unittest.main()
