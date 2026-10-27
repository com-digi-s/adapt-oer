"""Derive export-local course addresses from explicitly authored course titles.

Frameworks, their versions and modules must already be registered. A valid new
course number is an address inside that known module, not a reason to invent a
framework/module or allocate another source identity. The configured catalogue
and saved source bindings are never changed by these pure helpers.
"""
from copy import deepcopy

from lxp_catalogue import (LEGACY_TITLE, TITLE, PackageValidationError,
                           load_catalogue, normalize_title)


def _request(value, legacy_import):
    if (not isinstance(value, dict) or set(value) - {'title', 'binding'}
            or not isinstance(value.get('title'), str)):
        raise PackageValidationError('Course discovery requires an explicit title and optional source binding')
    title = value['title'].strip()
    match = TITLE.fullmatch(title)
    if match:
        prefix, module_number, course_number, _label = match.groups()
    else:
        match = LEGACY_TITLE.fullmatch(title) if legacy_import else None
        if not match:
            raise PackageValidationError('Ungültiger Kurstitel. Erwartet: DigComp5.1 – Titel oder AILit1.1 – Titel; '
                                         'keine führenden Nullen oder zusätzlichen Zahlensegmente.')
        prefix = 'DigComp'
        module_number, course_number, _label = match.groups()
    binding = value.get('binding')
    if binding is None:
        binding = {}
    if not isinstance(binding, dict):
        raise PackageValidationError('Course discovery source binding must be an object')
    # Match the saved binding to the exact canonical prefix. Numeric legacy
    # titles explicitly mean DigComp and must not inherit another version.
    bound_version = binding.get('version') if binding.get('prefix') == prefix else None
    return title, prefix, module_number, course_number, bound_version


def derive_course_catalogue(catalogue, requests, *, framework_versions=None, legacy_import=False):
    """Return a validated copy extended only with the requested course numbers.

    ``requests`` contains ``{'title': published_title, 'binding': saved_binding}``
    rows. The title must still be checked against the actual frozen publication
    by the caller. Source identity, authorization, legacy-source approval and
    explicit reassignment confirmation remain the caller's existing duties.

    Configured and derived hashes differ intentionally when a new course is
    discovered: freeze the configured hash for update detection, and package
    the returned derived catalogue for pool/selection verification.
    """
    result = load_catalogue(catalogue)
    if not isinstance(requests, (list, tuple)):
        raise PackageValidationError('Course discovery requires a list of selected course requests')
    if type(legacy_import) is not bool:
        raise PackageValidationError('Legacy course discovery requires an explicit boolean')
    if framework_versions is not None and not isinstance(framework_versions, dict):
        raise PackageValidationError('Framework version choices must be an object')
    allowed = {framework['prefix'] for framework in result['frameworks']}
    if set(framework_versions or {}) - allowed:
        raise PackageValidationError('Unknown framework version-selection prefix')
    addresses = set()
    for value in requests:
        title, prefix, module_number, course_number, bound_version = _request(value, legacy_import)
        # Probe additions in all matching configured versions, then delegate
        # version choice/migration/unknown-module checks to the authoritative
        # normalizer. Only its uniquely resolved module is actually extended.
        probe = deepcopy(result)
        for framework in probe['frameworks']:
            if framework['prefix'] != prefix:
                continue
            for module in framework.get('modules', []):
                if module['number'] == module_number and course_number not in module['courseNumbers']:
                    module['courseNumbers'].append(course_number)
        mapping = normalize_title(title, probe, framework_versions=framework_versions,
                                  legacy_import=legacy_import, bound_version=bound_version)
        address = (mapping['frameworkId'], mapping['moduleId'], mapping['courseNumber'])
        if address in addresses:
            raise PackageValidationError('Duplicate selected framework/module/course address: ' + title)
        addresses.add(address)
        framework = next(item for item in result['frameworks'] if item['id'] == mapping['frameworkId'])
        module = next(item for item in framework['modules'] if item['id'] == mapping['moduleId'])
        if course_number not in module['courseNumbers']:
            module['courseNumbers'] = sorted([*module['courseNumbers'], course_number], key=int)
    return load_catalogue(result)


def normalize_course_title(title, catalogue=None, *, framework_versions=None,
                           legacy_import=False, binding=None):
    """Resolve one GUI/export title without requiring a predeclared course slot."""
    request = {'title': title, 'binding': binding}
    derived = derive_course_catalogue(catalogue, [request], framework_versions=framework_versions,
                                     legacy_import=legacy_import)
    _title, _prefix, _module, _course, bound_version = _request(request, legacy_import)
    return normalize_title(title, derived, framework_versions=framework_versions,
                           legacy_import=legacy_import, bound_version=bound_version)
