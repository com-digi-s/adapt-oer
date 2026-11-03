"""Resolve local Markledge reading recommendations without rewriting content.

Only explicitly typed Markdown metadata identifies a destination. Neither a
filename, a title nor a legacy page ID is guessed into a current course ID.
"""
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from lxp_catalogue import PackageValidationError

_KEYS = {'framework', 'frameworkVersion', 'course', 'pageId', 'legacyPageId'}
_REQUIRED = {'framework', 'frameworkVersion', 'course', 'pageId'}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.values.extend(value for key, value in attrs if key == 'href' and value)


def _resource(value):
    if not isinstance(value, str):
        return None
    # This resolver owns local Markdown material identities, not the validity
    # of every historical external course link (some cannot be urlsplit).
    candidate = value.lstrip()
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', candidate) or candidate.startswith('//'):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise PackageValidationError('Ungültige Materialadresse: ' + value) from error
    if parsed.scheme or parsed.netloc or not parsed.path.lower().endswith('.md'):
        return None
    # AAT asset references are relative to the published course root. Do not
    # resolve ../, encoded separators or a guessed alternate asset directory.
    from lxp_multi_export import safe_relative
    try:
        path = safe_relative(parsed.path)
    except (PackageValidationError, ValueError, UnicodeError) as error:
        raise PackageValidationError('Unsicherer Weiterlern-Dateipfad: ' + value) from error
    return path


def linked_markdown(source):
    result = set()

    def visit(value):
        if isinstance(value, dict):
            if '_source' in value:
                resource = _resource(value['_source'])
                if resource:
                    result.add(resource)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and '<' in value:
            parser = _Links()
            parser.feed(value)
            for href in parser.values:
                resource = _resource(href)
                if resource:
                    result.add(resource)

    visit(source['course'])
    visit(source['data'])
    return sorted(result)


def _metadata(path):
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise PackageValidationError('Verlinktes Markdown-Material fehlt oder ist zu groß: ' + path.name)
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeError as error:
        raise PackageValidationError('Markdown-Material benötigt UTF-8-Text: ' + path.name) from error
    match = re.match(r'\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)', text)
    if not match:
        return None
    values = {}
    for line in match[1].splitlines():
        field = re.fullmatch(r'lxp:([A-Za-z]+):\s*(.*?)\s*', line)
        if not field or field[1] not in _KEYS:
            continue
        key, raw = field.groups()
        if key in values:
            raise PackageValidationError('Doppeltes Weiterlern-Metadatum: lxp:' + key)
        try:
            if raw.startswith('"'):
                value = json.loads(raw)
            elif raw.startswith("'") and raw.endswith("'"):
                value = raw[1:-1].replace("''", "'")
            else:
                value = raw
        except ValueError as error:
            raise PackageValidationError('Ungültiges Weiterlern-Metadatum: lxp:' + key) from error
        if not isinstance(value, str) or not value or re.search(r'[\x00-\x20\x7f\\/]', value):
            raise PackageValidationError('Ungültiges Weiterlern-Metadatum: lxp:' + key)
        values[key] = value
    if not values:
        return None  # Ordinary attached Markdown remains ordinary material.
    if not _REQUIRED <= values.keys() or not re.fullmatch(r'[1-9]\d*\.[1-9]\d*', values['course']):
        raise PackageValidationError('Weiterlernziel benötigt Rahmen, Rahmenfassung, Kursnummer und aktuelle Seiten-ID: ' + path.name)
    return values


def project_learning_links(sources):
    """Resolve only selected sources and actual, available content pages."""
    destinations = {}
    for item in sources:
        sco = item['sco']
        framework = item['framework']
        module_number = item['moduleNumber']
        key = (framework['prefix'], framework['version'], module_number + '.' + sco['number'])
        if key in destinations:
            raise PackageValidationError('Mehrdeutiges Weiterlernziel: ' + '/'.join(key))
        destinations[key] = item
    links = []
    for item in sources:
        for resource in linked_markdown(item['source']):
            path = Path(item['frozen']) / unquote(resource)
            metadata = _metadata(path)
            if metadata is None:
                continue
            key = tuple(metadata[k] for k in ('framework', 'frameworkVersion', 'course'))
            target = destinations.get(key)
            if target is None:
                raise PackageValidationError('Weiterlernziel fehlt in der Kursauswahl: ' + key[0] + ' ' + key[2]
                    + ' (' + key[1] + '). Bitte diesen Kurs mit auswählen.')
            pages = [node for node in target['source']['data']['contentObjects']
                if node.get('_id') == metadata['pageId'] and node.get('_type') == 'page' and node.get('_isAvailable', True) is not False]
            if len(pages) != 1:
                raise PackageValidationError('Weiterlern-Seite fehlt oder ist nicht verfügbar: ' + key[0] + ' ' + key[2]
                    + ' / ' + metadata['pageId'] + '. Aktuelle Seiten-ID prüfen; alte IDs werden nicht geraten.')
            links.append({'sourceScoId': item['sco']['id'], 'resourcePath': resource,
                'resourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'targetScoId': target['sco']['id'], 'contentId': metadata['pageId']})
    return sorted(links, key=lambda link: (link['sourceScoId'], link['resourcePath']))


def verify_learning_links(contract, sources):
    expected = project_learning_links(sources)
    if contract.get('learningLinks', []) != expected:
        raise PackageValidationError('Weiterlern-Verknüpfungen stimmen nicht mit den gelieferten Kursen und Markdown-Dateien überein.')
