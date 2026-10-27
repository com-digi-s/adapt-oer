"""Verify delivered export-time test selections against actual source pools.

This is a consistency gate, not a package signature or psychometric validation.
Archives predating the selection feature retain their existing validation path.
"""
import json
from pathlib import Path

from lxp_assessment_selection import question_fingerprint, select_questions
from lxp_assessment_overrides import normalize_overrides
from lxp_catalogue import PackageValidationError, load_catalogue


def _read(path):
    try:
        with path.open(encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise PackageValidationError('Assessment selection metadata cannot be read') from error


def _same(left, right):
    try:
        def canonical(value):
            return json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False)
        return canonical(left) == canonical(right)
    except (TypeError, ValueError) as error:
        raise PackageValidationError('Assessment selection metadata is not finite JSON data') from error


def _pool(contract, original_sources, sources, framework, phase):
    # Runtime imports avoid a module-level export/validator dependency cycle.
    from lxp_multi_export import PHASE_TAG, _question_domain, _question_preview_title, _validate_question
    modules = {module['id']: module for module in contract['modules']}
    result = []
    for item in sources:
        sco = item['sco']
        if sco['frameworkId'] != framework['id']:
            continue
        source = original_sources[sco['id']]
        info = sco['source']
        module = modules[sco['moduleId']]
        for question in source['data']['components']:
            classes = set((question.get('_classes') or '').split())
            if PHASE_TAG[phase] not in classes or 'self-assessment' in classes:
                continue
            _validate_question(question, source['config'].get('_questionWeight', 1))
            result.append({'identity': (info['originId'], info['courseId'], question['_id']),
                'frameworkId': framework['id'], 'moduleId': module['id'], 'moduleNumber': module['number'],
                'moduleLabel': module['label'], 'courseId': sco['id'], 'courseNumber': sco['number'],
                'courseLabel': sco['label'], 'domainId': _question_domain(question, source, framework, info),
                'title': _question_preview_title(question), 'fingerprint': question_fingerprint(question),
                'effectiveQuestionWeight': question.get('_questionWeight', source['config'].get('_questionWeight', 1)),
                'payload': question})
    return result


def _marking(root, sco, selected, options, contract_questions):
    from lxp_export import published_course
    generated = published_course(root / sco['launch']['root'])
    assessments = [article['_assessment'] for article in generated['data']['articles']
                   if isinstance(article.get('_assessment'), dict)
                   and article['_assessment'].get('_isEnabled') is True]
    if len(assessments) != 1:
        raise PackageValidationError('Assessment marking policy requires exactly one native assessment article')
    policy = assessments[0]
    expected = {'_canShowMarking': options['showMarking'], '_canShowModelAnswer': options['showModelAnswer']}
    actual = policy.get('_questions')
    if (not isinstance(actual, dict) or type(policy.get('_suppressMarking')) is not bool
            or policy['_suppressMarking'] is not (not options['showMarking'])
            or any(type(actual.get(key)) is not bool or actual[key] is not value
                   for key, value in expected.items())):
        raise PackageValidationError('Native assessment marking policy differs from the confirmed selection')
    generated_questions = {question['_id']: question for question in generated['data']['components']}
    legacy_policy = options == {'mode': 'all', 'questionCount': None, 'equalSampling': False,
                               'showMarking': True, 'showModelAnswer': True}
    by_source = {(question['sourceCourseId'], question['sourceComponentId']): question
                 for question in contract_questions.values()}
    for candidate in selected:
        record = by_source.get((candidate['courseId'], candidate['identity'][-1]))
        component = generated_questions.get(record['runtimeQuestionId']) if record else None
        if component is None:
            raise PackageValidationError('Selected native question is absent from its generated assessment')
        for key, value in expected.items():
            # The native article supplies inherited flags. Old full-pool callers
            # also retained authored component flags; those remain unchanged and
            # the native assessment policy still has the verified defaults.
            present = component.get(key, actual[key])
            retained_legacy_flag = legacy_policy and key in candidate['payload'] and present == candidate['payload'][key]
            if type(present) is not bool or present is not value and not retained_legacy_flag:
                raise PackageValidationError('Native question marking flag differs from the confirmed selection')


def validate_assessment_selection(contract, root, original_sources, provenance):
    """Cross-check sidecar, provenance, selected banks, flags and source pool.

    The enclosing product validator has already reconstructed source JSON and
    validated SCO/bank identities. Missing both metadata fields means an older
    archive; missing only one is an invalid partial feature export.
    """
    root = Path(root)
    path = root / 'assessment-selection.json'
    has_sidecar = path.exists() or path.is_symlink()
    has_provenance = 'assessmentSelection' in provenance
    if not has_sidecar and not has_provenance:
        return
    if not has_sidecar or not has_provenance or path.is_symlink() or not path.is_file():
        raise PackageValidationError('Assessment selection sidecar/provenance is missing or unsafe')
    summaries = _read(path)
    if not isinstance(summaries, list) or not _same(summaries, provenance['assessmentSelection']):
        raise PackageValidationError('Assessment selection sidecar differs from its provenance')
    from lxp_multi_export import _complete_selection_coverage, _reference_question_identities
    catalogue = load_catalogue(root / 'lxp-catalogue.json')
    framework_configs = {framework['id']: framework for framework in catalogue['frameworks']}
    frameworks = {framework['id']: framework for framework in contract['frameworks']}
    scos = {sco['id']: sco for sco in contract['scos']}
    questions = {question['id']: question for question in contract['questions']}
    assessments = {(assessment['frameworkId'], assessment['phase']): assessment
                   for assessment in contract['assessments']}
    expected_phases = {(framework['id'], phase) for framework in frameworks.values()
                       for phase in ('entry', 'final')
                       if framework['diagnostics'][phase]['intent'] != 'omit'}
    sources = [{'sco': scos[report['scoId']]} for report in provenance['courses']
               if report.get('scoId') in scos and scos[report['scoId']]['role'] == 'learning']
    seen = set()
    for summary in summaries:
        if not isinstance(summary, dict):
            raise PackageValidationError('Malformed assessment selection summary')
        framework_id, phase = summary.get('frameworkId'), summary.get('phase')
        if not isinstance(framework_id, str) or not isinstance(phase, str):
            raise PackageValidationError('Assessment selection lacks an explicit framework/phase')
        key = (framework_id, phase)
        if key not in expected_phases or key in seen:
            raise PackageValidationError('Unknown, omitted or duplicate assessment selection phase')
        seen.add(key)
        framework = framework_configs.get(framework_id)
        if (not framework or any(framework.get(field) != frameworks[framework_id].get(field)
                                 for field in ('prefix', 'version', 'label'))):
            raise PackageValidationError('Assessment selection framework differs from the packaged catalogue')
        options = summary.get('options')
        if not isinstance(options, dict):
            raise PackageValidationError('Assessment selection options are missing')
        has_inventory = 'availableQuestions' in summary
        if normalize_overrides(options) and not has_inventory:
            raise PackageValidationError('Manual assessment selection requires the complete available question inventory')
        pool = _pool(contract, original_sources, sources, framework, phase)
        reference = (_reference_question_identities(pool, framework, phase)
                     if options.get('mode') == 'reference' else None)
        selected, expected = select_questions(pool, options, framework, phase, reference_identities=reference,
                                             include_available_questions=has_inventory)
        _complete_selection_coverage(expected, sources, framework)
        if not _same(summary, expected):
            raise PackageValidationError('Assessment selection summary differs from the actual source pool or policy')
        assessment = assessments.get(key)
        selected_pairs = {(candidate['courseId'], candidate['identity'][-1]) for candidate in selected}
        if assessment is None:
            if selected:
                raise PackageValidationError('Selected questions have no generated assessment')
            continue
        bank_ids = [question_id for bank in assessment['banks'] for question_id in bank['questionIds']]
        bank_pairs = {(questions[question_id]['sourceCourseId'], questions[question_id]['sourceComponentId'])
                      for question_id in bank_ids}
        if (len(bank_ids) != len(selected) or len(set(bank_ids)) != len(bank_ids)
                or bank_pairs != selected_pairs):
            raise PackageValidationError('Confirmed selected questions differ from the actual assessment banks')
        _marking(root, scos[assessment['scoId']], selected, expected['options'], questions)
    if seen != expected_phases:
        raise PackageValidationError('Assessment selection metadata does not cover all requested phases')
