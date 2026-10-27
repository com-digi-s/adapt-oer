"""Small authenticated catalogue forms, independent of the application module.

Existing identities are display-only. Every submitted form is bounded,
allowlisted and protected by CSRF plus the catalogue store's compare-and-swap
snapshot. The store remains responsible for durable backups and revisions.
"""
from copy import deepcopy
from pathlib import Path
import re
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from flask_wtf.csrf import generate_csrf, validate_csrf
from wtforms.validators import ValidationError

from lxp_catalogue import NUMBER, PREFIX, PackageValidationError
from lxp_catalogue_store import CatalogueConflictError, read_snapshot, save_catalogue


blueprint = Blueprint('lxp_catalogue_admin', __name__, template_folder='templates')
MAX_FORM_BYTES = 128 * 1024
MAX_FORM_FIELDS = 1200
MAX_TITLES = 100
MAX_QUESTION_COUNT = 10000
PHASES = ('entry', 'final')
FLAGS = ('equalSampling', 'showMarking', 'showModelAnswer')
INTENTS = ('require', 'ifAvailable', 'omit')
COMMON_FIELDS = {'csrf_token', 'expected_sha256', 'action'}
_HASH = re.compile(r'^[0-9a-f]{64}$')


def _paths():
    base = current_app.config.get('LXP_CATALOGUE')
    path = current_app.config.get('LXP_CATALOGUE_STORE_PATH')
    if path is None:
        path = Path(current_app.instance_path) / 'lxp-catalogue.json'
    return base, path


def _form():
    # Browser forms send a known Content-Length. Refusing other encodings and
    # unbounded bodies also prevents multipart/file fields from entering here.
    if (request.mimetype != 'application/x-www-form-urlencoded'
            or request.content_length is None or request.content_length > MAX_FORM_BYTES):
        raise PackageValidationError('Das Formular ist zu groß oder hat ein ungültiges Format. Bitte die Seite neu öffnen.')
    request.max_form_memory_size = MAX_FORM_BYTES
    form = request.form
    if len(form) > MAX_FORM_FIELDS:
        raise PackageValidationError('Das Formular enthält zu viele Felder. Bitte die Seite neu öffnen.')
    if any(len(key) > 180 or len(form.getlist(key)) != 1
           or len(form[key]) > 32000 for key in form):
        raise PackageValidationError('Doppelte oder zu lange Formularfelder. Bitte die Seite neu öffnen.')
    try:
        validate_csrf(form.get('csrf_token'))
    except ValidationError as error:
        raise PackageValidationError('Das Formular ist abgelaufen oder ungültig. Bitte die Seite neu öffnen.') from error
    if not _HASH.fullmatch(form.get('expected_sha256', '')):
        raise PackageValidationError('Die Katalogfassung fehlt. Bitte die Seite neu öffnen.')
    return form


def _allow_fields(form, allowed, required):
    if set(form) - (COMMON_FIELDS | allowed) or (COMMON_FIELDS | required) - set(form):
        raise PackageValidationError('Unbekannte oder fehlende Formularfelder. Bitte die Seite neu öffnen.')


def _text(value, label, limit=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or re.search(r'[\x00-\x1f\x7f]', value)):
        raise PackageValidationError(label + ' fehlt oder ist zu lang.')
    return value.strip()


def _titles(value):
    if not isinstance(value, str) or len(value) > 22000:
        raise PackageValidationError('Die Liste der Menütitel ist zu lang.')
    rows = [line.strip() for line in value.splitlines() if line.strip()]
    if len(rows) > MAX_TITLES or any(len(row) > 200 or '\x00' in row for row in rows):
        raise PackageValidationError('Bitte höchstens 100 Menütitel mit jeweils höchstens 200 Zeichen angeben.')
    if len(set(rows)) != len(rows):
        raise PackageValidationError('Jeden Menütitel bitte nur einmal je Domäne angeben.')
    return rows


def _framework(catalogue, identity):
    framework = next((row for row in catalogue['frameworks'] if row['id'] == identity), None)
    if framework is None:
        raise PackageValidationError('Der gewählte Kompetenzrahmen ist nicht vorhanden. Bitte die Seite neu öffnen.')
    return framework


def _assessment_defaults(framework):
    result = {}
    for phase in PHASES:
        count = (24 if phase == 'entry' else 39) if framework['prefix'] == 'DigComp' else 12
        result[phase] = {'mode': 'balanced', 'questionCount': count, 'equalSampling': True,
                         'showMarking': True, 'showModelAnswer': False}
        result[phase].update(framework.get('defaultAssessmentOptions', {}).get(phase, {}))
    return result


def _read_defaults(form, framework):
    options, intents = {}, {}
    for phase in PHASES:
        name = phase + '_'
        intent, mode = form[name + 'intent'], form[name + 'mode']
        if intent not in INTENTS:
            raise PackageValidationError('Bitte eine gültige Bereitstellung für den Test auswählen.')
        allowed_modes = {'balanced', 'all'}
        if framework['prefix'] == 'DigComp' and framework['version'] == '2.2':
            allowed_modes.add('reference')
        if mode not in allowed_modes:
            raise PackageValidationError('Bitte eine gültige Fragen-Auswahl für diesen Rahmen wählen.')
        raw_count = form[name + 'questionCount']
        if raw_count == '' and mode in ('all', 'reference'):
            count = None
        elif (not NUMBER.fullmatch(raw_count) or len(raw_count) > 5
              or int(raw_count) > MAX_QUESTION_COUNT):
            raise PackageValidationError('Bitte eine ganze Fragenanzahl zwischen 1 und 10000 angeben.')
        else:
            count = int(raw_count)
        if mode == 'reference':
            expected = 24 if phase == 'entry' else 39
            if count not in (None, expected):
                raise PackageValidationError('Die öffentliche DigComp-Auswahl enthält 24 Einstiegs- und 39 Abschlussfragen.')
            count = expected
        values = {'mode': mode, 'questionCount': None if mode == 'all' else count}
        for flag in FLAGS:
            raw = form.get(name + flag)
            if raw not in (None, 'yes', 'no'):
                raise PackageValidationError('Eine Ja-/Nein-Testeinstellung ist ungültig.')
            values[flag] = raw == 'yes'
        options[phase], intents[phase] = values, intent
    return options, intents


def _edit_framework(catalogue, form):
    framework = _framework(catalogue, form.get('framework_id'))
    fields = {'framework_id', 'label', 'default_domain'}
    required = set(fields)
    for phase in PHASES:
        fields.update(phase + '_' + key for key in ('intent', 'mode', 'questionCount', *FLAGS))
        required.update(phase + '_' + key for key in ('intent', 'mode', 'questionCount'))
    for module in framework.get('modules', []):
        fields.update(key + module['id'] for key in ('module_id_', 'module_number_', 'module_label_'))
        required.update(key + module['id'] for key in ('module_id_', 'module_number_', 'module_label_'))
    for domain in framework.get('domains', []):
        fields.update(key + domain['id'] for key in ('domain_id_', 'domain_label_', 'domain_titles_'))
        required.update(key + domain['id'] for key in ('domain_id_', 'domain_label_', 'domain_titles_'))
    _allow_fields(form, fields, required)
    framework['label'] = _text(form['label'], 'Der Rahmenname')
    default_domain = form['default_domain']
    if default_domain and default_domain not in {row['id'] for row in framework.get('domains', [])}:
        raise PackageValidationError('Bitte eine vorhandene Standarddomäne wählen oder die Zuordnung ausdrücklich offen lassen.')
    if default_domain:
        framework['defaultDomainId'] = default_domain
    else:
        framework.pop('defaultDomainId', None)
    framework['defaultAssessmentOptions'], framework['defaultDiagnosticIntents'] = _read_defaults(form, framework)
    for module in framework.get('modules', []):
        identity = module['id']
        if form['module_id_' + identity] != identity or form['module_number_' + identity] != module['number']:
            raise PackageValidationError('Vorhandene Modulnummern und Kennungen dürfen nicht verändert werden.')
        module['label'] = _text(form['module_label_' + identity], 'Der Modulname')
    for domain in framework.get('domains', []):
        identity = domain['id']
        if form['domain_id_' + identity] != identity:
            raise PackageValidationError('Vorhandene Domänenkennungen dürfen nicht verändert werden.')
        domain['label'] = _text(form['domain_label_' + identity], 'Der Domänenname')
        # Keep existing technical sourceTitlePatterns unchanged. This form only
        # edits literal menu names, never executable matching expressions.
        domain['sourceTitles'] = _titles(form['domain_titles_' + identity])
    return 'Die Einstellungen des Kompetenzrahmens wurden gespeichert.'


def _add_framework(catalogue, form):
    _allow_fields(form, {'prefix', 'version', 'label'}, {'prefix', 'version', 'label'})
    prefix = form['prefix']
    if len(prefix) > 48 or not PREFIX.fullmatch(prefix):
        raise PackageValidationError('Das Präfix darf nur ASCII-Buchstaben von A bis Z enthalten, ohne Leerzeichen oder Ziffern.')
    version = _text(form['version'], 'Die Rahmenfassung', 80)
    for existing in catalogue['frameworks']:
        if existing['prefix'].lower() == prefix.lower() and existing['prefix'] != prefix:
            raise PackageValidationError('Dieses Präfix ist bereits in anderer Groß-/Kleinschreibung registriert.')
        if (existing['prefix'], existing['version']) == (prefix, version):
            raise PackageValidationError('Dieses Präfix mit dieser Rahmenfassung ist bereits vorhanden.')
    framework = {'id': 'fw-' + uuid.uuid4().hex, 'prefix': prefix, 'version': version,
                 'label': _text(form['label'], 'Der Rahmenname'), 'modules': [], 'domains': [],
                 'defaultDiagnosticIntents': {'entry': 'ifAvailable', 'final': 'ifAvailable'}}
    framework['defaultAssessmentOptions'] = {phase: {'mode': 'balanced', 'questionCount': 12,
        'equalSampling': True, 'showMarking': True, 'showModelAnswer': False} for phase in PHASES}
    catalogue['frameworks'].append(framework)
    return 'Der Kompetenzrahmen wurde angelegt. Füge jetzt seine Module und Domänen hinzu.'


def _add_module(catalogue, form):
    _allow_fields(form, {'framework_id', 'number', 'label'}, {'framework_id', 'number', 'label'})
    framework = _framework(catalogue, form['framework_id'])
    number = form['number']
    if len(number) > 12 or not NUMBER.fullmatch(number):
        raise PackageValidationError('Bitte eine positive ganze Modulnummer ohne führende Null angeben.')
    if any(row['number'] == number for row in framework.get('modules', [])):
        raise PackageValidationError('Diese Modulnummer ist in dieser Rahmenfassung bereits vorhanden.')
    framework.setdefault('modules', []).append({'id': 'mod-' + uuid.uuid4().hex, 'number': number,
        'label': _text(form['label'], 'Der Modulname'), 'courseNumbers': []})
    return 'Das Modul wurde angelegt. Neue Kursnummern werden beim Export aus den veröffentlichten Kurstiteln erkannt.'


def _add_domain(catalogue, form):
    _allow_fields(form, {'framework_id', 'label', 'source_titles'}, {'framework_id', 'label', 'source_titles'})
    framework = _framework(catalogue, form['framework_id'])
    framework.setdefault('domains', []).append({'id': 'dom-' + uuid.uuid4().hex,
        'label': _text(form['label'], 'Der Domänenname'), 'sourceTitles': _titles(form['source_titles'])})
    return 'Die kognitive Domäne wurde angelegt.'


def _render(snapshot, *, error=None, conflict=False, status=200):
    frameworks = []
    if snapshot:
        for framework in snapshot['catalogue']['frameworks']:
            row = deepcopy(framework)
            row['_assessment'] = _assessment_defaults(framework)
            frameworks.append(row)
    return render_template('lxp_catalogue_admin.html', snapshot=snapshot, frameworks=frameworks,
                           error=error, conflict=conflict, csrf_token=generate_csrf()), status


@blueprint.route('/lxp/catalogue', methods=['GET', 'POST'])
@login_required
def index():
    snapshot = None
    base, store_path = _paths()
    try:
        form = _form() if request.method == 'POST' else None
        snapshot = read_snapshot(base, store_path)
        if form is None:
            return _render(snapshot)
        # Detect a stale schema before reading fields for newly added modules
        # or domains. Otherwise an old form could get a fresh hash on a 400
        # page and accidentally overwrite another window's newer labels.
        if form['expected_sha256'] != snapshot['sha256']:
            raise CatalogueConflictError(snapshot['sha256'])
        actions = {'edit_framework': _edit_framework, 'add_framework': _add_framework,
                   'add_module': _add_module, 'add_domain': _add_domain}
        action = actions.get(form.get('action'))
        if action is None:
            raise PackageValidationError('Diese Katalogaktion ist nicht erlaubt. Bitte die Seite neu öffnen.')
        candidate = deepcopy(snapshot['catalogue'])
        message = action(candidate, form)
        save_catalogue(base, store_path, candidate, form['expected_sha256'])
        flash(message, 'notice')
        return redirect(url_for('lxp_catalogue_admin.index'), code=303)
    except CatalogueConflictError:
        # Do not attach a fresh save token to stale inputs. Only a new GET may
        # open an editable current snapshot after this conflict.
        return _render(None, conflict=True, status=409,
            error='Inzwischen wurde der Katalog in einem anderen Fenster geändert. Deine Eingaben wurden nicht gespeichert. '
                  'Lade den aktuellen Stand neu und prüfe deine Änderungen noch einmal.')
    except PackageValidationError as error:
        return _render(snapshot, error='Nicht gespeichert: ' + str(error), status=400)
