"""Frozen-source preflight and the production one-ZIP multi-framework exporter.

There is intentionally no inference from SCO IDs, paths or array positions.
Source registration, catalogue addresses and native numeric banks are three
different things.  A PreparedExport owns its immutable build snapshot until
``close()`` and must be confirmed with its exact preflight hash.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import binascii
import fcntl
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from adapt_persistence_bootstrap import bootstrap_runtime
from lxp_catalogue import (PackageValidationError, canonical_bytes, catalogue_sha256,
                           load_catalogue, normalize_title)
from lxp_diagnostics_articles import learning_source_view, restore_removed_nodes


PHASE_TAG = {'entry': 'et', 'final': 'at'}
SUPPORTED = {'mcq', 'gmcq', 'matching', 'slider', 'textinput', 'infai-dragndrop'}
GENERATOR_VERSION = 'adapt-oer-multi-framework@1'
SHA = re.compile(r'^[0-9a-f]{64}$')


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_sha256(path):
    return sha256_bytes(Path(path).read_bytes())


def _generator_files():
    base = Path(__file__).parent
    return {name: file_sha256(base / name) for name in ('adapt_persistence_bootstrap.py', 'lxp_catalogue.py', 'lxp_diagnostics_articles.py', 'lxp_export.py', 'lxp_multi_export.py')}


def _generator_sha256():
    return sha256_bytes(canonical_bytes(_generator_files()))


def _read(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def _write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def safe_relative(value, *, allow_suffix=False):
    """Return one decoded, local, canonical path; query/fragment stay separate."""
    if not isinstance(value, str) or not value or re.search(r'[\x00-\x20\x7f\\]', value):
        raise PackageValidationError('Unsafe launch/ZIP path: ' + repr(value))
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or (not allow_suffix and (parts.query or parts.fragment)):
        raise PackageValidationError('Unsafe absolute/external path: ' + value)
    if re.search(r'%(?![0-9a-fA-F]{2})', value):
        raise PackageValidationError('Malformed encoded path: ' + value)
    if re.search(r'%(?:2f|5c)', parts.path, re.I):
        raise PackageValidationError('Ambiguous encoded path separator: ' + value)
    path = unquote(parts.path, errors='strict')
    if re.search(r'%[0-9a-fA-F]{2}|[\x00-\x20\x7f\\:?#]', path) or path.startswith('/'):
        raise PackageValidationError('Unsafe decoded path: ' + value)
    segments = path.split('/')
    if any(segment in {'', '.', '..'} for segment in segments):
        raise PackageValidationError('Unsafe traversal/ambiguous path: ' + value)
    # Query/fragment are never used as identity, nor decoded into another URL.
    if re.search(r'[\x00-\x20\x7f\\]', unquote(parts.query + parts.fragment)):
        raise PackageValidationError('Unsafe launch suffix: ' + value)
    suffix = ('?' + parts.query if parts.query else '') + ('#' + parts.fragment if parts.fragment else '')
    return path + suffix


def safe_file_path(value):
    """Validate a physical ZIP/tree name, not a navigation URL.

    Spaces and literal URL punctuation are legitimate asset filename characters.
    They must not be stripped or interpreted as query/fragment separators. The
    stricter ``safe_relative`` remains mandatory for every launch URL. Decoded
    aliases are still returned so a file and its encoded alias cannot coexist.
    """
    if not isinstance(value, str) or not value or value.startswith('/') or re.search(r'[\x00-\x1f\x7f\\:]', value):
        raise PackageValidationError('Unsafe physical file path: ' + repr(value))
    if re.search(r'%(?![0-9a-fA-F]{2})|%(?:2f|5c)', value, re.I):
        raise PackageValidationError('Ambiguous encoded physical file path: ' + value)
    decoded = unquote(value, errors='strict')
    if decoded.startswith('/') or re.search(r'%[0-9a-fA-F]{2}|[\x00-\x1f\x7f\\:]', decoded) or any(segment in {'', '.', '..'} for segment in decoded.split('/')):
        raise PackageValidationError('Unsafe decoded physical file path: ' + value)
    return decoded


def validate_archive_entries(archive):
    seen, files = set(), set()
    for entry in archive.infolist():
        name = entry.filename[:-1] if entry.is_dir() else entry.filename
        try:
            decoded = safe_file_path(name)
        except (PackageValidationError, ValueError, UnicodeError) as error:
            raise PackageValidationError('Unsafe ZIP entry: ' + entry.filename) from error
        if decoded in seen or ((entry.external_attr >> 16) & 0o170000) == 0o120000:
            raise PackageValidationError('Unsafe ZIP entry (duplicate or symlink): ' + entry.filename)
        seen.add(decoded)
        if not entry.is_dir():
            files.add(decoded)
    if any('/'.join(path.split('/')[:i]) in files for path in seen for i in range(1, len(path.split('/')))):
        raise PackageValidationError('Unsafe ZIP entry overlaps a file path')


def tree_manifest(directory):
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise PackageValidationError('Published build directory missing or symlinked: ' + str(root))
    entries, decoded_entries, file_entries = {}, set(), set()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise PackageValidationError('Symlinks are not permitted in published sources: ' + str(path))
        relative = path.relative_to(root).as_posix()
        decoded = safe_file_path(relative)
        if decoded in decoded_entries:
            raise PackageValidationError('Source paths collide after URL decoding: ' + relative)
        decoded_entries.add(decoded)
        if path.is_file():
            entries[relative] = file_sha256(path)
            file_entries.add(decoded)
    if any('/'.join(path.split('/')[:i]) in file_entries for path in decoded_entries for i in range(1, len(path.split('/')))):
        raise PackageValidationError('Source path overlaps a file after URL decoding')
    if not entries:
        raise PackageValidationError('Published build is empty')
    return entries


def tree_sha256(directory):
    return sha256_bytes(canonical_bytes(tree_manifest(directory)))


def learning_compatibility_fingerprint(source):
    """Bind answers/completion to content AND its actual completion hierarchy.

    A component can become required through an unchanged component's ancestors.
    Course and hierarchy titles are presentation metadata; component prompts and
    all hierarchy flags/links/assessment settings retain their semantic meaning.
    """
    source, _ = learning_source_view(source)
    def node(value):
        return {key: deepcopy(item) for key, item in value.items() if key not in {'title', 'displayTitle'}}
    config = source.get('config', {})
    semantic = {'format': 'adapt-lxp-learning-compatibility', 'version': 1,
        'components': source['data']['components'],
        'hierarchy': {name: [node(item) for item in source['data'][name]] for name in ('contentObjects', 'articles', 'blocks')},
        'course': node(source['course']),
        'completionConfig': {key: deepcopy(config[key]) for key in ('_completionCriteria', '_assessment', '_requireCompletionOf', '_isTrackable', '_questionWeight', '_spoor') if key in config}}
    return sha256_bytes(canonical_bytes(semantic))


def _manifest_launch(directory, resource_id=None):
    manifest = Path(directory) / 'imsmanifest.xml'
    if not manifest.is_file():
        raise PackageValidationError('Published source has no imsmanifest.xml; no guessed index.html fallback is allowed')
    raw = manifest.read_bytes()
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw, re.I):
        raise PackageValidationError('Unsafe XML declaration in source manifest')
    try:
        xml = ET.fromstring(raw)
    except ET.ParseError as error:
        raise PackageValidationError('Invalid source manifest XML') from error
    def tag(node):
        return node.tag.rsplit('}', 1)[-1]
    resources = [node for node in xml.iter() if tag(node) == 'resource']
    ids = [node.get('identifier') for node in resources]
    if len(set(ids)) != len(ids):
        raise PackageValidationError('Duplicate manifest resource identifier')
    organizations = next((n for n in xml if tag(n) == 'organizations'), None)
    if organizations is None:
        raise PackageValidationError('Manifest has no organization/item launch reference')
    orgs = [n for n in organizations if tag(n) == 'organization']
    selected_orgs = [n for n in orgs if n.get('identifier') == organizations.get('default')] if organizations.get('default') else orgs
    if len(selected_orgs) != 1:
        raise PackageValidationError('Manifest launch organization is ambiguous')
    references = {n.get('identifierref') for n in selected_orgs[0].iter() if tag(n) == 'item' and n.get('identifierref')}
    if resource_id:
        references = references & {resource_id}
    if len(references) != 1:
        raise PackageValidationError('Manifest must select exactly one SCO resource; provide an explicit referenced resourceId')
    selected = [n for n in resources if n.get('identifier') in references]
    if len(selected) != 1:
        raise PackageValidationError('Manifest item references a missing resource')
    resource = selected[0]
    if any(key.rsplit('}', 1)[-1] == 'base' for node in xml.iter() for key in node.attrib):
        raise PackageValidationError('Manifest xml:base is not supported; publish an explicit package-relative href')
    href = resource.get('href')
    try:
        entry = safe_relative(href, allow_suffix=True)
    except PackageValidationError:
        raise
    except (ValueError, UnicodeError) as error:
        raise PackageValidationError('Invalid manifest launch path') from error
    path = urlsplit(entry).path
    target = Path(directory) / path
    if not target.is_file() or not target.resolve().is_relative_to(Path(directory).resolve()):
        raise PackageValidationError('Manifest launch resource is missing or escapes source: ' + str(href))
    return {'sha256': sha256_bytes(raw), 'resourceId': resource.get('identifier'), 'href': href}, entry


def _aat_core_manifest(source, metadata):
    """Explicit adapter for verified AAT Core-only production build artifacts.

    Such AAT downloads genuinely have no SCORM manifest. This adapter does not
    pretend one existed: the derived file is generated only in the delivered
    copy, and both its origin and every change are recorded in provenance.
    """
    build = source['build']
    if metadata.get('kind', 'aat-published') != 'aat-published' or metadata.get('profile') != 'aat-core-build-v1':
        raise PackageValidationError('Published source has no imsmanifest.xml; an explicit verified aat-core-build-v1 profile is required (no guessed index.html fallback)')
    config = _read(build / 'course/config.json')
    if config.get('_courseId') != metadata['courseId']:
        raise PackageValidationError('AAT Core build _courseId differs from authenticated source courseId')
    spoor = config.get('_spoor')
    if spoor is not None and (not isinstance(spoor, dict) or spoor.get('_isEnabled') is not False):
        raise PackageValidationError('Core-only adapter cannot replace an enabled/ambiguous Spoor launch')
    for name in ('index.html', 'adapt.css', 'adapt/js/scriptLoader.js', 'adapt/js/adapt.min.js', 'templates.js'):
        if not (build / name).is_file():
            raise PackageValidationError('AAT Core build anchor missing: ' + name)
    if (build / 'index_lms.html').exists():
        raise PackageValidationError('Ambiguous SCORM-like source without manifest; Core-only adapter refused')
    index = (build / 'index.html').read_text(encoding='utf-8')
    loader = (build / 'adapt/js/scriptLoader.js').read_text(encoding='utf-8')
    if not re.search(r'ADAPT_BUILD_TYPE\s*=\s*[\"\']production[\"\']', index) or 'adapt/js/scriptLoader.js' not in index or 'adapt.css' not in index or 'adapt/js/adapt.min.js' not in loader:
        raise PackageValidationError('Source does not match the known AAT Core production build anchors')
    xml = ET.Element('manifest', identifier='aat-core-build-adapter')
    orgs = ET.SubElement(xml, 'organizations', default='aat-core-organization')
    org = ET.SubElement(orgs, 'organization', identifier='aat-core-organization')
    ET.SubElement(org, 'title').text = source['course'].get('title', '')
    ET.SubElement(org, 'item', identifier='aat-core-item', identifierref='aat-core-resource')
    resources = ET.SubElement(xml, 'resources')
    resource = ET.SubElement(resources, 'resource', identifier='aat-core-resource', type='webcontent', href='index.html')
    ET.SubElement(resource, 'file', href='index.html')
    raw = ET.tostring(xml, encoding='utf-8', xml_declaration=True)
    manifest = {'sha256': sha256_bytes(raw), 'resourceId': 'aat-core-resource', 'href': 'index.html'}
    adapter = {'id': 'aat-core-build-v1', 'generatorSha256': _generator_sha256(),
               'originalManifestAbsent': True, 'generatedManifestSha256': manifest['sha256']}
    return manifest, 'index.html', adapter, raw


def _extract(path, destination):
    from lxp_export import safe_extract
    with ZipFile(path) as archive:
        safe_extract(archive, destination)


def _validate_wrapper(directory):
    root = Path(directory)
    if not (root / 'index.html').is_file() or not (root / 'config.json').is_file():
        raise PackageValidationError('LXP wrapper must contain index.html and config.json at its root')
    if (root / 'scos').exists() or (root / 'lxp-contract.json').exists():
        raise PackageValidationError('LXP wrapper must not contain prepackaged SCOs or a stale product contract')
    persistence_path = root / 'adapt-persistence-contract.json'
    if not persistence_path.is_file():
        raise PackageValidationError('LXP wrapper lacks persistence contract v1')
    persistence = _read(persistence_path)
    if persistence.get('version') != 1 or persistence.get('bootstrap') != 'parent.App.attachAdaptPersistence' or persistence.get('adapter') != 'js/corePersistence.js':
        raise PackageValidationError('Unsupported LXP persistence contract')
    capability_path = root / 'adapt-multi-framework-capabilities.json'
    if not capability_path.is_file():
        raise PackageValidationError('LXP wrapper lacks explicit multi-framework capability')
    capability = _read(capability_path)
    expected = {'capability': 'multi-framework', 'version': 1, 'packageContract': 'adapt-lxp-multi-framework@1',
                'persistenceContract': 1, 'launchContext': 'explicit-sco-id'}
    if any(capability.get(k) != v for k, v in expected.items()):
        raise PackageValidationError('Unsupported multi-framework wrapper capability/version')
    files = capability.get('implementationFiles')
    required_files = {'index.html', 'js/multiContract.js', 'js/multiState.js', 'js/multiBridge.js',
                      'js/multiAssessment.js', 'js/multiFramework.js', 'js/corePersistence.js'}
    if not isinstance(files, dict) or not required_files <= set(files):
        raise PackageValidationError('Wrapper capability must bind all seven runtime implementation files/hashes')
    for name, digest in files.items():
        name = safe_relative(name)
        if not isinstance(digest, str) or not SHA.fullmatch(digest) or not (root / name).is_file() or file_sha256(root / name) != digest:
            raise PackageValidationError('Wrapper implementation SHA-256 mismatch: ' + name)
    if capability.get('implementationSha256') != sha256_bytes(canonical_bytes(files)):
        raise PackageValidationError('Wrapper implementation manifest SHA-256 mismatch')
    app = (root / 'js/multiFramework.js').read_text(encoding='utf-8')
    index = (root / 'index.html').read_text(encoding='utf-8')
    implementation = '\n'.join((root / name).read_text(encoding='utf-8') for name in files if name.endswith(('.js', '.html')))
    contract_loader = 'MULTI_FRAMEWORK_CONTRACT' in index and re.search(r'import\s*\(\s*[\"\']\./js/multiFramework\.js[\"\']\s*\)', index)
    if 'attachAdaptPersistence' not in app or not contract_loader or 'frameworkId' not in implementation or 'assessmentId' not in implementation:
        raise PackageValidationError('Capability declaration does not match a multi-framework implementation')
    return capability, persistence


def _metadata_for(builds, source_metadata):
    if isinstance(source_metadata, list):
        if len(builds) != len(source_metadata):
            raise PackageValidationError('source_metadata count differs from selected sources')
        values = source_metadata
    elif isinstance(source_metadata, dict):
        values = [source_metadata.get(str(build), source_metadata.get(str(Path(build).resolve()))) for build in builds]
    else:
        raise PackageValidationError('source_metadata is mandatory for every source in multi-framework mode')
    result = []
    for value in values:
        if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip() for k in ('originId', 'courseId', 'currentTitle')):
            raise PackageValidationError('Each source requires originId, immutable courseId and currentTitle')
        if value.get('kind', 'aat-published') not in {'aat-published', 'native-registered'}:
            raise PackageValidationError('Production source kind must be aat-published or native-registered, never fixture')
        result.append(deepcopy(value))
    return result


def source_identity(metadata):
    return metadata['originId'] + '::' + metadata['courseId']


def _source_key(metadata):
    return json.dumps([metadata['originId'], metadata['courseId']], ensure_ascii=False, separators=(',', ':'))


def _registry(path):
    path = Path(path)
    if path.exists():
        value = _read(path)
        if value.get('format') != 'adapt-lxp-source-registry' or value.get('version') != 1:
            raise PackageValidationError('Unsupported persistent source registry')
        return value
    return {'format': 'adapt-lxp-source-registry', 'version': 1, 'sources': {}, 'identities': {}}


def _identity(registry, key, prefix):
    # Persistent UUID allocation is independent of title, order, path and hash.
    if key not in registry['identities']:
        registry['identities'][key] = prefix + '-' + uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]+', registry['identities'][key]):
        raise PackageValidationError('Unsafe technical identity in persistent registry')
    return registry['identities'][key]


def _save_registry(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    _write(temporary, value)
    os.replace(temporary, path)


def _question_domain(question, source, framework, metadata):
    domains = {d['id']: d for d in framework.get('domains', [])}
    explicit = (metadata.get('questionDomains') or {}).get(question['_id'])
    if explicit:
        if explicit not in domains:
            raise PackageValidationError('Unknown explicitly assigned diagnostic domain: ' + explicit)
        return explicit
    # Flat hierarchies only get a default if this catalogue explicitly grants it.
    blocks = {i['_id']: i for i in source['data']['blocks']}
    articles = {i['_id']: i for i in source['data']['articles']}
    objects = {i['_id']: i for i in source['data']['contentObjects']}
    try:
        page = objects[articles[blocks[question['_parentId']]['_parentId']]['_parentId']]
    except KeyError as error:
        raise PackageValidationError('Question has a broken page/article/block hierarchy: ' + question['_id']) from error
    chain, seen = [], set()
    node = page
    while node.get('_parentId') in objects:
        if node['_id'] in seen:
            raise PackageValidationError('Cyclic question hierarchy')
        seen.add(node['_id'])
        node = objects[node['_parentId']]
        chain.append(node)
    if node.get('_parentId') not in {'course', source['course'].get('_id')}:
        raise PackageValidationError('Question has a missing parent menu')
    if chain:
        title = chain[-1].get('title', '').strip()
        for domain in domains.values():
            if title in domain.get('sourceTitles', []):
                return domain['id']
            if any(re.match(pattern, title, re.I) for pattern in domain.get('sourceTitlePatterns', [])):
                return domain['id']
    elif framework.get('defaultDomainId'):
        return framework['defaultDomainId']
    raise PackageValidationError('No explicitly configured diagnostic domain for question ' + question['_id'])


def _validate_question(question, default_weight=1):
    component = question.get('_component')
    if component not in SUPPORTED:
        raise PackageValidationError('Unsupported diagnostic component: ' + str(component) + ' (' + str(question.get('_id')) + ')')
    if question.get('_isAvailable') is False:
        raise PackageValidationError('Tagged question is individually unavailable: ' + question['_id'])
    if not any(re.sub(r'<[^>]+>', '', str(question.get(key, ''))).strip() for key in ('title', 'displayTitle', 'body', 'instruction')):
        raise PackageValidationError('Question has no visible prompt: ' + question['_id'])
    items = question.get('_items', [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PackageValidationError('Invalid native question item structure: ' + question['_id'])
    if component in {'mcq', 'gmcq'} and (not items or not any(i.get('_shouldBeSelected') is True for i in items)):
        raise PackageValidationError('MCQ has no correct answer: ' + question['_id'])
    # Native MatchingModel permits alternative correct options, but permits
    # exactly one active option per row. Preserve every authored accepted key;
    # alternatives do not add extra attainable points to that row.
    if component == 'matching' and (not items or any(not isinstance(i.get('_options'), list) or not i['_options'] or any(not isinstance(o, dict) for o in i['_options']) or not any(o.get('_isCorrect') is True for o in i['_options']) for i in items)):
        raise PackageValidationError('Matching question has no valid correct options: ' + question['_id'])
    if component == 'textinput':
        def valid_answers(value):
            return isinstance(value, list) and bool(value) and all(isinstance(answer, str) and answer.strip() for answer in value)
        generic = question.get('_answers')
        if not items or (generic is not None and (not isinstance(generic, list) or len(generic) < len(items) or not all(valid_answers(group) for group in generic))) or (generic is None and any(not valid_answers(i.get('_answers', i.get('answers'))) for i in items)):
            raise PackageValidationError('Textinput question has no accepted answers: ' + question['_id'])
    if component == 'slider':
        def number(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        start, end, step = question.get('_scaleStart'), question.get('_scaleEnd'), question.get('_scaleStep', 1)
        answer, correct_range = question.get('_correctAnswer'), question.get('_correctRange')
        if not number(start) or not number(end) or not number(step) or start >= end or step <= 0:
            raise PackageValidationError('Slider has an invalid native scale: ' + question['_id'])
        # SliderModel uses a falsy empty single-answer field to select its
        # authored range. A range may extend beyond the visible scale; only
        # its intersection with actual native slider values can be selected.
        if answer is not None and answer != '':
            try:
                parsed_answer = float(answer)
            except (TypeError, ValueError):
                parsed_answer = float('nan')
            if isinstance(answer, bool) or not math.isfinite(parsed_answer) or not start <= parsed_answer <= end or not math.isclose((parsed_answer - start) / step, round((parsed_answer - start) / step)):
                raise PackageValidationError('Slider correct answer is not on its native scale: ' + question['_id'])
        elif not isinstance(correct_range, dict) or not number(correct_range.get('_bottom')) or not number(correct_range.get('_top')) or correct_range['_bottom'] > correct_range['_top'] or math.ceil((max(start, correct_range['_bottom']) - start) / step) > math.floor((min(end, correct_range['_top']) - start) / step):
            raise PackageValidationError('Slider question has no configured correct answer/range: ' + question['_id'])
    if component == 'infai-dragndrop' and not items:
        raise PackageValidationError('Drag-and-drop question has no items: ' + question['_id'])
    points = question.get('_questionWeight', default_weight)
    if not isinstance(points, (int, float)) or isinstance(points, bool) or not math.isfinite(points) or points <= 0:
        raise PackageValidationError('Question weight must be a finite positive number: ' + question['_id'])
    if component in {'mcq', 'gmcq'}:
        return sum(i.get('_shouldBeSelected') is True for i in items) * points
    if component == 'matching':
        return len(items) * points
    if component == 'infai-dragndrop':
        accepted_values = [i.get('accepted') for i in items]
        if any(not value or not (isinstance(value, str) or isinstance(value, list) and all(isinstance(v, str) and v for v in value)) for value in accepted_values):
            raise PackageValidationError('Drag-and-drop question has no accepted targets: ' + question['_id'])
        maximum = sum(1 if isinstance(value, str) else len(value) for value in accepted_values)
        return maximum * points
    return points


def _explicit_navigation(catalogue, sources, contract):
    """Resolve explicit source references, never historical taxonomy URL text."""
    selected = {_source_key(item['metadata']): item for item in sources}
    by_id = {item['sco']['id']: item for item in sources}
    def resolve(reference, framework_id):
        if isinstance(reference, str):
            item = by_id.get(reference)
        elif isinstance(reference, dict) and reference.get('originId') and reference.get('courseId'):
            item = selected.get(_source_key(reference))
        else:
            item = None
        if not item or item['sco']['frameworkId'] != framework_id:
            raise PackageValidationError('Explicit navigation references an unselected or foreign-framework course')
        return item
    for pathway in catalogue.get('pathways', []):
        refs = pathway.get('courseRefs', pathway.get('courseIds'))
        if not pathway.get('id') or not pathway.get('label') or not isinstance(refs, list) or not refs:
            raise PackageValidationError('Invalid explicit catalogue pathway')
        contract['pathways'].append({'id': pathway['id'], 'frameworkId': pathway['frameworkId'], 'label': pathway['label'],
            'courseIds': [resolve(ref, pathway['frameworkId'])['sco']['id'] for ref in refs]})
    for term in catalogue.get('glossary', []):
        if not term.get('id') or not term.get('term') or not isinstance(term.get('targets'), list) or not term['targets']:
            raise PackageValidationError('Invalid explicit catalogue glossary term')
        result = {k: deepcopy(term[k]) for k in ('id', 'frameworkId', 'term')}
        if 'definition' in term:
            result['definition'] = term['definition']
        result['targets'] = []
        for target in term['targets']:
            item = resolve(target.get('scoId', target), term['frameworkId'])
            content_ids = {v['_id'] for values in item['source']['data'].values() for v in values}
            if target.get('contentId') not in content_ids:
                raise PackageValidationError('Explicit glossary target content ID does not exist')
            result['targets'].append({'scoId': item['sco']['id'], 'contentId': target['contentId']})
        contract['glossary'].append(result)


@dataclass
class PreparedExport:
    contract: dict
    preview: dict
    preflight_sha256: str
    _temporary: object
    _sources: list
    _wrapper_original: Path
    _wrapper_sha256: str
    _template_original: Path
    _template_sha256: str
    _catalogue_original: object
    _catalogue_sha256: str
    _report: dict
    _candidates: dict
    _closed: bool = False

    def close(self):
        if not self._closed:
            self._temporary.cleanup()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _prepare_multi_framework_export(builds, wrapper_path, template_path, *, source_metadata,
        registry_path, catalogue=None, package_id, revision, title, diagnostics=None,
        framework_versions=None, required_courses=None, target_profile_id='scorm-1.2-conservative',
        expected_wrapper_sha256=None, legacy_import=False, confirm_reassignment=False):
    """Freeze actual published bytes, validate, normalize and return a preview.

    ``diagnostics`` is keyed by framework ID or prefix, then entry/final intent.
    ``required_courses`` is keyed by ``originId::courseId`` (default: required).
    Native sources must already have an explicit registry entry with
    ``nativeRegistered: true``; copying an arbitrary directory is not registration.
    """
    from lxp_export import published_course
    builds = [Path(build).resolve() for build in builds]
    if not builds:
        raise PackageValidationError('Select at least one published learning course')
    if any(not isinstance(v, str) or not v.strip() for v in (package_id, revision, title, target_profile_id)):
        raise PackageValidationError('Stable package ID, revision, title and target profile are mandatory')
    metadata = _metadata_for(builds, source_metadata)
    if len({_source_key(m) for m in metadata}) != len(metadata):
        raise PackageValidationError('Duplicate selected source origin/course identity')
    original_catalogue = catalogue
    normalized_catalogue = load_catalogue(catalogue)
    allowed_diagnostics = {value for f in normalized_catalogue['frameworks'] for value in (f['id'], f['prefix'])}
    if diagnostics is not None and (not isinstance(diagnostics, dict) or any(key not in allowed_diagnostics for key in diagnostics)):
        raise PackageValidationError('Diagnostic request contains an unknown framework key')
    if any(not isinstance(value, dict) or set(value) - set(PHASE_TAG) for value in (diagnostics or {}).values()):
        raise PackageValidationError('Diagnostic request contains an unknown phase')
    if required_courses is not None and (not isinstance(required_courses, dict) or set(required_courses) - {source_identity(m) for m in metadata}):
        raise PackageValidationError('Required-course selection refers to an unselected source')
    if framework_versions is not None and (not isinstance(framework_versions, dict) or set(framework_versions) - {f['prefix'] for f in normalized_catalogue['frameworks']}):
        raise PackageValidationError('Unknown framework version-selection prefix')
    catalogue_hash = catalogue_sha256(normalized_catalogue)
    registry = _registry(registry_path)
    temporary = tempfile.TemporaryDirectory(prefix='adapt-oer-multi-')
    try:
        frozen = Path(temporary.name)
        wrapper_path, template_path = Path(wrapper_path).resolve(), Path(template_path).resolve()
        wrapper_hash, template_hash = file_sha256(wrapper_path), file_sha256(template_path)
        if expected_wrapper_sha256 and wrapper_hash != expected_wrapper_sha256.lower():
            raise PackageValidationError('LXP wrapper SHA-256 mismatch')
        shutil.copyfile(wrapper_path, frozen / 'wrapper.zip')
        shutil.copyfile(template_path, frozen / 'template.zip')
        root = frozen / 'wrapper'
        _extract(frozen / 'wrapper.zip', root)
        capability, persistence = _validate_wrapper(root)
        contract = {'format': 'adapt-lxp-multi-framework', 'version': 1, 'exampleOnly': False,
            'package': {'id': package_id, 'revision': revision, 'title': title, 'catalogueRevision': normalized_catalogue['revision'],
                        'catalogueSha256': catalogue_hash, 'outerScorm': '1.2', 'manifestLaunch': 'index.html', 'targetProfileId': target_profile_id},
            'frameworks': [], 'modules': [], 'scos': [], 'domains': [], 'questions': [],
            'assessments': [], 'pathways': [], 'glossary': [],
            'completion': {'policy': 'all-required-learning-courses', 'outerScorePolicy': 'no-cross-framework-score'}}
        sources, addresses, versions, used_frameworks, candidates = [], set(), {}, {}, {}
        report = {'format': 'adapt-lxp-provenance', 'version': 2, 'packageId': package_id, 'revision': revision,
                  'wrapperSha256': wrapper_hash, 'wrapperCapability': capability, 'persistenceContract': persistence,
                  'catalogueRevision': normalized_catalogue['revision'], 'catalogueSha256': catalogue_hash,
                  'courses': [], 'tests': []}
        for index, (build, meta) in enumerate(zip(builds, metadata)):
            key = _source_key(meta)
            previous = registry['sources'].get(key, {})
            if meta.get('kind', 'aat-published') == 'native-registered' and not previous.get('nativeRegistered'):
                raise PackageValidationError('Native source must be explicitly registered before export: ' + source_identity(meta))
            original_hash = tree_sha256(build)
            frozen_build = frozen / 'sources' / str(index)
            shutil.copytree(build, frozen_build)
            if tree_sha256(frozen_build) != original_hash or tree_sha256(build) != original_hash:
                raise PackageValidationError('Published source changed while freezing; publish/preview again')
            source = published_course(frozen_build)
            if meta.get('kind', 'aat-published') == 'aat-published' and '_courseId' in source['config'] and source['config']['_courseId'] != meta['courseId']:
                raise PackageValidationError('Published AAT _courseId differs from authenticated source courseId: ' + meta['courseId'])
            runtime = frozen_build / 'adapt/js/adapt.min.js'
            if not runtime.is_file():
                raise PackageValidationError('Published source has no supported Adapt runtime: ' + meta['courseId'])
            try:
                bootstrap_runtime(runtime.read_bytes())
            except (ValueError, UnicodeError) as error:
                raise PackageValidationError('Source persistence bootstrap unavailable: ' + meta['courseId'] + ': ' + str(error)) from error
            published_title = source['course'].get('title', '')
            if meta['currentTitle'] != published_title:
                raise PackageValidationError('Bitte Kurs erneut veröffentlichen: aktueller AAT-Title stimmt nicht mit veröffentlichtem Title überein (' + meta['courseId'] + ')')
            bound = previous.get('binding', {})
            # Prefix changes must not apply the old framework's version to a new framework.
            old_prefix = bound.get('prefix')
            current_prefix = re.match(r'^([A-Za-z]+)', published_title.strip())
            bound_version = bound.get('version') if not current_prefix or current_prefix[1] == old_prefix else None
            mapping = normalize_title(published_title, normalized_catalogue, framework_versions=framework_versions,
                                      legacy_import=legacy_import, bound_version=bound_version)
            if not current_prefix and legacy_import and not previous.get('legacyApproved'):
                raise PackageValidationError('Numeric legacy import requires a known explicitly approved source registration')
            binding = {k: mapping[k] for k in ('frameworkId', 'prefix', 'version', 'moduleNumber', 'courseNumber')}
            changed = bool(bound and bound != binding)
            if changed and not confirm_reassignment:
                raise PackageValidationError('Zuordnungsänderung bitte ausdrücklich bestätigen: ' + source_identity(meta))
            address = (mapping['frameworkId'], mapping['moduleNumber'], mapping['courseNumber'])
            if address in addresses:
                raise PackageValidationError('Duplicate selected framework/module/course address: ' + published_title)
            addresses.add(address)
            if mapping['prefix'] in versions and versions[mapping['prefix']] != mapping['version']:
                raise PackageValidationError('Two versions of the same framework cannot be merged in one package')
            versions[mapping['prefix']] = mapping['version']
            framework = next(f for f in normalized_catalogue['frameworks'] if f['id'] == mapping['frameworkId'])
            used_frameworks[framework['id']] = framework
            sco_id = previous.get('scoId') or _identity(registry, 'course:' + key, 'sco')
            if not isinstance(sco_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]+', sco_id):
                raise PackageValidationError('Unsafe technical SCO identity in source registry')
            registry['sources'][key] = {**previous, 'originId': meta['originId'], 'courseId': meta['courseId'], 'scoId': sco_id,
                                       'binding': binding, 'lastPublishedTitle': published_title}
            adapter, derived_manifest = None, None
            if (frozen_build / 'imsmanifest.xml').is_file():
                manifest, entry = _manifest_launch(frozen_build, meta.get('resourceId'))
            else:
                manifest, entry, adapter, derived_manifest = _aat_core_manifest(source, meta)
            source_info = {'kind': meta.get('kind', 'aat-published'), 'originId': meta['originId'], 'courseId': meta['courseId'],
                'title': meta['currentTitle'], 'publishedTitle': published_title, 'language': source['language'],
                'buildSha256': original_hash, 'catalogueSha256': catalogue_hash, 'manifest': manifest,
                'componentFingerprint': learning_compatibility_fingerprint(source)}
            if adapter:
                source_info['launchAdapter'] = adapter
                source_info['manifestOrigin'] = 'generated-aat-core-build'
            required = (required_courses or {}).get(source_identity(meta), True)
            if not isinstance(required, bool):
                raise PackageValidationError('requiredForCompletion must be an explicit boolean')
            sco = {'id': sco_id, 'frameworkId': framework['id'], 'moduleId': mapping['moduleId'], 'number': mapping['courseNumber'],
                'role': 'learning', 'requiredForCompletion': required, 'label': mapping['label'], 'source': source_info,
                'launch': {'root': 'scos/' + sco_id, 'entry': entry, 'manifestHref': manifest['href']}}
            contract['scos'].append(sco)
            if not any(m['id'] == mapping['moduleId'] for m in contract['modules']):
                contract['modules'].append({'id': mapping['moduleId'], 'frameworkId': framework['id'], 'number': mapping['moduleNumber'], 'label': mapping['moduleLabel']})
            sources.append({'original': build, 'frozen': frozen_build, 'hash': original_hash, 'metadata': meta,
                            'source': source, 'sco': sco, 'mappingChanged': changed, 'derivedManifest': derived_manifest})
            report['courses'].append({'scoId': sco_id, 'source': deepcopy(source_info), 'mapping': binding,
                                     'mappingChanged': changed, 'fileManifest': tree_manifest(frozen_build), 'copyChanges': []})
        if not any(s['requiredForCompletion'] for s in contract['scos']):
            raise PackageValidationError('Leerer Pflichtumfang: mindestens ein Lernkurs muss verpflichtend sein')
        _explicit_navigation(normalized_catalogue, sources, contract)
        for framework_id, framework in sorted(used_frameworks.items()):
            diagnostics_for = (diagnostics or {}).get(framework_id, (diagnostics or {}).get(framework['prefix'], {}))
            if not isinstance(diagnostics_for, dict):
                raise PackageValidationError('Diagnostic intents must be configured per framework and phase')
            entry = {k: framework[k] for k in ('id', 'prefix', 'version', 'label')}
            entry['diagnostics'] = {}
            contract['frameworks'].append(entry)
            contract['domains'].extend({'frameworkId': framework_id, 'id': d['id'], 'label': d['label']} for d in framework.get('domains', []))
            for phase, tag in PHASE_TAG.items():
                intent = diagnostics_for.get(phase, framework.get('defaultDiagnosticIntents', {}).get(phase, 'ifAvailable'))
                if intent not in {'omit', 'ifAvailable', 'require'}:
                    raise PackageValidationError('Unknown diagnostic intent: ' + str(intent))
                availability = {'intent': intent, 'status': 'notProvided', 'reason': 'not-requested' if intent == 'omit' else 'no-assigned-questions'}
                entry['diagnostics'][phase] = availability
                if intent == 'omit':
                    continue
                selected = []
                for item in sources:
                    if item['sco']['frameworkId'] != framework_id:
                        continue
                    seen_originals = set()
                    for question in item['source']['data']['components']:
                        classes = set((question.get('_classes') or '').split())
                        if tag not in classes or 'self-assessment' in classes:
                            continue
                        if not question.get('_id') or question['_id'] in seen_originals:
                            raise PackageValidationError('Duplicate/absent original source question ID')
                        seen_originals.add(question['_id'])
                        points = _validate_question(question, item['source']['config'].get('_questionWeight', 1))
                        domain_id = _question_domain(question, item['source'], framework, item['metadata'])
                        q_id = _identity(registry, 'question:' + _source_key(item['metadata']) + ':' + question['_id'], 'q')
                        runtime_id = 'diagnostic-' + item['sco']['id'] + '-' + question['_id']
                        runtime_type = 'mcq' if question['_component'] == 'gmcq' else question['_component']
                        record = {'id': q_id, 'sourceCourseId': item['sco']['id'], 'sourceComponentId': question['_id'],
                                  'runtimeQuestionId': runtime_id, 'component': runtime_type,
                                  'sourceComponentType': question['_component'], 'runtimeComponent': runtime_type,
                                  'tags': sorted(classes & {'et', 'at'}), 'available': True, 'selfAssessment': False, 'maxPoints': points}
                        if runtime_type != question['_component']:
                            record['componentAdapter'] = 'gmcq-to-mcq-v1'
                        existing = next((q for q in contract['questions'] if q['id'] == q_id), None)
                        if existing is None:
                            contract['questions'].append(record)
                        selected.append((item, question, q_id, domain_id, points))
                if not selected:
                    if intent == 'require':
                        raise PackageValidationError(framework['prefix'] + ' ' + phase + ': required diagnostic has no assigned questions')
                    continue
                assessment_id = _identity(registry, 'assessment:' + package_id + ':' + framework_id + ':' + phase, 'as')
                sco_id = _identity(registry, 'assessment-sco:' + package_id + ':' + framework_id + ':' + phase, 'sco')
                banks, quiz_candidates = [], []
                grouped = {}
                for item, question, q_id, domain_id, points in selected:
                    grouped.setdefault((item['sco']['id'], domain_id), []).append((item, question, q_id, points))
                # Runtime numbers are local table keys, never encoded taxonomies.
                for runtime_bank, ((course_id, domain_id), questions) in enumerate(sorted(grouped.items()), 1):
                    bank_id = _identity(registry, 'bank:' + assessment_id + ':' + course_id + ':' + domain_id, 'bank')
                    banks.append({'id': bank_id, 'runtimeBankId': runtime_bank, 'targetCourseId': course_id, 'domainId': domain_id,
                                  'questionIds': sorted(q[2] for q in questions), 'maxPoints': sum(q[3] for q in questions)})
                    for item, question, q_id, points in sorted(questions, key=lambda q: q[2]):
                        quiz_candidates.append((question, item['source'], runtime_bank, item['sco']['id']))
                assessment = {'id': assessment_id, 'frameworkId': framework_id, 'scoId': sco_id, 'phase': phase, 'banks': banks}
                contract['assessments'].append(assessment)
                assessment_title = framework['label'] + (' – Einstiegstest' if phase == 'entry' else ' – Abschlusstest')
                contract['scos'].append({'id': sco_id, 'frameworkId': framework_id, 'role': phase, 'requiredForCompletion': False,
                    'label': assessment_title, 'source': {'kind': 'generated-assessment', 'assessmentId': assessment_id,
                        'generator': GENERATOR_VERSION, 'generatorSha256': _generator_sha256(), 'generatorFiles': _generator_files(), 'templateSha256': template_hash,
                        'catalogueSha256': catalogue_hash,
                        'questions': [{'questionId': q_id, 'sourceCourseId': item['sco']['id'], 'sourceComponentId': question['_id']} for item, question, q_id, _, _ in sorted(selected, key=lambda x: x[2])]},
                    'launch': {'root': 'scos/' + sco_id}})
                candidates[sco_id] = quiz_candidates
                availability.update(status='provided', reason=None)
        # Real generator output is validated during preflight, not deferred until after confirmation.
        from lxp_export import make_quiz
        for sco in contract['scos']:
            if sco['role'] == 'learning':
                continue
            target = frozen / 'generated' / sco['id']
            bootstrap = make_quiz(target, frozen / 'template.zip', candidates[sco['id']], sco['label'])
            manifest, entry = _manifest_launch(target)
            sco['launch'].update(entry=entry, manifestHref=manifest['href'])
            sco['source'].update(buildSha256=tree_sha256(target), manifest=manifest)
            report['tests'].append({'scoId': sco['id'], 'assessmentId': sco['source']['assessmentId'], 'persistenceBootstrap': bootstrap,
                                    'source': deepcopy(sco['source'])})
        contract['frameworks'].sort(key=lambda f: (f['prefix'], f['version']))
        contract['modules'].sort(key=lambda m: (m['frameworkId'], int(m['number'])))
        contract['scos'].sort(key=lambda s: (s['frameworkId'], s['role'], s.get('moduleId', ''), int(s.get('number', '0')), s['id']))
        contract['questions'].sort(key=lambda q: q['id'])
        contract['assessments'].sort(key=lambda a: (a['frameworkId'], a['phase']))
        report['diagnostics'] = {f['id']: f['diagnostics'] for f in contract['frameworks']}
        preview = {'package': deepcopy(contract['package']), 'frameworks': deepcopy(contract['frameworks']),
                   'modules': deepcopy(contract['modules']), 'scos': deepcopy(contract['scos']),
                   'assessments': deepcopy(contract['assessments']), 'diagnostics': deepcopy(report['diagnostics']),
                   'catalogueRevision': normalized_catalogue['revision'], 'catalogueSha256': catalogue_hash,
                   'warnings': ['Target LMS storage/runtime acceptance remains a separate human release gate.']}
        binding = {'contract': contract, 'wrapperSha256': wrapper_hash, 'templateSha256': template_hash,
                   'catalogueSha256': catalogue_hash, 'generatorSha256': _generator_sha256()}
        preflight_hash = sha256_bytes(canonical_bytes(binding))
        preview['preflightSha256'] = preflight_hash
        report['preflightSha256'] = preflight_hash
        _write(frozen / 'catalogue.json', normalized_catalogue)
        _save_registry(registry_path, registry)
        return PreparedExport(deepcopy(contract), preview, preflight_hash, temporary, sources,
            wrapper_path, wrapper_hash, template_path, template_hash, original_catalogue, catalogue_hash, report, candidates)
    except Exception:
        temporary.cleanup()
        raise


@contextmanager
def _registry_lock(registry_path):
    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def prepare_multi_framework_export(builds, wrapper_path, template_path, *, source_metadata,
        registry_path, catalogue=None, package_id, revision, title, diagnostics=None,
        framework_versions=None, required_courses=None, target_profile_id='scorm-1.2-conservative',
        expected_wrapper_sha256=None, legacy_import=False, confirm_reassignment=False):
    """Public, process-locked source-registry transaction; see implementation."""
    with _registry_lock(registry_path):
        return _prepare_multi_framework_export(builds, wrapper_path, template_path,
            source_metadata=source_metadata, registry_path=registry_path, catalogue=catalogue,
            package_id=package_id, revision=revision, title=title, diagnostics=diagnostics,
            framework_versions=framework_versions, required_courses=required_courses,
            target_profile_id=target_profile_id, expected_wrapper_sha256=expected_wrapper_sha256,
            legacy_import=legacy_import, confirm_reassignment=confirm_reassignment)


def get_source_binding(registry_path, *, origin_id, course_id):
    """Read one existing source binding for GUI display, without allocating IDs."""
    key = _source_key({'originId': origin_id, 'courseId': course_id})
    return deepcopy(_registry(registry_path)['sources'].get(key))


def register_native_source(registry_path, *, origin_id, course_id, current_title=None):
    """Explicitly register a native source once; callers must preserve these IDs."""
    if not origin_id or not course_id:
        raise PackageValidationError('Native registration requires stable origin/course IDs')
    with _registry_lock(registry_path):
        registry = _registry(registry_path)
        meta = {'originId': origin_id, 'courseId': course_id}
        key = _source_key(meta)
        previous = registry['sources'].get(key, {})
        registry['sources'][key] = {**previous, 'originId': origin_id, 'courseId': course_id,
                                   'nativeRegistered': True, 'scoId': previous.get('scoId') or _identity(registry, 'course:' + key, 'sco')}
        _save_registry(registry_path, registry)
        return registry['sources'][key]['scoId']


def approve_legacy_source(registry_path, *, origin_id, course_id, current_title,
                          catalogue=None, framework_version=None):
    """Record an author's explicit known-source numeric DigComp import decision.

    This does not import browser state or infer an old package identity. Such
    migrations still require their own explicit verified alias table.
    """
    from lxp_catalogue import LEGACY_TITLE
    if not origin_id or not course_id or not LEGACY_TITLE.fullmatch((current_title or '').strip()):
        raise PackageValidationError('Legacy approval requires a known origin/course and a numeric published title')
    mapping = normalize_title(current_title, catalogue, legacy_import=True,
        framework_versions={'DigComp': framework_version} if framework_version else None)
    with _registry_lock(registry_path):
        registry = _registry(registry_path)
        key = _source_key({'originId': origin_id, 'courseId': course_id})
        previous = registry['sources'].get(key, {})
        binding = {k: mapping[k] for k in ('frameworkId', 'prefix', 'version', 'moduleNumber', 'courseNumber')}
        if previous.get('binding') and previous['binding'] != binding:
            raise PackageValidationError('Legacy approval cannot silently reassign an existing source binding')
        registry['sources'][key] = {**previous, 'originId': origin_id, 'courseId': course_id,
            'legacyApproved': True, 'legacyApprovedTitle': current_title, 'binding': binding,
            'scoId': previous.get('scoId') or _identity(registry, 'course:' + key, 'sco')}
        _save_registry(registry_path, registry)
        return registry['sources'][key]['scoId']


def _outer_manifest(root, contract):
    ET.register_namespace('', 'http://www.imsproject.org/xsd/imscp_rootv1p1p2')
    ET.register_namespace('adlcp', 'http://www.adlnet.org/xsd/adlcp_rootv1p2')
    ns = '{http://www.imsproject.org/xsd/imscp_rootv1p1p2}'
    adl = '{http://www.adlnet.org/xsd/adlcp_rootv1p2}'
    manifest = ET.Element(ns + 'manifest', identifier=contract['package']['id'], version='1')
    metadata = ET.SubElement(manifest, ns + 'metadata')
    ET.SubElement(metadata, ns + 'schema').text = 'ADL SCORM'
    ET.SubElement(metadata, ns + 'schemaversion').text = '1.2'
    orgs = ET.SubElement(manifest, ns + 'organizations', default='lxp-organization')
    org = ET.SubElement(orgs, ns + 'organization', identifier='lxp-organization')
    ET.SubElement(org, ns + 'title').text = contract['package']['title']
    item = ET.SubElement(org, ns + 'item', identifier='lxp-item', identifierref='lxp-resource')
    ET.SubElement(item, ns + 'title').text = contract['package']['title']
    resources = ET.SubElement(manifest, ns + 'resources')
    resource = ET.SubElement(resources, ns + 'resource', {'identifier': 'lxp-resource', 'type': 'webcontent', 'href': 'index.html', adl + 'scormtype': 'sco'})
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and path != Path(root) / 'imsmanifest.xml':
            ET.SubElement(resource, ns + 'file', href=path.relative_to(root).as_posix())
    ET.ElementTree(manifest).write(Path(root) / 'imsmanifest.xml', encoding='utf-8', xml_declaration=True)


def build_multi_framework_export(prepared, *, confirmed_preflight_sha256, source_metadata=None):
    """Build exactly the server-frozen, confirmed preview or fail closed."""
    if not isinstance(prepared, PreparedExport) or prepared._closed:
        raise PackageValidationError('Preflight expired; create a new preview')
    if confirmed_preflight_sha256 != prepared.preflight_sha256:
        raise PackageValidationError('Preview confirmation does not match preflight SHA-256')
    # Mutating a public preview/contract object cannot alter the confirmed package.
    binding = {'contract': prepared.contract, 'wrapperSha256': prepared._wrapper_sha256,
               'templateSha256': prepared._template_sha256, 'catalogueSha256': prepared._catalogue_sha256,
               'generatorSha256': _generator_sha256()}
    if sha256_bytes(canonical_bytes(binding)) != prepared.preflight_sha256:
        raise PackageValidationError('Prepared contract/implementation changed; create a new preview')
    if file_sha256(prepared._wrapper_original) != prepared._wrapper_sha256 or file_sha256(prepared._template_original) != prepared._template_sha256:
        raise PackageValidationError('Wrapper/template changed since preview; create a new preview')
    if catalogue_sha256(load_catalogue(prepared._catalogue_original)) != prepared._catalogue_sha256:
        raise PackageValidationError('Catalogue changed since preview; create a new preview')
    for item in prepared._sources:
        if tree_sha256(item['original']) != item['hash'] or tree_sha256(item['frozen']) != item['hash']:
            raise PackageValidationError('Published source changed since preview; create a new preview')
    if source_metadata is not None:
        fresh = _metadata_for([s['original'] for s in prepared._sources], source_metadata)
        if any(any(a.get(k) != b['metadata'].get(k) for k in ('originId', 'courseId', 'currentTitle', 'kind', 'profile', 'resourceId', 'questionDomains')) for a, b in zip(fresh, prepared._sources)):
            raise PackageValidationError('AAT source selection/title changed since preview; publish and create a new preview')
    frozen = Path(prepared._temporary.name)
    # Start each build from the checked wrapper snapshot, so repeated confirmation
    # cannot accumulate courses or copy changes from an earlier attempt.
    with tempfile.TemporaryDirectory(prefix='adapt-oer-multi-output-') as output_dir:
        root = Path(output_dir)
        shutil.copytree(frozen / 'wrapper', root, dirs_exist_ok=True)
        capability, _ = _validate_wrapper(root)
        report = deepcopy(prepared._report)
        for item in prepared._sources:
            target = root / item['sco']['launch']['root']
            shutil.copytree(item['frozen'], target)
            from lxp_export import published_course
            raw_source = published_course(item['frozen'])
            learning, removals = learning_source_view(raw_source)
            json_changes = []
            for name, entries in removals.items():
                relative = 'course/' + raw_source['language'] + '/' + name + '.json'
                path = target / relative
                before_bytes = path.read_bytes()
                before_hash = sha256_bytes(before_bytes)
                _write(path, learning['data'][name])
                json_changes.append({'path': relative, 'kind': 'diagnostic-only-article-removal',
                    'input_sha256': before_hash, 'output_sha256': file_sha256(path),
                    'input_bytes_base64': base64.b64encode(before_bytes).decode('ascii'),
                    'source_semantic_sha256': sha256_bytes(canonical_bytes(raw_source['data'][name])),
                    'removedIds': [entry['node']['_id'] for entry in entries], 'removedNodes': entries})
            runtime = target / 'adapt/js/adapt.min.js'
            # The sole content exception is the explicitly validated hidden,
            # optional diagnostic article subtree above. All original native
            # learning content and tracking IDs otherwise remain unchanged.
            if not runtime.is_file():
                raise PackageValidationError('Source has no supported Adapt runtime: ' + item['sco']['id'])
            try:
                patched, metadata = bootstrap_runtime(runtime.read_bytes())
            except (ValueError, UnicodeError) as error:
                raise PackageValidationError('Source persistence bootstrap unavailable: ' + item['sco']['id'] + ': ' + str(error)) from error
            runtime.write_bytes(patched)
            course_report = next(c for c in report['courses'] if c['scoId'] == item['sco']['id'])
            course_report['copyChanges'] = ([{'path': 'adapt/js/adapt.min.js', 'kind': 'persistence-bootstrap', **metadata}]
                                           if metadata['input_sha256'] != metadata['output_sha256'] else [])
            if item.get('derivedManifest'):
                (target / 'imsmanifest.xml').write_bytes(item['derivedManifest'])
                course_report['copyChanges'].append({'path': 'imsmanifest.xml', 'kind': 'generated-core-build-manifest',
                    'adapterId': 'aat-core-build-v1', 'originalAbsent': True, 'output_sha256': file_sha256(target / 'imsmanifest.xml'),
                    'generatorSha256': _generator_sha256()})
            course_report['copyChanges'].extend(json_changes)
            course_report['deliveredFileManifest'] = tree_manifest(target)
        for sco in prepared.contract['scos']:
            if sco['role'] != 'learning':
                source = frozen / 'generated' / sco['id']
                if tree_sha256(source) != sco['source']['buildSha256']:
                    raise PackageValidationError('Generated assessment changed since preview')
                shutil.copytree(source, root / sco['launch']['root'])
        _write(root / 'lxp-contract.json', prepared.contract)
        _write(root / 'lxp-catalogue.json', _read(frozen / 'catalogue.json'))
        _write(root / 'package-provenance.json', report)
        # Legacy catalogue is not a parallel source of course/role authority.
        config = _read(root / 'config.json')
        for key in ('SCOS', 'pathways', 'glossary'):
            config.pop(key, None)
        config['packageContract'] = 'lxp-contract.json'
        config['MULTI_FRAMEWORK_CONTRACT'] = 'lxp-contract.json'
        _write(root / 'config.json', config)
        validate_product_contract(prepared.contract, root)
        _outer_manifest(root, prepared.contract)
        output = BytesIO()
        with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        with ZipFile(output) as archive:
            validate_archive_entries(archive)
        output.seek(0)
        return output


def _original_learning_source(published, directory, sco, report):
    """Verify exact copy deltas and restore diagnostic-only source questions.

    Hidden helper nodes are intentionally absent from delivered learning JSON.
    Their original JSON bytes are bound to the source file manifest. Indexed
    removed nodes must reconstruct that exact source JSON, not merely match a
    separately editable semantic hash in the same provenance record.
    """
    if not isinstance(report, dict) or report.get('source') != sco['source']:
        raise PackageValidationError('Learning copy provenance differs from its source contract')
    before, delivered = report.get('fileManifest'), tree_manifest(directory)
    if not isinstance(before, dict) or sha256_bytes(canonical_bytes(before)) != sco['source']['buildSha256'] or report.get('deliveredFileManifest') != delivered:
        raise PackageValidationError('Learning original/delivered file manifest differs from provenance')
    changes = report.get('copyChanges')
    if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
        raise PackageValidationError('Invalid learning copy change provenance')
    paths = [c.get('path') for c in changes]
    difference = {path for path in set(before) | set(delivered) if before.get(path) != delivered.get(path)}
    if any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths) or set(paths) != difference:
        raise PackageValidationError('Learning copy change list differs from actual source/delivery delta')
    removals, semantic_hashes, original_json = {}, {}, {}
    for change in changes:
        path = change['path']
        kind = change.get('kind')
        if change.get('output_sha256') != delivered.get(path):
            raise PackageValidationError('Learning copy output SHA-256 differs from actual file')
        if kind == 'generated-core-build-manifest':
            if path != 'imsmanifest.xml' or path in before or change.get('originalAbsent') is not True or sco['source'].get('manifestOrigin') != 'generated-aat-core-build':
                raise PackageValidationError('Invalid generated learning manifest copy change')
            continue
        if change.get('input_sha256') != before.get(path) or path not in before:
            raise PackageValidationError('Learning copy input SHA-256 differs from original file manifest')
        if kind == 'persistence-bootstrap' and path == 'adapt/js/adapt.min.js':
            continue
        name = Path(path).stem
        if kind != 'diagnostic-only-article-removal' or name not in {'articles', 'blocks', 'components'} or path != 'course/' + published['language'] + '/' + name + '.json':
            raise PackageValidationError('Unsupported learning copy change')
        encoded = change.get('input_bytes_base64')
        if not isinstance(encoded, str):
            raise PackageValidationError('Diagnostic-only original source JSON bytes are missing')
        try:
            original_bytes = base64.b64decode(encoded, validate=True)
            original_json[name] = json.loads(original_bytes)
        except (binascii.Error, ValueError, UnicodeError) as error:
            raise PackageValidationError('Invalid diagnostic-only original source JSON bytes') from error
        if sha256_bytes(original_bytes) != before[path]:
            raise PackageValidationError('Diagnostic-only original source JSON bytes differ from source file SHA-256')
        entries = change.get('removedNodes')
        if not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get('node'), dict) for e in entries) or change.get('removedIds') != [e['node'].get('_id') for e in entries]:
            raise PackageValidationError('Diagnostic-only removed IDs differ from recorded nodes')
        removals[name] = entries
        semantic_hashes[name] = change.get('source_semantic_sha256')
    if learning_source_view(published)[1]:
        raise PackageValidationError('Diagnostic-only article was not removed from learning delivery')
    original = restore_removed_nodes(published, removals) if removals else published
    if any(canonical_bytes(original['data'][name]) != canonical_bytes(values) for name, values in original_json.items()):
        raise PackageValidationError('Diagnostic-only reconstructed nodes differ from original source JSON bytes')
    if any(sha256_bytes(canonical_bytes(original['data'][name])) != digest for name, digest in semantic_hashes.items()):
        raise PackageValidationError('Diagnostic-only reconstructed source semantic SHA-256 differs from provenance')
    return original


def validate_product_contract(contract, root):
    """Validate actual delivered provenance, references and navigation boundaries."""
    if contract.get('format') != 'adapt-lxp-multi-framework' or contract.get('version') != 1 or contract.get('exampleOnly') is not False:
        raise PackageValidationError('Unsupported production package contract')
    root = Path(root)
    collections = ('frameworks', 'modules', 'scos', 'questions', 'assessments', 'pathways', 'glossary')
    for name in collections:
        values = contract.get(name)
        if not isinstance(values, list) or len({v.get('id') for v in values}) != len(values) or any(not v.get('id') for v in values):
            raise PackageValidationError('Duplicate/invalid contract identities: ' + name)
    frameworks = {f['id']: f for f in contract['frameworks']}
    modules = {m['id']: m for m in contract['modules']}
    scos = {s['id']: s for s in contract['scos']}
    questions = {q['id']: q for q in contract['questions']}
    provenance = _read(root / 'package-provenance.json')
    course_reports = provenance.get('courses', [])
    if not isinstance(course_reports, list) or any(not isinstance(c, dict) for c in course_reports) or len({c.get('scoId') for c in course_reports}) != len(course_reports):
        raise PackageValidationError('Invalid learning course provenance')
    course_reports = {c.get('scoId'): c for c in course_reports}
    original_sources = {}
    if len({f.get('prefix') for f in frameworks.values()}) != len(frameworks):
        raise PackageValidationError('Multiple versions of one framework are not supported')
    if any(m.get('frameworkId') not in frameworks for m in modules.values()):
        raise PackageValidationError('Unknown module framework')
    roots, addresses = [], set()
    for sco in scos.values():
        if sco.get('frameworkId') not in frameworks or sco.get('role') not in {'learning', 'entry', 'final'}:
            raise PackageValidationError('Invalid SCO framework/role')
        launch = sco.get('launch', {})
        prefix = safe_relative(launch.get('root'))
        entry = safe_relative(launch.get('entry'), allow_suffix=True)
        combined = safe_relative(prefix + '/' + entry, allow_suffix=True)
        if not prefix.startswith('scos/') or not (root / urlsplit(combined).path).is_file():
            raise PackageValidationError('Delivered SCO launch is missing or unsafe')
        if any(prefix == other or prefix.startswith(other + '/') or other.startswith(prefix + '/') for other in roots):
            raise PackageValidationError('SCO target directories collide or overlap')
        roots.append(prefix)
        source = sco.get('source', {})
        if not SHA.fullmatch(source.get('buildSha256', '')):
            raise PackageValidationError('Missing actual source build SHA-256')
        manifest, actual_entry = _manifest_launch(root / prefix, source.get('manifest', {}).get('resourceId'))
        if actual_entry != entry or manifest != source.get('manifest') or launch.get('manifestHref') != manifest['href']:
            raise PackageValidationError('Delivered launch disagrees with original manifest selection')
        if sco['role'] == 'learning':
            module = modules.get(sco.get('moduleId'))
            if not module or module['frameworkId'] != sco['frameworkId'] or source.get('kind') not in {'aat-published', 'native-registered'}:
                raise PackageValidationError('Invalid learning source/module')
            if any(not source.get(k) for k in ('originId', 'courseId', 'language')) or source.get('title') != source.get('publishedTitle') or not SHA.fullmatch(source.get('catalogueSha256', '')):
                raise PackageValidationError('Incomplete real learning source provenance')
            from lxp_export import published_course
            published = published_course(root / prefix)
            original_sources[sco['id']] = _original_learning_source(published, root / prefix, sco, course_reports.get(sco['id']))
            if published['language'] != source['language'] or published['course'].get('title') != source['publishedTitle']:
                raise PackageValidationError('Delivered published title/language differs from provenance')
            if source['kind'] == 'aat-published' and '_courseId' in published['config'] and published['config']['_courseId'] != source['courseId']:
                raise PackageValidationError('Delivered AAT _courseId differs from source provenance')
            if source.get('componentFingerprint') != learning_compatibility_fingerprint(published):
                raise PackageValidationError('Delivered learning content/completion hierarchy differs from original source')
            if not isinstance(sco.get('requiredForCompletion'), bool):
                raise PackageValidationError('Learning completion requirement must be boolean')
            address = (sco['frameworkId'], sco['moduleId'], sco['number'])
            if address in addresses:
                raise PackageValidationError('Duplicate learning address')
            addresses.add(address)
        elif source.get('kind') != 'generated-assessment' or any(not SHA.fullmatch(source.get(k, '')) for k in ('generatorSha256', 'templateSha256', 'catalogueSha256')) or tree_sha256(root / prefix) != source['buildSha256']:
            raise PackageValidationError('Incomplete generated assessment provenance')
    for question in questions.values():
        course = scos.get(question.get('sourceCourseId'))
        maximum = question.get('maxPoints')
        if not course or course['role'] != 'learning' or not question.get('sourceComponentId') or not question.get('runtimeQuestionId') or question.get('component') not in SUPPORTED or question.get('available') is not True or question.get('selfAssessment') is not False or not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or not math.isfinite(maximum) or maximum <= 0 or not isinstance(question.get('tags'), list):
            raise PackageValidationError('Invalid production diagnostic question')
        original_type = question.get('sourceComponentType', question['component'])
        runtime_type = question.get('runtimeComponent', question['component'])
        if runtime_type != question['component'] or (original_type != runtime_type and (original_type, runtime_type, question.get('componentAdapter')) != ('gmcq', 'mcq', 'gmcq-to-mcq-v1')):
            raise PackageValidationError('Undeclared or unsupported source/runtime component conversion')
        original_source = original_sources[course['id']]
        original_components = original_source['data']['components']
        original = next((q for q in original_components if q.get('_id') == question['sourceComponentId']), None)
        if not original or original.get('_component') != original_type or sorted(set((original.get('_classes') or '').split()) & {'et', 'at'}) != question['tags'] or not math.isclose(_validate_question(original, original_source['config'].get('_questionWeight', 1)), maximum):
            raise PackageValidationError('Declared diagnostic component type differs from actual source')
    used_assessment_scos, used_phases, global_bank_ids = set(), set(), set()
    for assessment in contract['assessments']:
        sco = scos.get(assessment.get('scoId'))
        if not sco or sco['frameworkId'] != assessment.get('frameworkId') or sco['role'] != assessment.get('phase'):
            raise PackageValidationError('Assessment SCO/phase/framework mismatch')
        phase_key = (assessment['frameworkId'], assessment['phase'])
        if sco['id'] in used_assessment_scos or phase_key in used_phases:
            raise PackageValidationError('Duplicate assessment SCO or phase')
        used_assessment_scos.add(sco['id'])
        used_phases.add(phase_key)
        from lxp_export import published_course
        generated = published_course(root / sco['launch']['root'])
        generated_questions = {q['_id']: q for q in generated['data']['components']}
        generated_blocks = {b['_id']: b for b in generated['data']['blocks']}
        runtime_ids, seen_questions, bank_ids = set(), set(), set()
        for bank in assessment.get('banks', []):
            native = bank.get('runtimeBankId')
            course = scos.get(bank.get('targetCourseId'))
            if not isinstance(native, int) or isinstance(native, bool) or native < 1 or native in runtime_ids or not bank.get('id') or bank['id'] in bank_ids or bank['id'] in global_bank_ids:
                raise PackageValidationError('Invalid local runtime bank table')
            runtime_ids.add(native)
            bank_ids.add(bank.get('id'))
            global_bank_ids.add(bank['id'])
            if not course or course['role'] != 'learning' or course['frameworkId'] != assessment['frameworkId']:
                raise PackageValidationError('Cross-framework bank target')
            if not any(d['id'] == bank.get('domainId') and d['frameworkId'] == assessment['frameworkId'] for d in contract['domains']):
                raise PackageValidationError('Unknown explicit bank domain')
            maximum = 0
            if not bank.get('questionIds'):
                raise PackageValidationError('Empty production question bank')
            for q_id in bank.get('questionIds', []):
                question = questions.get(q_id)
                if not question or question['sourceCourseId'] != course['id'] or PHASE_TAG[assessment['phase']] not in question['tags']:
                    raise PackageValidationError('Invalid/cross-framework bank question')
                source_identity_key = (question['sourceCourseId'], question['sourceComponentId'])
                if source_identity_key in seen_questions:
                    raise PackageValidationError('Original source question counted twice in assessment')
                seen_questions.add(source_identity_key)
                maximum += question['maxPoints']
                component = generated_questions.get(question['runtimeQuestionId'])
                block = generated_blocks.get(component.get('_parentId')) if component else None
                if not component or component.get('_component') != question.get('runtimeComponent', question['component']) or not block or block.get('_assessment', {}).get('_quizBankID') != native or not math.isclose(_validate_question(component), question['maxPoints']):
                    raise PackageValidationError('Delivered diagnostic component/bank/scoring differs from product contract')
            if not isinstance(bank.get('maxPoints'), (int, float)) or isinstance(bank['maxPoints'], bool) or not math.isfinite(bank['maxPoints']) or not math.isclose(maximum, bank['maxPoints']):
                raise PackageValidationError('Bank maximum differs from its actual source questions')
        if not seen_questions:
            raise PackageValidationError('Empty provided assessment')
        expected_questions = {qid for bank in assessment['banks'] for qid in bank['questionIds']}
        provenance = sco['source'].get('questions', [])
        if sco['source'].get('assessmentId') != assessment['id'] or len(provenance) != len(expected_questions) or {ref.get('questionId') for ref in provenance} != expected_questions or any(ref.get('sourceCourseId') != questions[ref['questionId']]['sourceCourseId'] or ref.get('sourceComponentId') != questions[ref['questionId']]['sourceComponentId'] for ref in provenance):
            raise PackageValidationError('Generated source question provenance differs from assessment banks')
    if any(sco['role'] != 'learning' and (sco['id'] not in used_assessment_scos or sco.get('requiredForCompletion') is not False) for sco in scos.values()):
        raise PackageValidationError('Assessment SCO without explicit non-required test assignment')
    for framework in frameworks.values():
        for phase in PHASE_TAG:
            status = framework.get('diagnostics', {}).get(phase, {})
            actual = [a for a in contract['assessments'] if a['frameworkId'] == framework['id'] and a['phase'] == phase]
            if status.get('status') == 'provided':
                if len(actual) != 1 or status.get('reason') is not None or status.get('intent') == 'omit':
                    raise PackageValidationError('Provided diagnostic status contradicts generated SCO')
            elif status.get('status') == 'notProvided':
                reason = 'not-requested' if status.get('intent') == 'omit' else 'no-assigned-questions'
                if actual or status.get('intent') == 'require' or status.get('reason') != reason:
                    raise PackageValidationError('Missing diagnostic status conceals a required test')
            else:
                raise PackageValidationError('Invalid diagnostic availability status')
    for pathway in contract.get('pathways', []):
        if not pathway.get('courseIds') or any(course not in scos or scos[course]['role'] != 'learning' or scos[course]['frameworkId'] != pathway.get('frameworkId') for course in pathway['courseIds']):
            raise PackageValidationError('Invalid explicit learning pathway references')
    for term in contract.get('glossary', []):
        for target in term.get('targets', []):
            if target.get('scoId') not in scos or scos[target['scoId']]['frameworkId'] != term.get('frameworkId') or not target.get('contentId'):
                raise PackageValidationError('Invalid explicit glossary target references')
    if not any(s['role'] == 'learning' and s.get('requiredForCompletion') is True for s in scos.values()):
        raise PackageValidationError('Empty required learning-course set')
    return True
