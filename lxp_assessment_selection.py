"""Pure, reproducible export-time selection from an explicitly scoped test pool.

The balanced algorithm is a new documented v1 policy, not an undocumented copy
of Adaptmin. It balances available module/course capacities in round-robin
order, then favours the least represented configured domain. No runtime random
sampling, source mutation, grading or learner-state migration happens here.
"""
from collections import Counter, deque
from copy import deepcopy
import hashlib
import json
import re

from lxp_catalogue import PackageValidationError


PHASES = {'entry', 'final'}
OPTION_KEYS = {'mode', 'questionCount', 'equalSampling', 'showMarking', 'showModelAnswer'}
DEFAULT_OPTIONS = {'mode': 'balanced', 'questionCount': None, 'equalSampling': True,
                   'showMarking': True, 'showModelAnswer': False}
QUESTION_FINGERPRINT_FIELDS = ('_component', 'title', 'displayTitle', 'body', 'instruction',
    '_items', '_answers', '_correctAnswer', '_correctRange', '_selectable', '_questionWeight',
    '_scaleStart', '_scaleEnd', '_scaleStep', '_step')
SHA = re.compile(r'^[0-9a-f]{64}$')
NUMBER = re.compile(r'^[1-9][0-9]*$')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def question_fingerprint(question):
    """Bind content, native options/keys, selection count and question weight.

    Missing fields remain missing; null and absent therefore cannot alias.
    Runtime/generated IDs and unrelated display flags are deliberately absent.
    """
    if not isinstance(question, dict):
        raise PackageValidationError('Question fingerprint requires a source question object')
    try:
        return hashlib.sha256(_canonical({key: question[key] for key in QUESTION_FINGERPRINT_FIELDS
                                         if key in question})).hexdigest()
    except (TypeError, ValueError) as error:
        raise PackageValidationError('Question fingerprint contains non-JSON or non-finite data') from error


def _options(value):
    if not isinstance(value, dict) or set(value) - OPTION_KEYS:
        raise PackageValidationError('Unknown or malformed assessment selection options')
    result = {**DEFAULT_OPTIONS, **deepcopy(value)}
    if not isinstance(result['mode'], str) or result['mode'] not in {'balanced', 'all', 'reference'}:
        raise PackageValidationError('Unknown assessment selection mode')
    count = result['questionCount']
    if count is not None and (type(count) is not int or count < 1):
        raise PackageValidationError('Assessment questionCount must be a positive integer or null')
    for key in ('equalSampling', 'showMarking', 'showModelAnswer'):
        if type(result[key]) is not bool:
            raise PackageValidationError('Assessment ' + key + ' must be an explicit boolean')
    return result


def normalize_assessment_options(options, catalogue):
    """Normalize supplied ID/prefix aliases to explicit catalogue framework IDs.

    Missing API options return {}, preserving old export callers' full-pool
    behaviour. GUI defaults must be submitted explicitly, not silently inferred
    by this helper. Conflicting prefix/ID aliases and unknown keys fail closed.
    """
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise PackageValidationError('Assessment options must be keyed by framework')
    frameworks = catalogue.get('frameworks', []) if isinstance(catalogue, dict) else []
    result = {}
    for alias, phases in options.items():
        choices = [framework for framework in frameworks
                   if alias in (framework.get('id'), framework.get('prefix'))]
        if not choices:
            raise PackageValidationError('Assessment options contain an unknown framework: ' + str(alias))
        if not isinstance(phases, dict) or set(phases) - PHASES:
            raise PackageValidationError('Assessment options contain an unknown phase')
        normalized = {phase: _options(value) for phase, value in phases.items()}
        for framework in choices:
            destination = result.setdefault(framework['id'], {})
            for phase, value in normalized.items():
                if phase in destination and destination[phase] != value:
                    raise PackageValidationError('Conflicting framework ID/prefix assessment options')
                destination[phase] = deepcopy(value)
    return result


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not re.search(r'[\x00-\x1f\x7f]', value)


def _identity(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3 or not all(_text(part) for part in value):
        raise PackageValidationError('Selection candidate requires an explicit origin/course/component identity')
    return tuple(value)


def _validated(candidates, framework):
    if not isinstance(candidates, (tuple, list)) or not isinstance(framework, dict):
        raise PackageValidationError('Selection requires a scoped candidate list and framework')
    modules = {module['id']: module for module in framework.get('modules', [])}
    domains = {domain['id']: domain for domain in framework.get('domains', [])}
    seen, course_addresses, addresses = set(), {}, {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise PackageValidationError('Malformed selection candidate')
        identity = _identity(candidate.get('identity'))
        if identity in seen:
            raise PackageValidationError('Duplicate source question in selection pool')
        seen.add(identity)
        if not _text(candidate.get('moduleId')) or not _text(candidate.get('domainId')):
            raise PackageValidationError('Selection candidate requires explicit module/domain IDs')
        module = modules.get(candidate.get('moduleId'))
        if not module or candidate.get('moduleNumber') != module.get('number'):
            raise PackageValidationError('Selection candidate module is not explicitly mapped to its framework')
        number = candidate.get('courseNumber')
        if (not isinstance(number, str) or not NUMBER.fullmatch(number)
                or number not in module.get('courseNumbers', [])):
            raise PackageValidationError('Selection candidate course address is not in its framework module')
        course_id = candidate.get('courseId')
        if not _text(course_id) or candidate.get('domainId') not in domains:
            raise PackageValidationError('Selection candidate requires a known course and domain')
        if 'frameworkId' in candidate and candidate['frameworkId'] != framework.get('id'):
            raise PackageValidationError('Selection pool contains a question from another framework')
        address = (candidate['moduleId'], number)
        if course_addresses.setdefault(course_id, address) != address or addresses.setdefault(address, course_id) != course_id:
            raise PackageValidationError('Conflicting course identities/addresses in selection pool')
        if not isinstance(candidate.get('fingerprint'), str) or not SHA.fullmatch(candidate['fingerprint']):
            raise PackageValidationError('Selection candidate requires a question content fingerprint')
    return modules, domains


def _rank(candidate, phase):
    # Stable hash order avoids privileging the first source file/UUID while
    # making repeated previews and reordered candidate input reproducible.
    value = {'phase': phase, 'identity': list(_identity(candidate['identity'])),
             'fingerprint': candidate['fingerprint']}
    return hashlib.sha256(_canonical(value)).hexdigest(), tuple(candidate['identity'])


def _balanced(candidates, limit, framework, phase):
    modules = {}
    for candidate in sorted(candidates, key=lambda question: _rank(question, phase)):
        modules.setdefault(candidate['moduleId'], {}).setdefault(candidate['courseId'], {}).setdefault(
            candidate['domainId'], deque()).append(candidate)
    course_numbers = {candidate['courseId']: int(candidate['courseNumber']) for candidate in candidates}
    module_numbers = {module['id']: int(module['number']) for module in framework['modules']}
    module_queue = deque(sorted(modules, key=lambda module_id: module_numbers[module_id]))
    course_queues = {module_id: deque(sorted(courses, key=lambda course_id: course_numbers[course_id]))
                     for module_id, courses in modules.items()}
    domain_order = {domain['id']: index for index, domain in enumerate(framework['domains'])}
    domain_counts, local_domain_counts = Counter(), Counter()
    result = []
    while len(result) < limit and module_queue:
        module_id = module_queue.popleft()
        courses = course_queues[module_id]
        course_id = courses.popleft()
        domains = modules[module_id][course_id]
        domain_id = min(domains, key=lambda value: (
            domain_counts[value], local_domain_counts[(course_id, value)], domain_order[value]))
        result.append(domains[domain_id].popleft())
        domain_counts[domain_id] += 1
        local_domain_counts[(course_id, domain_id)] += 1
        if not domains[domain_id]:
            del domains[domain_id]
        if domains:
            courses.append(course_id)
        if courses:
            module_queue.append(module_id)
    return result


def _coverage(candidates, selected, framework):
    def row(kind, value, label, **extra):
        available = sum(candidate[kind] == value for candidate in candidates)
        count = sum(candidate[kind] == value for candidate in selected)
        return {'id': value, 'label': label, **extra, 'poolCount': available,
                'selectedCount': count, 'status': 'selected' if count else 'not-assessed'}
    modules = [row('moduleId', module['id'], module['label'], number=module['number'])
               for module in sorted(framework.get('modules', []), key=lambda value: int(value['number']))]
    courses = {}
    for candidate in candidates:
        courses.setdefault(candidate['courseId'], candidate)
    course_rows = [row('courseId', candidate['courseId'], candidate.get('courseLabel', candidate['courseId']),
                      moduleId=candidate['moduleId'], number=candidate['courseNumber'])
                   for candidate in sorted(courses.values(), key=lambda value: (
                       int(value['moduleNumber']), int(value['courseNumber'])))]
    domains = [row('domainId', domain['id'], domain['label']) for domain in framework.get('domains', [])]
    return {'modules': modules, 'courses': course_rows, 'domains': domains}


def select_questions(candidates, options, framework, phase, *, reference_identities=None):
    """Return original candidate objects and an exportable selection summary.

    Reference mode accepts only a complete trusted identity list; the caller
    additionally verifies its content fingerprints against the reviewed preset.
    No partial reference match or unannounced top-up from the pool is allowed.
    """
    if phase not in PHASES:
        raise PackageValidationError('Unknown assessment selection phase')
    _validated(candidates, framework)
    settings = _options({} if options is None else options)
    mode, requested = settings['mode'], settings['questionCount']
    warnings = []
    if mode == 'reference':
        if not isinstance(reference_identities, (tuple, list)) or not reference_identities:
            raise PackageValidationError('A trusted complete public reference selection is required')
        identities = [_identity(value) for value in reference_identities]
        if len(set(identities)) != len(identities):
            raise PackageValidationError('Duplicate question in public reference selection')
        pool = {tuple(candidate['identity']): candidate for candidate in candidates}
        if set(identities) - set(pool):
            raise PackageValidationError('Public reference selection is incomplete in the selected source pool')
        if requested is not None and requested != len(identities):
            raise PackageValidationError('Public reference selection has a fixed question count')
        requested = len(identities)
        settings['questionCount'] = requested
        selected = [pool[identity] for identity in identities]
        algorithm = 'adapt-oer-public-reference@1'
    else:
        limit = len(candidates) if mode == 'all' or requested is None else min(requested, len(candidates))
        if mode == 'all':
            requested = None
            settings['questionCount'] = None
        elif requested is not None and requested > len(candidates):
            warnings.append('Es sind nur ' + str(len(candidates)) + ' geeignete Fragen verfügbar; '
                            'die gewünschte Anzahl von ' + str(requested) + ' wird nicht erreicht.')
        if mode != 'all' and requested is not None and settings['equalSampling']:
            selected = _balanced(candidates, limit, framework, phase)
            algorithm = 'adapt-oer-balanced-selection@1'
        else:
            selected = sorted(candidates, key=lambda candidate: _rank(candidate, phase))[:limit]
            algorithm = 'adapt-oer-all-selection@1' if limit == len(candidates) else 'adapt-oer-fixed-selection@1'
    coverage = _coverage(candidates, selected, framework)
    if any(row['poolCount'] and not row['selectedCount'] for row in coverage['modules'] + coverage['domains']):
        warnings.append('Die Auswahl deckt nicht alle verfügbaren Module oder Domänen ab; '
                        'nicht enthaltene Bereiche sind nicht geprüft.')
    questions = [{'identity': list(candidate['identity']), 'sourceCourseId': candidate['courseId'],
                  'sourceComponentId': candidate['identity'][-1], 'title': candidate.get('title', ''),
                  'domainId': candidate['domainId'], 'fingerprint': candidate['fingerprint']}
                 for candidate in selected]
    return selected, {'frameworkId': framework['id'], 'prefix': framework['prefix'], 'phase': phase,
                      'poolCount': len(candidates), 'selectedCount': len(selected),
                      'requestedCount': requested, 'options': settings, 'algorithm': algorithm,
                      'warnings': warnings, 'coverage': coverage, 'questions': questions}
