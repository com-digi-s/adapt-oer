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
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
from adapt_persistence_bootstrap import bootstrap_runtime
from lxp_catalogue import PackageValidationError, canonical_bytes


QUESTION_TYPES = {'mcq', 'gmcq', 'matching', 'slider', 'textinput', 'infai-dragndrop'}
DATA_FILES = ('contentObjects', 'articles', 'blocks', 'components')
ASSESSMENT_CAPABILITIES_FILE = 'assessment-template-capabilities.json'
APPROVED_ASSESSMENT_CONTENT_SHA256 = 'e1463960aad95dd8dc3afb47e98e322bee1fce2d2460f2babd07c2a59e90831e'
ASSESSMENT_PLUGINS = {'adapt-contrib-mcq': '7.2.1', 'adapt-contrib-matching': '7.1.2',
    'adapt-contrib-slider': '4.1.1', 'adapt-contrib-textInput': '7.1.2',
    'adapt-dragndrop-infai-fork': '0.0.1', 'adapt-contrib-text': '6.1.1',
    'adapt-submitAll': '3.0.2', 'adapt-contrib-spoor': '5.2.1', 'adapt-pageNav': '2.3.2'}
ASSESSMENT_MODULES = {
    'mcq': ('adapt-contrib-mcq/js/McqModel', 'adapt-contrib-mcq/js/McqView'),
    'matching': ('adapt-contrib-matching/js/MatchingModel', 'adapt-contrib-matching/js/MatchingView'),
    'slider': ('adapt-contrib-slider/js/sliderModel', 'adapt-contrib-slider/js/sliderView'),
    'textinput': ('adapt-contrib-textInput/js/textInputModel', 'adapt-contrib-textInput/js/textInputView'),
    'infai-dragndrop': ('adapt-dragndrop-infai-fork/js/adapt-dragndrop', 'adapt-dragndrop-infai-fork/js/dragndropView')}


def assessment_template_capabilities(entries):
    """Describe one template payload; only the validator approves its content.

    Re-hashing altered files cannot approve a new runtime. Such a release also
    needs an explicit review/update of APPROVED_ASSESSMENT_CONTENT_SHA256.
    """
    files = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())
             if name != ASSESSMENT_CAPABILITIES_FILE and not name.endswith('/')}
    return {'format': 'adapt-oer-assessment-template-capabilities', 'version': 1,
        'templateId': 'adapt-oer-assessment-5.22.6-r1', 'frameworkVersion': '5.22.6',
        'sourceQuestionTypes': sorted(QUESTION_TYPES), 'nativeQuestionTypes': sorted(ASSESSMENT_MODULES),
        'componentAdapters': {'gmcq': {'id': 'gmcq-to-mcq-v1', 'runtimeType': 'mcq'}},
        'navigation': {'strategy': 'single-page-submit-all-host-return', 'launch': 'index_lms.html',
            'returnMethod': 'parent.App.renderView', 'returnView': 'views/dashboard/index.html',
            'pageNavDummyRequired': False},
        'plugins': dict(ASSESSMENT_PLUGINS), 'fileSha256': files,
        'contentSha256': hashlib.sha256(canonical_bytes(files)).hexdigest()}


def validate_assessment_template(path):
    """Validate the actual pinned bundle, dependencies and assessment skeleton.

    This is a source-template gate, not an export-time repair or a claim that
    arbitrary plugin versions work. No authored dummy components are needed.
    """
    def require(condition, message):
        if not condition:
            raise PackageValidationError('Assessment template: ' + message)

    from lxp_multi_export import validate_archive_entries
    with ZipFile(path) as archive:
        validate_archive_entries(archive)
        entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
    require(ASSESSMENT_CAPABILITIES_FILE in entries, 'versioned capabilities manifest missing')
    try:
        declared = json.loads(entries[ASSESSMENT_CAPABILITIES_FILE])
    except (ValueError, UnicodeError) as error:
        raise PackageValidationError('Assessment template: invalid capabilities JSON') from error
    expected = assessment_template_capabilities(entries)
    require(declared == expected, 'capabilities/file SHA-256 manifest differs from actual payload')
    critical = ('index_lms.html', 'imsmanifest.xml', 'adapt.css', 'templates.js',
        'adapt/js/adapt.min.js', 'adapt/js/build.min.js', 'adapt/js/scriptLoader.js',
        'libraries/modernizr.js', 'libraries/require.min.js', 'libraries/jquery.min.js',
        'libraries/jquery-ui.min.js', 'libraries/jquery.ui.touch-punch.js', 'libraries/rangeslider.js',
        'libraries/SCORM_API_wrapper.js', 'course/config.json', 'course/en/course.json')
    require(all(name in entries and entries[name] for name in critical), 'required runtime/dependency file missing')
    try:
        runtime, templates = (entries[name].decode('utf-8') for name in ('adapt/js/adapt.min.js', 'templates.js'))
        build = json.loads(entries['adapt/js/build.min.js'])
        config, course = (json.loads(entries[name]) for name in ('course/config.json', 'course/en/course.json'))
        data = {name: json.loads(entries['course/en/' + name + '.json']) for name in DATA_FILES}
        loader, index = (entries[name].decode('utf-8') for name in ('adapt/js/scriptLoader.js', 'index_lms.html'))
        manifest = entries['imsmanifest.xml']
        require(not re.search(br'<!\s*(DOCTYPE|ENTITY)', manifest, re.I), 'unsafe launch manifest XML')
        xml = ET.fromstring(manifest)
    except PackageValidationError:
        raise
    except (ValueError, KeyError, UnicodeError, ET.ParseError) as error:
        raise PackageValidationError('Assessment template: invalid runtime/build/course/manifest structure') from error
    require(all(isinstance(value, dict) for value in (build, config, course)), 'build/config/course must be JSON objects')
    require(isinstance(build.get('package'), dict) and isinstance(build.get('plugins'), list)
            and all(isinstance(plugin, dict) and isinstance(plugin.get('name'), str) for plugin in build['plugins'])
            and isinstance(build.get('includes'), list) and all(isinstance(name, str) for name in build['includes']),
            'invalid plugin/build metadata types')
    require(build.get('package', {}).get('version') == '5.22.6', 'unapproved framework version')
    plugins = {plugin.get('name'): plugin.get('version') for plugin in build.get('plugins', [])}
    require(len(plugins) == len(build['plugins']), 'duplicate pinned plugin names')
    require(all(plugins.get(name) == version and name in build.get('includes', [])
                for name, version in ASSESSMENT_PLUGINS.items()), 'required pinned plugin metadata missing')
    for component, modules in ASSESSMENT_MODULES.items():
        require(all(runtime.count('__AMD("components/' + module + '"') == 1 for module in modules),
                'compiled model/view module missing: ' + component)
        if component == 'infai-dragndrop':
            require('Adapt$1.register("infai-dragndrop", {' in runtime and 'model: QuestionModel$1' in runtime,
                    'native drag-and-drop registration missing')
        else:
            require('components$1.register("' + component + '",' in runtime,
                    'native question registration missing: ' + component)
        if component in {'mcq', 'matching', 'textinput'}:
            require('register("' + component + '", function' in runtime, 'React question renderer missing: ' + component)
        else:
            require('this["Handlebars"]["templates"]["' + component + '"]' in templates,
                    'compiled question renderer missing: ' + component)
    require('components$1.register("gmcq",' not in runtime, 'GMCQ must use the reviewed MCQ adapter')
    require('register("text", function' in runtime and 'this["Handlebars"]["templates"]["submitAll"]' in templates
            and 'const SubmitAll = Backbone.View.extend' in runtime, 'result/SubmitAll implementation missing')
    require('components/adapt-pageNav/js/adapt-pageNav' in runtime
            and 'this["Handlebars"]["templates"]["pageNav"]' in templates, 'bundled pageNav implementation missing')
    require(all(value in runtime for value in ('"libraries/rangeslider"', '"libraries/jquery-ui.min"',
            '"libraries/jquery.ui.touch-punch"', '"libraries/SCORM_API_wrapper"')),
            'native question/Spoor dependency wiring missing')
    require('adapt/js/scriptLoader.js' in index and 'adapt.css' in index and 'adapt/js/adapt.min.js' in loader
            and "'templates'" in loader and 'offline_API_wrapper.js' not in index, 'LMS launch/loader wiring missing')
    resources = [node for node in xml.iter() if node.tag.rsplit('}', 1)[-1] == 'resource']
    refs = {node.get('identifierref') for node in xml.iter() if node.tag.rsplit('}', 1)[-1] == 'item'}
    require(len(resources) == 1 and resources[0].get('href') == 'index_lms.html'
            and refs == {resources[0].get('identifier')}, 'manifest does not select the actual LMS entry')
    require(config.get('_defaultLanguage') == 'en' and isinstance(config.get('_spoor'), dict)
            and config['_spoor'].get('_isEnabled') is True,
            'native English/Spoor source profile missing')
    require(all(isinstance(data[name], list) and len(data[name]) == 1 and isinstance(data[name][0], dict) for name in DATA_FILES),
            'single-page/article/result skeleton required')
    page, article, block, result = (data[name][0] for name in DATA_FILES)
    require(all(isinstance(value.get('_id'), str) and value['_id'] for value in (course, page, article, block, result))
            and len({value['_id'] for value in (course, page, article, block, result)}) == 5,
            'unique explicit skeleton IDs required')
    require(page.get('_type') == 'page' and page.get('_parentId') == course.get('_id')
            and article.get('_parentId') == page.get('_id') and block.get('_parentId') == article.get('_id')
            and result.get('_parentId') == block.get('_id') and result.get('_component') == 'text'
            and '_assessment' not in block, 'invalid result hierarchy or fictitious result bank')
    require(isinstance(article.get('_submitAll'), dict) and isinstance(article.get('_assessment'), dict)
            and isinstance(article['_assessment'].get('_banks'), dict), 'invalid SubmitAll/bank configuration types')
    require(article.get('_submitAll', {}).get('_isEnabled') is True
            and article.get('_assessment', {}).get('_isEnabled') is True
            and article.get('_assessment', {}).get('_banks', {}).get('_isEnabled') is True,
            'single-article SubmitAll/bank configuration missing')
    start = course.get('_start', {})
    require(isinstance(start, dict) and isinstance(start.get('_startIds'), list)
            and all(isinstance(item, dict) for item in start['_startIds']), 'invalid assessment start configuration types')
    require(start.get('_isEnabled') is True and start.get('_force') is True and start.get('_isMenuDisabled') is True
            and [item.get('_id') for item in start.get('_startIds', [])] == [article.get('_id')],
            'single-page assessment start strategy differs')
    instruction = result.get('instruction', '')
    require(isinstance(instruction, str) and 'parentApp.adaptHelpers.getDiagnosticResults()' in instruction
            and "parentApp.renderView(parentApp.SCO_FRAME, 'views/dashboard/index.html')" in instruction,
            'existing diagnostic result/host-return wiring missing')
    try:
        patched, _ = bootstrap_runtime(entries['adapt/js/adapt.min.js'])
    except (ValueError, UnicodeError) as error:
        raise PackageValidationError('Assessment template: unsupported persistence bootstrap') from error
    require(patched == entries['adapt/js/adapt.min.js'], 'checked early persistence bootstrap missing')
    require(expected['contentSha256'] == APPROVED_ASSESSMENT_CONTENT_SHA256,
            'unapproved content SHA-256; a re-hashed manifest cannot approve a new bundle')
    return declared


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def safe_extract(archive, destination, *, exclude=()):
    target = Path(destination).resolve()
    from lxp_multi_export import validate_archive_entries
    validate_archive_entries(archive)
    for entry in archive.infolist():
        resolved = (target / entry.filename).resolve()
        if not resolved.is_relative_to(target) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise PackageValidationError('Unsafe ZIP entry: ' + entry.filename)
    archive.extractall(target, members=[entry for entry in archive.infolist() if entry.filename not in exclude])


def published_course(build):
    build = Path(build)
    config = read_json(build / 'course/config.json')
    language = config.get('_defaultLanguage', 'en')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', language):
        raise PackageValidationError('Invalid published course language')
    data_dir = build / 'course' / language
    course = read_json(data_dir / 'course.json')
    data = {name: read_json(data_dir / (name + '.json')) for name in DATA_FILES}
    return {'build': build, 'language': language, 'course': course, 'data': data, 'config': config}


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
    capabilities = validate_assessment_template(template_path)
    with ZipFile(template_path) as archive:
        # The manifest qualifies the input template. Keep generated SCO bytes
        # and native resume fingerprints stable by recording it in provenance.
        safe_extract(archive, destination, exclude=(ASSESSMENT_CAPABILITIES_FILE,))
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
        question.setdefault('_questionWeight', source.get('config', {}).get('_questionWeight', 1))
        if question['_component'] == 'textinput':
            # Older exports used this alias, while the actual bundled native
            # model requires item._answers. Never alter the learning source.
            for item in question.get('_items', []):
                if '_answers' not in item and 'answers' in item:
                    item['_answers'] = item.pop('answers')
        if question['_component'] == 'infai-dragndrop':
            for item in question.get('_items', []):
                if isinstance(item.get('accepted'), str):
                    item['accepted'] = [item['accepted']]
        if question['_component'] == 'slider' and not isinstance(question.get('_correctAnswer'), bool) and question.get('_correctAnswer') == 0:
            # Native SliderModel treats numeric zero as a falsy single answer;
            # an equivalent explicit singleton range preserves the real key.
            question['_correctRange'] = {'_bottom': 0, '_top': 0}
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
    metadata['templateCapabilities'] = capabilities
    return metadata


def _package_legacy(builds, wrapper_path, template_path, expected_wrapper_sha256=None):
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


def package_courses(builds, wrapper_path, template_path, expected_wrapper_sha256=None, *, mode='legacy', **options):
    """Export the historical numeric package or an explicitly selected new mode.

    The legacy branch deliberately retains its old contract. Unknown versions
    and foreign prefixed titles never cause an automatic fallback to it.
    """
    if mode == 'legacy':
        if options:
            raise PackageValidationError('Multi-framework options require mode="multi-framework"')
        return _package_legacy(builds, wrapper_path, template_path, expected_wrapper_sha256)
    if mode != 'multi-framework':
        raise PackageValidationError('Unsupported LXP export mode: ' + str(mode))
    prepared = prepare_multi_framework_export(builds, wrapper_path, template_path,
        expected_wrapper_sha256=expected_wrapper_sha256, **options)
    try:
        return build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256)
    finally:
        prepared.close()


from lxp_multi_export import prepare_multi_framework_export, build_multi_framework_export
