"""Versioned local catalogue overrides for the existing single-admin GUI.

The configured catalogue is a read-only starting point. An explicit, separate
instance path holds edits; it never changes AAT sources, registry bindings or
existing export profiles. A caller must send the snapshot hash shown in its
form so a second browser cannot silently overwrite the first browser's edits.
"""
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from lxp_catalogue import DEFAULT_CATALOGUE, NUMBER, PackageValidationError, load_catalogue


MAX_CATALOGUE_BYTES = 2 * 1024 * 1024
_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')
_PREFIX = re.compile(r'^[A-Za-z]+$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class CatalogueConflictError(PackageValidationError):
    """The submitted form is stale; reload before making an explicit edit."""

    def __init__(self, current_sha256):
        self.current_sha256 = current_sha256
        super().__init__('Catalogue changed since this form was opened; reload before saving')


def _json_bytes(value):
    def check_keys(node):
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise ValueError('JSON object keys must be strings')
            for item in node.values():
                check_keys(item)
        elif isinstance(node, list):
            for item in node:
                check_keys(item)
        elif node is not None and not isinstance(node, (str, int, float, bool)):
            raise ValueError('Only JSON values are permitted')

    try:
        check_keys(value)
        data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PackageValidationError('Catalogue must be finite UTF-8 JSON: ' + str(error)) from error
    if len(data) >= MAX_CATALOGUE_BYTES:  # Reserve one byte for the persisted newline.
        raise PackageValidationError('Catalogue exceeds the 2 MiB size limit')
    return data


def _text(value, label, *, identity=False):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PackageValidationError(label + ' must be non-empty text without surrounding whitespace')
    if identity and not _ID.fullmatch(value):
        raise PackageValidationError(label + ' must be a safe stable ID')


def _validate(value):
    """Validate mutation inputs before passing them to the shared normalizer."""
    _json_bytes(value)
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        raise PackageValidationError('Production catalogue adapt-lxp-catalogue@1 required')
    _text(value.get('revision'), 'Catalogue revision')
    frameworks = value.get('frameworks')
    if not isinstance(frameworks, list):
        raise PackageValidationError('Catalogue frameworks must be a list')
    case_prefixes = {}
    for framework in frameworks:
        if not isinstance(framework, dict):
            raise PackageValidationError('Catalogue framework must be an object')
        for key in ('id', 'prefix', 'version', 'label'):
            _text(framework.get(key), 'Framework ' + key, identity=key == 'id')
        prefix = framework['prefix']
        if not _PREFIX.fullmatch(prefix):
            raise PackageValidationError('Framework prefix must contain ASCII letters only')
        if case_prefixes.setdefault(prefix.casefold(), prefix) != prefix:
            raise PackageValidationError('Framework prefixes may not differ only in letter case')
        modules, domains = framework.get('modules', []), framework.get('domains', [])
        if not isinstance(modules, list) or not isinstance(domains, list):
            raise PackageValidationError('Framework modules and domains must be lists')
        for module in modules:
            if not isinstance(module, dict):
                raise PackageValidationError('Catalogue module must be an object')
            _text(module.get('id'), 'Module id', identity=True)
            _text(module.get('label'), 'Module label')
            slots = module.get('courseNumbers', [])
            if not isinstance(slots, list) or any(not isinstance(n, str) or not NUMBER.fullmatch(n) for n in slots):
                raise PackageValidationError('Module courseNumbers must be a list of positive integer strings')
            labels = module.get('courseLabels', {})
            if not isinstance(labels, dict):
                raise PackageValidationError('Module courseLabels must be an object')
            for number, label in labels.items():
                if not NUMBER.fullmatch(number):
                    raise PackageValidationError('Course label number must be a positive integer string')
                _text(label, 'Course label')
        for domain in domains:
            if not isinstance(domain, dict):
                raise PackageValidationError('Catalogue domain must be an object')
            _text(domain.get('id'), 'Domain id', identity=True)
            _text(domain.get('label'), 'Domain label')
            for key in ('sourceTitles', 'sourceTitlePatterns'):
                entries = domain.get(key, [])
                if not isinstance(entries, list):
                    raise PackageValidationError('Domain ' + key + ' must be a list')
                for entry in entries:
                    _text(entry, 'Domain title marker')
                    if key == 'sourceTitlePatterns':
                        try:
                            re.compile(entry)
                        except (re.error, RecursionError, OverflowError) as error:
                            raise PackageValidationError('Invalid domain title pattern: ' + str(error)) from error
        defaults = framework.get('defaultDiagnosticIntents', {})
        if not isinstance(defaults, dict) or set(defaults) - {'entry', 'final'} or any(
                intent not in ('require', 'ifAvailable', 'omit') for intent in defaults.values()):
            raise PackageValidationError('Invalid entry/final diagnostic defaults')
        default_domain = framework.get('defaultDomainId')
        if default_domain is not None and not isinstance(default_domain, str):
            raise PackageValidationError('Default domain must be a domain ID')
        if 'defaultAssessmentOptions' in framework:
            from lxp_assessment_selection import normalize_assessment_options
            options = framework['defaultAssessmentOptions']
            normalized = normalize_assessment_options({framework['id']: options}, {'frameworks': [framework]})[framework['id']]
            for phase, settings in normalized.items():
                mode, count = settings['mode'], settings['questionCount']
                if mode == 'reference':
                    if prefix != 'DigComp' or framework['version'] != '2.2':
                        raise PackageValidationError('Public reference defaults are only available for DigComp 2.2')
                    if count not in (None, 24 if phase == 'entry' else 39):
                        raise PackageValidationError('Public reference defaults have fixed entry/final question counts')
                elif mode == 'all' and count is not None:
                    raise PackageValidationError('All-question defaults must not specify a questionCount')
                elif mode == 'balanced' and 'questionCount' in options[phase] and count is None:
                    raise PackageValidationError('Balanced defaults require a positive questionCount when specified')
    try:
        return load_catalogue(value)
    except (TypeError, KeyError, AttributeError) as error:
        raise PackageValidationError('Invalid catalogue structure: ' + str(error)) from error


def _decode(data):
    if len(data) > MAX_CATALOGUE_BYTES:
        raise PackageValidationError('Catalogue exceeds the 2 MiB size limit')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON object key: ' + key)
            result[key] = value
        return result

    def reject_constant(item):
        raise ValueError('Non-finite JSON number: ' + item)

    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PackageValidationError('Invalid UTF-8 catalogue JSON: ' + str(error)) from error
    return _validate(value)


def _read_file(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_CATALOGUE_BYTES + 1)
    return _decode(data), data


def _regular_target(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise PackageValidationError('Catalogue storage files may not be symlinks or non-regular files')


def _store_path(base, store_path):
    if store_path is None:
        return None
    if not isinstance(store_path, (str, Path)) or not str(store_path).strip():
        raise PackageValidationError('An explicit catalogue override path is required')
    path = Path(store_path).absolute()
    base_paths = [DEFAULT_CATALOGUE]
    if base is None or isinstance(base, (str, Path)):
        base_paths.append(DEFAULT_CATALOGUE if base is None else Path(base))
    if any(path.resolve() == Path(source).resolve() for source in base_paths):
        raise PackageValidationError('Catalogue override must be separate from the read-only configured source')
    _regular_target(path)
    return path


def _effective(base, path):
    if path is not None:
        _regular_target(path)
        if path.exists():
            return _read_file(path)
    if base is None or isinstance(base, (str, Path)):
        return _read_file(DEFAULT_CATALOGUE if base is None else base)
    value = _validate(base)
    return value, _json_bytes(value) + b'\n'


def _snapshot(catalogue):
    return {'catalogue': deepcopy(catalogue), 'sha256': hashlib.sha256(_json_bytes(catalogue)).hexdigest()}


def read_snapshot(base, store_path):
    """Read the override, or the unchanged configured source when none exists.

    Returns ``{'catalogue': dict, 'sha256': str}``. Reads do not create any
    directories or files. A broken override fails closed rather than silently
    reverting to a different catalogue. ``store_path=None`` is read-only mode.
    """
    try:
        catalogue, _ = _effective(base, _store_path(base, store_path))
        return _snapshot(catalogue)
    except OSError as error:
        raise PackageValidationError('Cannot read catalogue storage: ' + str(error)) from error


def _preserve_existing(current, candidate):
    frameworks = {item['id']: item for item in candidate['frameworks']}
    for old in current['frameworks']:
        new = frameworks.get(old['id'])
        if new is None:
            raise PackageValidationError('Existing frameworks may not be deleted or have their IDs changed')
        if any(old[key] != new[key] for key in ('prefix', 'version')):
            raise PackageValidationError('Existing framework prefix/version is immutable; add a new framework version instead')
        modules = {item['id']: item for item in new.get('modules', [])}
        for module in old.get('modules', []):
            replacement = modules.get(module['id'])
            if replacement is None or module['number'] != replacement['number']:
                raise PackageValidationError('Existing module IDs, framework ownership and numbers are immutable')
            if set(module.get('courseNumbers', [])) - set(replacement.get('courseNumbers', [])):
                raise PackageValidationError('Existing configured course slots may not be removed')
        domain_ids = {item['id'] for item in new.get('domains', [])}
        if any(item['id'] not in domain_ids for item in old.get('domains', [])):
            raise PackageValidationError('Existing domains may not be deleted or have their IDs changed')


def _protect_bindings(catalogue, protected_bindings):
    if protected_bindings is None:
        return
    if isinstance(protected_bindings, dict):
        if 'sources' in protected_bindings:
            protected_bindings = protected_bindings['sources']
        if isinstance(protected_bindings, dict):
            protected_bindings = ([protected_bindings] if 'frameworkId' in protected_bindings or 'binding' in protected_bindings
                                  else list(protected_bindings.values()))
    if not isinstance(protected_bindings, (list, tuple)):
        raise PackageValidationError('Protected source bindings must be a registry mapping or list')
    frameworks = {item['id']: item for item in catalogue['frameworks']}
    for source in protected_bindings:
        if not isinstance(source, dict):
            raise PackageValidationError('Protected source binding must be an object')
        binding = source.get('binding', source if 'frameworkId' in source else {})
        if not isinstance(binding, dict):
            raise PackageValidationError('Protected source binding must be an object')
        if not binding:
            continue  # An as-yet unassigned registry source has no binding.
        if any(not isinstance(binding.get(key), str) or not binding[key]
               for key in ('frameworkId', 'prefix', 'version', 'moduleNumber')):
            raise PackageValidationError('Protected source binding requires frameworkId, prefix, version and moduleNumber')
        framework = frameworks.get(binding.get('frameworkId'))
        if framework is None or any(framework[key] != binding.get(key) for key in ('prefix', 'version')):
            raise PackageValidationError('Catalogue does not preserve a registered source framework/version binding')
        module = next((item for item in framework.get('modules', []) if item['number'] == binding.get('moduleNumber')), None)
        if module is None or binding.get('moduleId', module['id']) != module['id']:
            raise PackageValidationError('Catalogue does not preserve a registered source module binding')
        if binding.get('domainId') is not None and binding['domainId'] not in {item['id'] for item in framework.get('domains', [])}:
            raise PackageValidationError('Catalogue does not preserve a protected domain binding')
        # Course numbers are discovered from published source titles. They need
        # not be predeclared in the editable catalogue to keep this binding.


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary_bytes(parent, prefix, data):
    descriptor, name = tempfile.mkstemp(prefix=prefix + '.', suffix='.tmp', dir=str(parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _backup(path, data):
    directory = path.with_name(path.name + '.backups')
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise PackageValidationError('Catalogue backup directory must be a real directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    backup = directory / (hashlib.sha256(data).hexdigest() + '.json')
    _regular_target(backup)
    temporary = _temporary_bytes(directory, 'backup', data)
    try:
        try:
            os.link(temporary, backup)
        except FileExistsError:
            _regular_target(backup)
            with backup.open('rb') as stream:
                existing = stream.read(MAX_CATALOGUE_BYTES + 1)
            if existing != data:
                raise PackageValidationError('Catalogue backup integrity check failed')
        _sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def save_catalogue(base, store_path, candidate, expected_sha256, protected_bindings=None):
    """Validate and atomically save one explicit admin edit.

    Raises ``CatalogueConflictError`` on a stale snapshot. Every actual change
    gets a server-generated revision and a content-addressed, before-write
    backup of the exact previous bytes. Existing identities cannot be renamed,
    moved or deleted. Recovery can submit a backup as a normal, validated edit
    against a fresh snapshot; it is never an unchecked file replacement.
    """
    candidate = _validate(candidate)
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise PackageValidationError('A valid catalogue snapshot hash is required')
    try:
        path = _store_path(base, store_path)
        if path is None:
            raise PackageValidationError('An explicit catalogue override path is required for saving')
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock = path.with_name(path.name + '.lock')
        _regular_target(lock)
        descriptor = os.open(str(lock), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'r+b') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PackageValidationError('Catalogue lock must be a regular file')
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            current, previous_bytes = _effective(base, path)
            current_snapshot = _snapshot(current)
            if expected_sha256 != current_snapshot['sha256']:
                raise CatalogueConflictError(current_snapshot['sha256'])
            _preserve_existing(current, candidate)
            _protect_bindings(candidate, protected_bindings)
            candidate['revision'] = current['revision']
            if candidate == current:
                return current_snapshot
            candidate['revision'] = 'catalogue-' + uuid.uuid4().hex
            data = _json_bytes(candidate) + b'\n'
            _backup(path, previous_bytes)
            temporary = _temporary_bytes(path.parent, path.name, data)
            try:
                _regular_target(path)
                os.replace(temporary, path)
                _sync_directory(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
            return _snapshot(candidate)
    except OSError as error:
        raise PackageValidationError('Cannot save catalogue storage: ' + str(error)) from error
