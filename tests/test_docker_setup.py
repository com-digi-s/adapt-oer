"""Pinned bundled wrapper and native configuration, without production services.

Only the tracked native example is read. Runtime/helper fixtures are isolated;
no deployment stack, real env file, database or container daemon is accessed.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from lxp_catalogue import PackageValidationError
from lxp_multi_export import _extract, _validate_wrapper, tree_manifest, validate_archive_entries
import test_multi_framework_export as fixtures


ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SHA256 = '322f281ad57bae275dae141f07c6bd1a4c41bce65e7b702c71975be313c16d32'
UPSTREAM_COMMIT = '903429b527bf47241181e8dda9c7bb3992581960'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def example_values(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key in result:
            raise AssertionError('Malformed or duplicate example setting: ' + key)
        result[key] = value
    return result


class DockerConfigurationTest(unittest.TestCase):
    def config(self, values=None):
        # No reload of the real application/config module and no inherited
        # credentials or developer-machine environment enter these assertions.
        with patch.dict(os.environ, values or {}, clear=True):
            return load_module('isolated_docker_config', ROOT / 'config.py')

    def test_bundled_wrapper_matches_pinned_metadata_and_real_runtime_validation(self):
        artifact = ROOT / 'static/lxp-wrapper.zip'
        metadata = json.loads((ROOT / 'static/lxp-wrapper-source.json').read_text(encoding='utf-8'))
        digest = sha256(artifact.read_bytes()).hexdigest()
        self.assertEqual(digest, BUNDLED_SHA256)
        self.assertEqual(metadata['sha256'], digest)
        self.assertEqual((metadata['format'], metadata['version'], metadata['archive']),
                         ('adapt-oer-bundled-lxp-wrapper', 1, artifact.name))
        self.assertEqual(metadata['sourceRepository'], 'https://github.com/com-digi-s/adapt-lxp')
        self.assertEqual(metadata['sourceCommit'], UPSTREAM_COMMIT)
        self.assertEqual(metadata['sourceDirectory'], 'dist')
        self.assertRegex(metadata['sourceDirectoryGitTree'], r'^[0-9a-f]{40}$')
        self.assertEqual(metadata['additionalUpstreamNotices'], ['LICENSE', 'libraries/react-bootstrap.min.js.LICENSE.txt'])
        with ZipFile(artifact) as archive:
            validate_archive_entries(archive)
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertIn('index.html', names)
            self.assertIn('config.json', names)
            self.assertFalse(any(name == 'scos' or name.startswith('scos/') for name in names))
            self.assertNotIn('lxp-contract.json', names)
            self.assertIn(b'GNU GENERAL PUBLIC LICENSE', archive.read('LICENSE'))
            react_notice = archive.read('libraries/react-bootstrap.min.js.LICENSE.txt')
            self.assertIn(b'MIT', react_notice)
            self.assertIn(b'Facebook', react_notice)
        with tempfile.TemporaryDirectory() as directory:
            _extract(artifact, directory)
            capability, persistence = _validate_wrapper(directory)
            self.assertEqual(capability['capability'], 'multi-framework')
            self.assertEqual(capability['packageContract'], 'adapt-lxp-multi-framework@1')
            self.assertEqual(persistence['bootstrap'], 'parent.App.attachAdaptPersistence')

    def test_missing_or_empty_wrapper_environment_uses_the_included_pinned_artifact(self):
        for values in ({}, {'LXP_WRAPPER_ZIP': '', 'LXP_WRAPPER_SHA256': ''}):
            with self.subTest(values=values):
                config = self.config(values)
                self.assertEqual(config.LXP_WRAPPER_ZIP, './static/lxp-wrapper.zip')
                self.assertTrue((ROOT / config.LXP_WRAPPER_ZIP).is_file())
                self.assertEqual(config.LXP_WRAPPER_SHA256, BUNDLED_SHA256)
                self.assertEqual(config.LXP_REGISTRY_PATH, './instance/lxp-source-registry.json')
                self.assertIsNone(config.LXP_CATALOGUE_STORE_PATH)

    def test_explicit_wrapper_and_state_environment_is_not_reinterpreted(self):
        values = {'LXP_WRAPPER_ZIP': '/approved/custom-wrapper.zip', 'LXP_WRAPPER_SHA256': 'a' * 64,
                  'LXP_ORIGIN_ID': 'stable-aat-origin', 'LXP_REGISTRY_PATH': '/state/sources.json',
                  'LXP_CATALOGUE_STORE_PATH': '/state/catalogue.json', 'BUILDS_DIR': '/aat-builds'}
        config = self.config(values)
        for name, value in values.items():
            self.assertEqual(getattr(config, name), value)

    def test_original_deployment_needs_no_new_environment_settings(self):
        config = self.config({'SESSION_SECRET': 'existing-session-secret',
                              'DB_HOST': 'db', 'MONGODB_PORT': '27017', 'DB_NAME': 'adapt'})
        self.assertEqual(config.SECRET_KEY, 'existing-session-secret')
        self.assertEqual(config.BUILDS_DIR, '/courses')
        self.assertEqual(config.MONGO_URI, 'mongodb://db:27017/adapt')
        self.assertEqual(config.LXP_WRAPPER_SHA256, BUNDLED_SHA256)
        self.assertEqual(config.LXP_ORIGIN_ID, 'aat-local')

    def test_secret_fallback_preserves_explicit_values_and_has_no_insecure_default(self):
        self.assertEqual(self.config({'SECRET_KEY': 'explicit', 'SESSION_SECRET': 'session',
                                      'SESSION_KEY': 'legacy'}).SECRET_KEY, 'explicit')
        self.assertEqual(self.config({'SESSION_SECRET': 'session', 'SESSION_KEY': 'legacy'}).SECRET_KEY, 'session')
        self.assertEqual(self.config({'SECRET_KEY': '', 'SESSION_SECRET': '', 'SESSION_KEY': 'legacy'}).SECRET_KEY, 'legacy')
        self.assertIsNone(self.config().SECRET_KEY)

    def test_default_course_mount_does_not_replace_configured_native_paths(self):
        for value in ('/explicit/courses', '/native/course directory'):
            self.assertEqual(self.config({'BUILDS_DIR': value}).BUILDS_DIR, value)
        self.assertEqual(self.config({'BUILDS_DIR': ''}).BUILDS_DIR, '/courses')

    def test_image_declares_only_its_default_state_directory_as_a_volume(self):
        declarations = [json.loads(line.split(None, 1)[1])
                        for line in (ROOT / 'Dockerfile').read_text().splitlines()
                        if line.startswith('VOLUME ')]
        self.assertEqual(declarations, [['/app/instance']])

    def test_native_example_matches_the_bundled_pin_and_persistence_paths(self):
        native = example_values(ROOT / 'credentials.env.dist')
        required = {'LXP_WRAPPER_ZIP', 'LXP_WRAPPER_SHA256', 'LXP_ORIGIN_ID', 'LXP_REGISTRY_PATH',
                    'LXP_CATALOGUE_STORE_PATH', 'LXP_CATALOGUE', 'LXP_ASSESSMENT_TEMPLATE',
                    'LXP_TARGET_PROFILE_ID', 'LXP_AAT_BUILD_PROFILE'}
        self.assertTrue(required <= set(native))
        self.assertTrue(all(native[name] for name in required))
        self.assertEqual(native['LXP_WRAPPER_SHA256'], BUNDLED_SHA256)
        self.assertEqual(native['LXP_TARGET_PROFILE_ID'], 'scorm-1.2-conservative')
        self.assertEqual(native['LXP_AAT_BUILD_PROFILE'], 'aat-core-build-v1')
        self.assertEqual(native['LXP_WRAPPER_ZIP'], './static/lxp-wrapper.zip')
        self.assertEqual(native['LXP_ORIGIN_ID'], native['AUTHORING_DOMAIN'])
        for name in ('LXP_REGISTRY_PATH', 'LXP_CATALOGUE_STORE_PATH'):
            self.assertEqual(Path(native[name]).parent, Path('instance'))
        self.assertEqual(native['LXP_CATALOGUE'], './static/lxp-catalogue.json')
        self.assertEqual(native['LXP_ASSESSMENT_TEMPLATE'], './static/assessment-template.zip')


class WrapperBuildHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load_module('docker_wrapper_builder_fixture', ROOT / 'tools/build_lxp_wrapper.py')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'upstream/dist'
        fixture_zip = self.root / 'wrapper-input.zip'
        # Reuse the small real capability/hash schema without discovering the
        # original TestCase twice or starting any database/application.
        fixtures.MixedPackageTest.write_wrapper(SimpleNamespace(wrapper=fixture_zip))
        _extract(fixture_zip, self.source)
        self.output = self.root / 'output/wrapper.zip'

    def test_repeated_build_is_byte_identical_independent_of_source_timestamps(self):
        before = tree_manifest(self.source)
        first_hash = self.builder.build_wrapper(self.source, self.output)
        for path in self.source.rglob('*'):
            if path.is_file():
                os.utime(path, (1700000000, 1700000000))
        second = self.root / 'output/second.zip'
        second_hash = self.builder.build_wrapper(self.source, second)
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())
        self.assertEqual(first_hash, sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(tree_manifest(self.source), before)
        with ZipFile(self.output) as archive:
            self.assertTrue(all(row.date_time == (1980, 1, 1, 0, 0, 0) for row in archive.infolist()))
            validate_archive_entries(archive)

    def test_asset_spaces_and_upstream_notices_are_preserved_without_a_source_prefix(self):
        asset = 'views/dashboard/static/A course material with spaces.pdf'
        (self.source / asset).parent.mkdir(parents=True)
        (self.source / asset).write_bytes(b'%PDF-1.4\nfixture with spaces\n')
        (self.source.parent / 'LICENSE').write_text('Fixture upstream license\n', encoding='utf-8')
        notice = self.source.parent / 'libraries/react-bootstrap.min.js.LICENSE.txt'
        notice.parent.mkdir()
        notice.write_text('Fixture MIT notice\n', encoding='utf-8')
        self.builder.build_wrapper(self.source, self.output)
        with ZipFile(self.output) as archive:
            validate_archive_entries(archive)
            self.assertEqual(archive.read(asset), (self.source / asset).read_bytes())
            self.assertEqual(archive.read('LICENSE'), b'Fixture upstream license\n')
            self.assertEqual(archive.read('libraries/react-bootstrap.min.js.LICENSE.txt'), b'Fixture MIT notice\n')
            self.assertIn('index.html', archive.namelist())
            self.assertFalse(any(name.startswith('dist/') for name in archive.namelist()))
        extracted = self.root / 'extracted'
        _extract(self.output, extracted)
        _validate_wrapper(extracted)
        self.assertEqual((extracted / asset).read_bytes(), (self.source / asset).read_bytes())

    def test_wrong_runtime_hash_is_rejected_before_creating_an_output(self):
        path = self.source / 'js/multiState.js'
        path.write_text(path.read_text() + '\n// changed without approval\n', encoding='utf-8')
        with self.assertRaisesRegex(PackageValidationError, 'SHA-256 mismatch'):
            self.builder.build_wrapper(self.source, self.output)
        self.assertFalse(self.output.parent.exists())

    def test_repository_archive_root_or_zip_file_is_not_a_built_wrapper_directory(self):
        archive_root = self.source.parent
        for invalid in (archive_root, self.root / 'wrapper-input.zip'):
            with self.subTest(invalid=invalid.name), self.assertRaises(ValueError):
                self.builder.build_wrapper(invalid, self.output)
            self.assertFalse(self.output.exists())

    def test_prepackaged_scos_or_product_contract_are_rejected(self):
        (self.source / 'scos').mkdir()
        with self.assertRaisesRegex(PackageValidationError, 'prepackaged'):
            self.builder.build_wrapper(self.source, self.output)
        (self.source / 'scos').rmdir()
        (self.source / 'lxp-contract.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(PackageValidationError, 'prepackaged'):
            self.builder.build_wrapper(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_artifact_is_never_overwritten(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'previous approved deployment artifact')
        with self.assertRaises(FileExistsError):
            self.builder.build_wrapper(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'previous approved deployment artifact')

    def test_output_inside_source_and_symbolic_source_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.builder.build_wrapper(self.source, self.source / 'wrapper.zip')
        external = self.root / 'external.svg'
        external.write_bytes(b'<svg/>')
        (self.source / 'external-link.svg').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            self.builder.build_wrapper(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_prints_the_hash_of_its_new_artifact_and_refuses_replacing_it(self):
        command = [sys.executable, str(ROOT / 'tools/build_lxp_wrapper.py'), str(self.source),
                   '--output', str(self.output)]
        environment = {'PATH': os.environ.get('PATH', os.defpath)}
        first = subprocess.run(command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), 'LXP_WRAPPER_SHA256=' + sha256(self.output.read_bytes()).hexdigest())
        before = self.output.read_bytes()
        second = subprocess.run(command, cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('Wrapper build failed', second.stderr)
        self.assertEqual(self.output.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
