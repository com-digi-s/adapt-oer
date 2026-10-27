"""One production catalogue and title normalizer for the exporter and its GUI.

Local numbers are addresses, never identities or floating point numbers.  The
catalogue is deliberately independent of the historical ``config.json`` SCOS.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


class PackageValidationError(ValueError):
    """An export cannot truthfully or safely be produced."""


DEFAULT_CATALOGUE = Path(__file__).parent / 'static/lxp-catalogue.json'
NUMBER = re.compile(r'^[1-9][0-9]*$')
PREFIX = re.compile(r'^[A-Za-z]+$')
IDENTIFIER = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')
TITLE = re.compile(r'^([A-Za-z]+)([1-9][0-9]*)\.([1-9][0-9]*)\s*[–-]\s*(\S(?:.*\S)?)$')
LEGACY_TITLE = re.compile(r'^([1-9][0-9]*)\.([1-9][0-9]*)(?:\s*[–-]\s*|\s+)(\S(?:.*\S)?)$')


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def catalogue_sha256(catalogue):
    return hashlib.sha256(canonical_bytes(catalogue)).hexdigest()


def load_catalogue(value=None):
    if value is None:
        value = DEFAULT_CATALOGUE
    if isinstance(value, (str, Path)):
        with Path(value).open(encoding='utf-8') as stream:
            value = json.load(stream)
    result = deepcopy(value)
    if not isinstance(result, dict) or result.get('format') != 'adapt-lxp-catalogue' or type(result.get('version')) is not int or result.get('version') != 1 or result.get('exampleOnly') is not False:
        raise PackageValidationError('Production catalogue adapt-lxp-catalogue@1 required; proposal/fixture catalogues cannot be exported')
    if not isinstance(result.get('revision'), str) or not result['revision'].strip() or not isinstance(result.get('frameworks'), list):
        raise PackageValidationError('Catalogue revision and frameworks are required')
    identities, bindings, module_ids, prefixes = set(), set(), set(), {}
    for framework in result['frameworks']:
        if not isinstance(framework, dict):
            raise PackageValidationError('Catalogue framework must be an object')
        for key in ('id', 'prefix', 'version', 'label'):
            if not isinstance(framework.get(key), str) or not framework[key].strip():
                raise PackageValidationError('Catalogue framework requires ' + key)
        if not IDENTIFIER.fullmatch(framework['id']):
            raise PackageValidationError('Catalogue framework requires a safe stable ID')
        prefix = framework['prefix']
        if not PREFIX.fullmatch(prefix):
            raise PackageValidationError('Framework prefix must contain ASCII letters only')
        if prefixes.setdefault(prefix.lower(), prefix) != prefix:
            raise PackageValidationError('Framework prefixes may not differ only in letter case')
        identity, binding = framework['id'], (framework['prefix'], framework['version'])
        if identity in identities or binding in bindings:
            raise PackageValidationError('Duplicate framework identity/version in catalogue')
        identities.add(identity)
        bindings.add(binding)
        modules, domains = framework.get('modules', []), framework.get('domains', [])
        if not isinstance(modules, list) or not isinstance(domains, list):
            raise PackageValidationError('Catalogue framework modules and domains must be lists')
        numbers = set()
        for module in modules:
            if not isinstance(module, dict):
                raise PackageValidationError('Catalogue module must be an object')
            number = module.get('number')
            if not isinstance(number, str) or not NUMBER.fullmatch(number) or number in numbers:
                raise PackageValidationError('Invalid/duplicate catalogue module number')
            numbers.add(number)
            if (not isinstance(module.get('id'), str) or not IDENTIFIER.fullmatch(module['id'])
                    or not isinstance(module.get('label'), str) or not module['label'].strip()):
                raise PackageValidationError('Catalogue module requires stable id and label')
            if module['id'] in module_ids:
                raise PackageValidationError('Catalogue module IDs must be globally unique')
            module_ids.add(module['id'])
            course_numbers = module.get('courseNumbers')
            if (not isinstance(course_numbers, list)
                    or any(not isinstance(n, str) or not NUMBER.fullmatch(n) for n in course_numbers)
                    or len(set(course_numbers)) != len(course_numbers)):
                raise PackageValidationError('Invalid catalogue course slots')
        domain_ids = set()
        for domain in domains:
            if (not isinstance(domain, dict) or not isinstance(domain.get('id'), str)
                    or not IDENTIFIER.fullmatch(domain['id']) or not isinstance(domain.get('label'), str)
                    or not domain['label'].strip() or domain['id'] in domain_ids):
                raise PackageValidationError('Invalid catalogue domain configuration')
            domain_ids.add(domain['id'])
        if framework.get('defaultDomainId') is not None and (
                not isinstance(framework['defaultDomainId'], str) or framework['defaultDomainId'] not in domain_ids):
            raise PackageValidationError('Unknown explicit default domain')
    return result


def normalize_title(title, catalogue=None, *, framework_versions=None, legacy_import=False, bound_version=None):
    """Resolve an author's canonical title without assigning a technical ID.

    ``bound_version`` comes from the persistent source registry.  A changed
    default catalogue cannot silently move an already registered source.
    """
    catalogue = load_catalogue(catalogue)
    title = (title or '').strip()
    match = TITLE.fullmatch(title)
    if match:
        prefix, module_number, course_number, label = match.groups()
    else:
        old = LEGACY_TITLE.fullmatch(title) if legacy_import else None
        if not old:
            raise PackageValidationError('Ungültiger Kurstitel. Erwartet: DigComp5.1 – Titel oder AILit1.1 – Titel; keine führenden Nullen oder zusätzlichen Zahlensegmente.')
        prefix = 'DigComp'
        module_number, course_number, label = old.groups()
    choices = [f for f in catalogue['frameworks'] if f['prefix'] == prefix]
    if not choices:
        raise PackageValidationError('Unbekanntes oder nicht freigegebenes Rahmenpräfix: ' + prefix)
    requested = (framework_versions or {}).get(prefix)
    if bound_version and requested and requested != bound_version:
        raise PackageValidationError('Existing source is bound to framework version ' + bound_version + '; explicit version migration is required')
    version = bound_version or requested
    if version:
        choices = [f for f in choices if f['version'] == version]
    if len(choices) != 1:
        raise PackageValidationError('Rahmenfassung für ' + prefix + ' bitte eindeutig auswählen; vorhandene Bindungen werden nicht umgedeutet')
    framework = choices[0]
    module = next((m for m in framework.get('modules', []) if m['number'] == module_number), None)
    if module is None or course_number not in module.get('courseNumbers', []):
        raise PackageValidationError('Unbekannter Katalogplatz: ' + prefix + module_number + '.' + course_number)
    return {'frameworkId': framework['id'], 'prefix': prefix, 'version': framework['version'],
            'moduleNumber': module_number, 'courseNumber': course_number, 'label': label,
            'moduleId': module['id'], 'moduleLabel': module['label']}
