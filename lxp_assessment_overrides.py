"""Explicit, content-bound manual choices within one tagged assessment pool.

This helper does not discover questions, change et/at classes, select a framework
or write profiles. The existing selector supplies its already scoped, validated
phase pool. Every manual reference must still exist there with the same content
fingerprint; a stale profile is never silently reinterpreted.
"""
import re

from lxp_catalogue import PackageValidationError


OVERRIDE_KEYS = frozenset({'includeQuestions', 'excludeQuestions'})
_RECORD_KEYS = {'identity', 'fingerprint'}
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def _identity(value, *, submitted):
    allowed_types = (list,) if submitted else (list, tuple)
    if not isinstance(value, allowed_types) or len(value) != 3:
        raise PackageValidationError('Jede Fragenreferenz benötigt die drei Kennungen für Quelle, Kurs und Komponente.')
    for part in value:
        if not isinstance(part, str) or not part.strip() or _CONTROL.search(part):
            raise PackageValidationError('Fragenkennungen müssen nicht leere Zeichenketten ohne Steuerzeichen sein.')
        try:
            part.encode('utf-8')
        except UnicodeError as error:
            raise PackageValidationError('Fragenkennungen müssen gültiger UTF-8-Text sein.') from error
    return tuple(value)


def _fingerprint(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PackageValidationError('Jede Fragenreferenz benötigt einen gültigen SHA-256-Inhaltsfingerabdruck.')
    return value


def normalize_overrides(options):
    """Return only non-empty, canonical manual-option arrays.

    Other option keys are left to the existing selector's allowlist. Absent or
    empty arrays return no additional keys, retaining the legacy five-option
    policy exactly. Submitted identities must be JSON arrays, not coercible
    strings or tuples. Input dictionaries and records are never modified.
    """
    if not isinstance(options, dict):
        raise PackageValidationError('Die Fragenfeinauswahl muss ein Einstellungsobjekt sein.')
    result, seen_by_key = {}, {}
    for key in ('includeQuestions', 'excludeQuestions'):
        records = options.get(key, [])
        if not isinstance(records, list):
            raise PackageValidationError('Ein- und Ausschlüsse müssen Listen von Fragenreferenzen sein.')
        seen, normalized = set(), []
        for record in records:
            if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
                raise PackageValidationError('Fragenreferenzen dürfen nur identity und fingerprint enthalten.')
            identity = _identity(record['identity'], submitted=True)
            fingerprint = _fingerprint(record['fingerprint'])
            if identity in seen:
                raise PackageValidationError('Eine Frage darf nicht mehrfach zum Ein- oder Ausschließen vorgemerkt werden.')
            seen.add(identity)
            normalized.append({'identity': list(identity), 'fingerprint': fingerprint})
        seen_by_key[key] = seen
        if normalized:
            result[key] = sorted(normalized, key=lambda record: tuple(record['identity']))
    if seen_by_key['includeQuestions'] & seen_by_key['excludeQuestions']:
        raise PackageValidationError('Eine Frage darf nicht gleichzeitig ein- und ausgeschlossen werden.')
    return result


def resolve_overrides(candidates, options):
    """Resolve pinned/excluded choices against the exact current phase pool.

    Returns ``(included, excluded_identity_set, eligible)``. Included and
    eligible rows are original candidate objects. ``eligible`` is the complete
    pool minus exclusions, *including* the pinned questions; callers must remove
    those pins once before filling the remaining selection slots. Included rows
    are ordered by canonical source identity; eligible rows retain input order.
    No framework/course/component identity or content fingerprint is guessed.
    """
    normalized = normalize_overrides(options)
    if not isinstance(candidates, (list, tuple)):
        raise PackageValidationError('Die Fragenfeinauswahl benötigt den aktuellen, zugeordneten Fragenpool.')
    mode = options.get('mode', 'balanced')
    if not isinstance(mode, str) or mode not in ('balanced', 'all', 'reference'):
        raise PackageValidationError('Die Auswahlmethode für den Test ist ungültig.')
    requested = options.get('questionCount')
    if requested is not None and (type(requested) is not int or requested < 1):
        raise PackageValidationError('Die gewünschte Fragenanzahl muss eine positive ganze Zahl sein.')
    if mode == 'reference' and normalized:
        raise PackageValidationError('Die öffentliche Referenzauswahl darf nicht durch einzelne Ein- oder Ausschlüsse verändert werden.')
    pool = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise PackageValidationError('Der aktuelle Fragenpool enthält einen ungültigen Eintrag.')
        identity = _identity(candidate.get('identity'), submitted=False)
        _fingerprint(candidate.get('fingerprint'))
        if identity in pool:
            raise PackageValidationError('Der aktuelle Fragenpool enthält dieselbe Quellfrage mehrfach.')
        pool[identity] = candidate

    resolved = {}
    for key, records in normalized.items():
        values = []
        for record in records:
            identity = tuple(record['identity'])
            candidate = pool.get(identity)
            if candidate is None:
                raise PackageValidationError('Eine vorgemerkte Frage fehlt im aktuellen markierten Fragenpool. '
                    'Bitte Kursauswahl, Kompetenzrahmen und et-/at-Zuordnung prüfen oder die Feinauswahl bewusst zurücksetzen.')
            if candidate['fingerprint'] != record['fingerprint']:
                raise PackageValidationError('Eine vorgemerkte Frage wurde inhaltlich geändert. '
                    'Bitte die Frage erneut prüfen und die Feinauswahl aktualisieren.')
            values.append(candidate)
        resolved[key] = values
    included = resolved.get('includeQuestions', [])
    if mode == 'balanced' and requested is not None and len(included) > requested:
        raise PackageValidationError('Es sind mehr Fragen fest eingeschlossen als für den Test gewünscht. '
            'Bitte die Fragenanzahl erhöhen oder feste Einschlüsse entfernen.')
    excluded = {tuple(candidate['identity']) for candidate in resolved.get('excludeQuestions', [])}
    eligible = [candidate for candidate in candidates if tuple(candidate['identity']) not in excluded]
    return included, excluded, eligible
