"""Project explicit navigation onto a selection without hiding broken links.

An unselected course is not an unknown course. Callers may supply explicitly
authorized published builds to prove that distinction. Only referenced JSON
models are read; excluded assets and question text are never packaged.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from lxp_catalogue import PackageValidationError
from lxp_catalogue_courses import normalize_course_title


def source_key(metadata):
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(key), str)
            or not metadata[key].strip() for key in ('originId', 'courseId')):
        raise PackageValidationError('Navigation source requires explicit origin/course IDs')
    return json.dumps([metadata['originId'], metadata['courseId']], ensure_ascii=False, separators=(',', ':'))


def _hashes(build, language=None):
    config_path = build / 'course/config.json'
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    current_language = config.get('_defaultLanguage', 'en')
    import re
    if not isinstance(current_language, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', current_language):
        raise PackageValidationError('Invalid navigation source language')
    if language is not None and language != current_language:
        raise PackageValidationError('Navigation source changed since preview')
    result = {'course/config.json': hashlib.sha256(config_bytes).hexdigest()}
    for name in ('course', 'contentObjects', 'articles', 'blocks', 'components'):
        relative = 'course/' + current_language + '/' + name + '.json'
        result[relative] = hashlib.sha256((build / relative).read_bytes()).hexdigest()
    return current_language, result


def verify_navigation_inputs(inputs):
    for item in inputs:
        try:
            _, current = _hashes(item['original'], item['language'])
        except (OSError, ValueError) as error:
            raise PackageValidationError('Navigation source changed since preview; create a new preview') from error
        if current != item['hashes']:
            raise PackageValidationError('Navigation source changed since preview; create a new preview')


def project_navigation(catalogue, selected_sources, available_sources, registry, *, framework_versions=None):
    """Validate every explicit target, then retain only selected destinations."""
    from lxp_export import published_course

    frameworks = {item['id'] for item in catalogue['frameworks']}
    selected = {source_key(item['metadata']): item for item in selected_sources}
    available = {}
    if available_sources is not None and not isinstance(available_sources, (list, tuple)):
        raise PackageValidationError('navigation_sources must be an explicit list of published builds and metadata')
    for item in available_sources or []:
        if not isinstance(item, dict) or not isinstance(item.get('build'), (str, Path)):
            raise PackageValidationError('Invalid explicit navigation source')
        key = source_key(item.get('metadata'))
        if key in available:
            raise PackageValidationError('Duplicate explicit navigation source')
        available[key] = item
        if key in selected and Path(item['build']).resolve() != selected[key]['original']:
            raise PackageValidationError('Ambiguous selected/navigation source build')
    by_id = {}
    for key, entry in registry.get('sources', {}).items():
        sco_id = entry.get('scoId')
        if sco_id:
            if sco_id in by_id and by_id[sco_id] != key:
                raise PackageValidationError('Ambiguous registered navigation SCO identity')
            by_id[sco_id] = key
    for key, item in selected.items():
        by_id[item['sco']['id']] = key
    resolved, inputs, evidence = {}, [], []

    def resolve(reference, framework_id):
        if isinstance(reference, str):
            key = by_id.get(reference)
        elif isinstance(reference, dict):
            key = source_key(reference)
        else:
            key = None
        if key in selected:
            item = selected[key]
            result = {'selected': True, 'scoId': item['sco']['id'],
                'frameworkId': item['sco']['frameworkId'], 'source': item['source']}
        elif key in resolved:
            result = resolved[key]
        elif key in available:
            item = available[key]
            metadata = item['metadata']
            previous = registry.get('sources', {}).get(key, {})
            kind = metadata.get('kind', 'aat-published')
            if kind not in ('aat-published', 'native-registered') or (kind == 'native-registered'
                    and previous.get('nativeRegistered') is not True):
                raise PackageValidationError('Navigation source is not explicitly authorized/registered')
            build = Path(item['build']).resolve()
            language, hashes = _hashes(build)
            source = published_course(build)
            if _hashes(build)[1] != hashes:
                raise PackageValidationError('Navigation source changed while reading; create a new preview')
            if kind == 'aat-published' and source['config'].get('_courseId', metadata['courseId']) != metadata['courseId']:
                raise PackageValidationError('Navigation publication differs from authenticated AAT source ID')
            title = source['course'].get('title', '')
            bound = previous.get('binding', {})
            mapping = normalize_course_title(title, catalogue, framework_versions=framework_versions,
                legacy_import=previous.get('legacyApproved') is True, binding=bound)
            if bound and any(bound.get(key) != mapping[key] for key in
                    ('frameworkId', 'prefix', 'version', 'moduleNumber', 'courseNumber')):
                raise PackageValidationError('Referenced navigation source has an unconfirmed assignment change')
            result = {'selected': False, 'frameworkId': mapping['frameworkId'], 'source': source,
                'scoId': previous.get('scoId')}
            resolved[key] = result
            inputs.append({'original': build, 'metadata': deepcopy(metadata), 'language': language, 'hashes': hashes})
            evidence.append({'originId': metadata['originId'], 'courseId': metadata['courseId'],
                'frameworkId': mapping['frameworkId'], 'scoId': previous.get('scoId'),
                'publishedTitle': title, 'language': language, 'jsonSha256': hashes})
        else:
            raise PackageValidationError('Explicit navigation references an unknown or unavailable source')
        if result['frameworkId'] != framework_id:
            raise PackageValidationError('Explicit navigation references a foreign-framework course')
        return result

    projection = {'pathways': {'original': 0, 'retained': 0, 'shortened': 0, 'omitted': 0},
                  'glossary': {'original': 0, 'retained': 0, 'shortened': 0, 'omitted': 0}, 'details': []}
    pathways, glossary = [], []

    def account(kind, item, label, original, retained):
        counts = projection['pathways' if kind == 'pathway' else 'glossary']
        counts['original'] += 1
        if retained:
            counts['retained'] += 1
        if retained != original:
            status = 'shortened' if retained else 'omitted'
            counts[status] += 1
            projection['details'].append({'kind': kind, 'id': item['id'], 'label': label,
                'status': status, 'originalCount': original, 'retainedCount': retained})

    for kind, collection in (('pathway', catalogue.get('pathways', [])), ('glossary', catalogue.get('glossary', []))):
        if not isinstance(collection, list):
            raise PackageValidationError('Invalid explicit catalogue navigation collection')
        seen_ids = set()
        for item in collection:
            label_key = 'label' if kind == 'pathway' else 'term'
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in seen_ids:
                raise PackageValidationError('Invalid/duplicate explicit catalogue navigation identity')
            seen_ids.add(item['id'])
            if item.get('frameworkId') not in frameworks or not isinstance(item.get(label_key), str) or not item[label_key].strip():
                raise PackageValidationError('Invalid explicit catalogue navigation framework/label')
            refs = item.get('courseRefs', item.get('courseIds')) if kind == 'pathway' else item.get('targets')
            if not isinstance(refs, list) or not refs:
                raise PackageValidationError('Invalid explicit catalogue navigation targets')
            retained = []
            for reference in refs:
                if kind == 'glossary':
                    if not isinstance(reference, dict):
                        raise PackageValidationError('Invalid explicit glossary target')
                    source = resolve(reference.get('scoId', reference), item['frameworkId'])
                    content_ids = {node.get('_id') for values in source['source']['data'].values() for node in values}
                    if not isinstance(reference.get('contentId'), str) or reference['contentId'] not in content_ids:
                        raise PackageValidationError('Explicit glossary target content ID does not exist')
                    if source['selected']:
                        retained.append({'scoId': source['scoId'], 'contentId': reference['contentId']})
                else:
                    source = resolve(reference, item['frameworkId'])
                    if source['selected']:
                        retained.append(source['scoId'])
            account(kind, item, item[label_key], len(refs), len(retained))
            if not retained:
                continue
            result = {key: deepcopy(item[key]) for key in ('id', 'frameworkId', label_key)}
            if kind == 'pathway':
                result['courseIds'] = retained
                pathways.append(result)
            else:
                if 'definition' in item:
                    result['definition'] = deepcopy(item['definition'])
                result['targets'] = retained
                glossary.append(result)
    return {'pathways': pathways, 'glossary': glossary, 'projection': projection, 'inputs': inputs,
            'evidence': sorted(evidence, key=lambda row: (row['originId'], row['courseId']))}
