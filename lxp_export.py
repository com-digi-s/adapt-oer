"""Validated LXP packages built from immutable, actually published AAT JSON.

This deliberately supports the existing numeric LXP catalogue, not arbitrary
competency frameworks. Unknown mappings and empty diagnostic tests are errors.
"""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED
from adapt_persistence_bootstrap import bootstrap_runtime


QUESTION_TYPES = {'mcq', 'gmcq', 'matching', 'slider', 'textinput', 'infai-dragndrop'}
DATA_FILES = ('contentObjects', 'articles', 'blocks', 'components')


class PackageValidationError(ValueError):
    pass


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def safe_extract(archive, destination):
    target = Path(destination).resolve()
    for entry in archive.infolist():
        resolved = (target / entry.filename).resolve()
        if not resolved.is_relative_to(target) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise PackageValidationError('Unsafe ZIP entry: ' + entry.filename)
    archive.extractall(target)


def published_course(build):
    build = Path(build)
    config = read_json(build / 'course/config.json')
    language = config.get('_defaultLanguage', 'en')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', language):
        raise PackageValidationError('Invalid published course language')
    data_dir = build / 'course' / language
    course = read_json(data_dir / 'course.json')
    data = {name: read_json(data_dir / (name + '.json')) for name in DATA_FILES}
    return {'build': build, 'language': language, 'course': course, 'data': data}


def course_mapping(title, wrapper_config):
    match = re.match(r'^(\d)\.(\d)(?=\D|$)', title.strip())
    if not match:
        raise PackageValidationError('LXP mapping missing: course title must start with an existing numeric ID, e.g. "4.2 …". Other frameworks require an explicit future mapping.')
    module, course_id = 'M' + match[1], match[2]
    module_config = wrapper_config.get('SCOS', {}).get(module)
    if not module_config or not any(str(item.get('id')) == course_id for item in module_config.get('courses', [])):
        raise PackageValidationError('LXP catalogue has no course ' + module + '/' + course_id)
    return module + '/' + course_id


def question_domain(question, source):
    data = source['data']
    blocks = {item['_id']: item for item in data['blocks']}
    articles = {item['_id']: item for item in data['articles']}
    objects = {item['_id']: item for item in data['contentObjects']}
    try:
        article = articles[blocks[question['_parentId']]['_parentId']]
        page = objects[article['_parentId']]
    except KeyError as error:
        raise PackageValidationError('Question has a broken page/article/block hierarchy: ' + question['_id']) from error
    parent = objects.get(page.get('_parentId'))
    if parent is None:
        if page.get('_parentId') not in {'course', source['course'].get('_id')}:
            raise PackageValidationError('Question page has a missing parent menu: ' + question['_id'])
        # Flat Course > Page is a normal AAT/Core-only source structure.
        # Existing LXP has no domain UI for this: explicit default is domain 1.
        return 1, 'flat-course-default:Verstehen'
    domain = parent
    seen = set()
    while domain.get('_parentId') in objects:
        if domain['_id'] in seen:
            raise PackageValidationError('Cyclic content-object hierarchy')
        seen.add(domain['_id'])
        domain = objects[domain['_parentId']]
    title = domain.get('title', '').strip()
    for index, name in enumerate(('Verstehen', 'Anwenden', 'Bewerten'), 1):
        if re.match(r'^' + chr(64 + index) + r'(?:\b|[.\-_:])', title, re.I) or title.casefold().startswith(name.casefold()):
            return index, title
    raise PackageValidationError('Unknown diagnostic domain "' + title + '". Use A/Verstehen, B/Anwenden or C/Bewerten; do not rely on object sort order.')


def replace_asset_paths(value, source, namespace):
    if isinstance(value, dict):
        return {key: replace_asset_paths(item, source, namespace) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_asset_paths(item, source, namespace) for item in value]
    if isinstance(value, str):
        value = value.replace('course/' + source['language'] + '/assets/', 'course/en/assets/' + namespace + '/course/')
        return re.sub(r'(?<![\w/])assets/', 'course/en/assets/' + namespace + '/root/', value)
    return value


def remove_oer_helper(data):
    removed = {item['_id'] for item in data['contentObjects'] if item.get('title') == 'Adapt|OER Komponenten'}
    for name in DATA_FILES:
        items = data[name]
        while True:
            children = {item['_id'] for item in items if item.get('_parentId') in removed}
            if children <= removed:
                break
            removed |= children
        data[name] = [item for item in items if item['_id'] not in removed]


def make_quiz(destination, template_path, candidates, title):
    if not candidates:
        raise PackageValidationError(title + ' contains no supported graded questions. Mark actual question components with et/at and publish again.')
    with ZipFile(template_path) as archive:
        safe_extract(archive, destination)
    directory = Path(destination) / 'course/en'
    result_component = read_json(directory / 'components.json')[0]
    result_block = read_json(directory / 'blocks.json')[0]
    result_block.pop('_assessment', None)
    article_id = read_json(directory / 'articles.json')[0]['_id']
    components, blocks = [], []
    for index, (original, source, bank, namespace) in enumerate(candidates):
        question = replace_asset_paths(deepcopy(original), source, namespace)
        question_id = 'diagnostic-' + namespace + '-' + original['_id']
        block_id = 'block-' + question_id
        question.update({'_id': question_id, '_parentId': block_id, '_layout': 'full', '_attempts': 1, '_isOptional': False,
                         '_isAvailable': True, '_isHidden': False, '_isVisible': True, '_canShowFeedback': False})
        if not any(re.sub(r'<[^>]+>', '', str(question.get(key, ''))).strip() for key in ('displayTitle', 'body', 'instruction')):
            if not question.get('title', '').strip():
                raise PackageValidationError('Question has no visible prompt: ' + original['_id'])
            question['displayTitle'] = question['title']
        question.setdefault('_onScreen', {})['_percentInviewVertical'] = 100
        if question['_component'] == 'gmcq':
            # The bundled quiz runtime has MCQ but no GMCQ plugin. Keep every
            # option graphic and selection flag while using its native MCQ model.
            question['_component'] = 'mcq'
            for item in question.get('_items', []):
                graphic = item.get('_graphic') or {}
                source_url = graphic.get('src') or graphic.get('_src')
                if source_url:
                    from html import escape
                    item['text'] = '<img src="' + escape(source_url, quote=True) + '" alt="' + escape(graphic.get('alt', ''), quote=True) + '">' + item.get('text', '')
        if question['_component'] == 'mcq':
            items = question.get('_items', [])
            count = sum(bool(item.get('_shouldBeSelected')) for item in items)
            if not count:
                raise PackageValidationError('MCQ has no correct answer: ' + original['_id'])
            question['_selectable'] = 1 if count == 1 else len(items)
        components.append(question)
        blocks.append({'_id': block_id, '_parentId': article_id, '_type': 'block', 'title': '', 'displayTitle': '',
                       'body': '', '_classes': '', '_trackingId': index, '_isAvailable': True, '_isOptional': False,
                       '_assessment': {'_quizBankID': bank}})
        for asset_type, source_path in [('course', source['build'] / 'course' / source['language'] / 'assets'), ('root', source['build'] / 'assets')]:
            if source_path.is_dir():
                shutil.copytree(source_path, directory / 'assets' / namespace / asset_type, dirs_exist_ok=True)
    result_block['_trackingId'] = len(blocks)
    blocks.append(result_block)
    components.append(result_component)
    write_json(directory / 'components.json', components)
    write_json(directory / 'blocks.json', blocks)
    course = read_json(directory / 'course.json')
    course['title'] = title
    course['_latestTrackingId'] = len(blocks) - 1
    write_json(directory / 'course.json', course)
    for name in ('contentObjects', 'articles'):
        values = read_json(directory / (name + '.json'))
        values[0]['title'] = title
        write_json(directory / (name + '.json'), values)
    runtime = Path(destination) / 'adapt/js/adapt.min.js'
    output, metadata = bootstrap_runtime(runtime.read_bytes())
    runtime.write_bytes(output)
    return metadata


def package_courses(builds, wrapper_path, template_path, expected_wrapper_sha256=None):
    """Return an unmodified-source package stream. No writes to published builds."""
    wrapper_path = Path(wrapper_path)
    digest = hashlib.sha256(wrapper_path.read_bytes()).hexdigest()
    if expected_wrapper_sha256 and digest != expected_wrapper_sha256.lower():
        raise PackageValidationError('LXP wrapper SHA-256 mismatch')
    output = BytesIO()
    with tempfile.TemporaryDirectory(prefix='adapt-oer-lxp-') as temp:
        root = Path(temp)
        with ZipFile(wrapper_path) as archive:
            safe_extract(archive, root)
        if not (root / 'index.html').is_file() or not (root / 'config.json').is_file():
            raise PackageValidationError('LXP wrapper ZIP must contain index.html and config.json at its root')
        contract_path = root / 'adapt-persistence-contract.json'
        if not contract_path.is_file():
            raise PackageValidationError('LXP wrapper lacks persistence contract v1. Use a tested wrapper providing App.attachAdaptPersistence; old upstream wrappers cannot restore diagnostic answers.')
        contract = read_json(contract_path)
        if contract.get('version') != 1 or contract.get('bootstrap') != 'parent.App.attachAdaptPersistence':
            raise PackageValidationError('Unsupported LXP persistence contract')
        adapter = contract.get('adapter', '')
        if adapter != 'js/corePersistence.js' or not (root / adapter).is_file():
            raise PackageValidationError('LXP persistence adapter missing from wrapper')
        if not (root / 'js/app.js').is_file() or 'attachAdaptPersistence' not in (root / 'js/app.js').read_text(encoding='utf-8'):
            raise PackageValidationError('LXP wrapper App does not expose attachAdaptPersistence')
        # Reject a wrapper carrying old courses rather than silently shipping them.
        if (root / 'scos').exists():
            raise PackageValidationError('LXP wrapper must not contain prepackaged scos')
        config = read_json(root / 'config.json')
        selections, candidates = [], {'et': [], 'at': []}
        report = {'format': 1, 'wrapper_sha256': digest, 'persistence_contract': contract, 'courses': [], 'tests': {}}
        for index, build in enumerate(builds):
            source = published_course(build)
            title = source['course'].get('title', '')
            course_id = course_mapping(title, config)
            if course_id in selections:
                raise PackageValidationError('Duplicate selected LXP course mapping: ' + course_id)
            selections.append(course_id)
            namespace = 'course' + str(index + 1)
            course_report = {'id': course_id, 'title': title, 'language': source['language'], 'questions': []}
            for question in source['data']['components']:
                classes = set((question.get('_classes') or '').split())
                tags = classes & {'et', 'at'}
                if not tags or 'self-assessment' in classes:
                    continue
                if question.get('_isAvailable') is False:
                    raise PackageValidationError('Tagged question is individually unavailable: ' + question['_id'] + '. Keep question components available; hide only the supplementary article if required.')
                if question.get('_component') not in QUESTION_TYPES:
                    raise PackageValidationError('Unsupported diagnostic component: ' + question.get('_component', '?') + ' (' + question['_id'] + ')')
                domain, reason = question_domain(question, source)
                bank = int(course_id.replace('M', '').replace('/', '') + str(domain))
                for tag in tags:
                    candidates[tag].append((question, source, bank, namespace))
                course_report['questions'].append({'id': question['_id'], 'type': question['_component'], 'tests': sorted(tags), 'bank': bank, 'domain': reason})
            report['courses'].append(course_report)
            target = root / 'scos' / course_id
            shutil.copytree(source['build'], target)
            runtime = target / 'adapt/js/adapt.min.js'
            output_runtime, runtime_metadata = bootstrap_runtime(runtime.read_bytes())
            runtime.write_bytes(output_runtime)
            course_report['persistence_bootstrap'] = runtime_metadata
            remove_oer_helper(source['data'])
            for name, values in source['data'].items():
                write_json(target / 'course' / source['language'] / (name + '.json'), values)
        for tag, sco, title in [('et', 'M0', 'Einstiegstest'), ('at', 'MX', 'Abschlusstest')]:
            metadata = make_quiz(root / 'scos' / sco, template_path, candidates[tag], title)
            report['tests'][sco] = {'questions': len(candidates[tag]), 'banks': sorted({item[2] for item in candidates[tag]}), 'persistence_bootstrap': metadata}
        for module_id, module in config['SCOS'].items():
            selected_courses = [course for course in module.get('courses', []) if module_id + '/' + str(course['id']) in selections]
            module['disabled'] = module_id not in {'M0', 'MX'} and not selected_courses
            module['enabled'] = not module['disabled']
            for course in module.get('courses', []):
                mapping = module_id + '/' + str(course['id'])
                course['disabled'] = mapping not in selections
                course['enabled'] = not course['disabled']
                if mapping in selections:
                    course['label'] = next(item['title'] for item in report['courses'] if item['id'] == mapping)
        if 'pathways' in config:
            if not isinstance(config['pathways'], list):
                raise PackageValidationError('Unsupported LXP pathways configuration')
            pathways = []
            for pathway in config['pathways']:
                if not isinstance(pathway, dict) or not isinstance(pathway.get('courses'), list) or not all(isinstance(course, str) for course in pathway['courses']):
                    raise PackageValidationError('Unsupported LXP pathway course mapping')
                pathway['courses'] = [course for course in pathway['courses'] if course.replace('.', '/', 1) in selections]
                if pathway['courses']:
                    pathways.append(pathway)
            config['pathways'] = pathways
        write_json(root / 'config.json', config)
        write_json(root / 'package-provenance.json', report)
        with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
    output.seek(0)
    return output
