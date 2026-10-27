from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import re
import os
import uuid
import pymongo
import datetime
import requests
import shutil
import base64
import math
import json
import tempfile
import random
import copy
import zipfile
import pathlib
import hashlib
import secrets
import time

from collections import defaultdict
from config import ADDITIONAL_COMPONENTS
from wtforms import form, fields, validators
from flask import jsonify, Flask, Response, flash, request, abort, send_file, render_template, redirect, url_for, session, after_this_request, send_from_directory
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_babelex import Babel, format_date
from flask_admin import Admin, AdminIndexView, helpers, expose
from flask_admin.actions import action
from flask_admin.contrib.pymongo import ModelView, filters
from flask_admin.model import BaseModelView, typefmt, template
from flask_admin.model.template import EndpointLinkRowAction, LinkRowAction
from flask_admin.form import BaseForm
from flask_admin.contrib.pymongo.filters import FilterLike, FilterEqual
from flask_wtf import FlaskForm
from flask_wtf.csrf import generate_csrf, validate_csrf
from wtforms.validators import ValidationError

from bson import ObjectId, regex, json_util
from werkzeug.utils import secure_filename
from markupsafe import Markup
from bs4 import BeautifulSoup, NavigableString, Tag
from urls import get_editor_url
from filters import *
from utils import *
from users import *
from transpile_bson import *

import config

ADAPT_COURSE_TEMPLATE = "./static/adapt-course-template.zip"

app = Flask(__name__, template_folder="templates")
app.config.from_object(config)
babel = Babel(app)
conn = pymongo.MongoClient(app.config['MONGO_URI'])
db = conn.get_default_database()
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Prepared builds never go into the client session. Tokens are user-bound,
# short-lived, one-use references to frozen server-side preflight results.
app.extensions['lxp_previews'] = {}


def lxp_catalogue():
    from lxp_catalogue import load_catalogue
    return load_catalogue(app.config.get('LXP_CATALOGUE'))


def course_assignment(course, *, legacy_import=False, framework_versions=None):
    from lxp_catalogue import normalize_title
    from lxp_multi_export import get_source_binding
    bound_version = None
    approved_legacy = False
    if course.get('_id'):
        metadata = _lxp_metadata([course])[0]
        registration = get_source_binding(app.config['LXP_REGISTRY_PATH'],
            origin_id=metadata['originId'], course_id=metadata['courseId']) or {}
        bound_version = registration.get('binding', {}).get('version')
        approved_legacy = registration.get('legacyApproved') is True
    return normalize_title(course.get('title', ''), lxp_catalogue(),
                           framework_versions=framework_versions,
                           legacy_import=legacy_import or approved_legacy, bound_version=bound_version)


def add_assignment_columns(model):
    course = model if '_courseId' not in model else db.courses.find_one({'_id': model.get('_courseId')})
    try:
        assignment = course_assignment(course or {})
        model.update(framework=assignment['prefix'], framework_version=assignment['version'],
                     module_number=assignment['moduleNumber'], course_number=assignment['courseNumber'])
        model['_assignment'] = assignment
    except (ValueError, OSError) as error:
        model.update(framework='Nicht zugeordnet', framework_version='—', module_number='—', course_number='—')
        model['_assignment_error'] = str(error)
    return model


def assignment_sort_key(model):
    add_assignment_columns(model)
    assignment = model.get('_assignment', {})
    return (assignment.get('prefix', '\uffff'), assignment.get('version', ''),
            int(assignment.get('moduleNumber', 2147483647)),
            int(assignment.get('courseNumber', 2147483647)),
            model.get('_sortOrder', 0), str(model.get('_id', '')))


def _lxp_csrf():
    try:
        validate_csrf(request.form.get('csrf_token'))
    except ValidationError as error:
        abort(400, description='Formular abgelaufen oder ungültig. Bitte Seite erneut öffnen. ' + str(error))


def _lxp_selected(ids):
    if not ids or len(set(ids)) != len(ids) or not all(ObjectId.is_valid(value) for value in ids):
        raise ValueError('Bitte mindestens einen gültigen veröffentlichten Kurs auswählen; keine doppelten IDs.')
    # The same shared/authorization base query is retained, never replaced by
    # selection or a framework filter.
    courses = list(db.courses.find({'$and': [{'_isShared': True}, {'_id': {'$in': [ObjectId(value) for value in ids]}}]}))
    if len(courses) != len(ids):
        raise ValueError('Ein gewählter Kurs ist nicht mehr freigegeben oder nicht zugänglich.')
    return sorted(courses, key=lambda course: str(course['_id']))


def _lxp_metadata(courses):
    origin = app.config.get('LXP_ORIGIN_ID') or app.config.get('AUTHORING_DOMAIN') or 'aat-local'
    metadata = []
    for course in courses:
        registered = course.get('_lxpSource') or {}
        item = {'kind': registered.get('kind', 'aat-published'),
                         'originId': registered.get('originId', origin),
                         'courseId': registered.get('courseId', str(course['_id'])),
                         'currentTitle': course.get('title', '')}
        if item['kind'] == 'aat-published' and app.config.get('LXP_AAT_BUILD_PROFILE'):
            item['profile'] = app.config['LXP_AAT_BUILD_PROFILE']
        for key in ('profile', 'resourceId', 'questionDomains'):
            if key in registered:
                item[key] = registered[key]
        metadata.append(item)
    return metadata


def _lxp_assessment_defaults(framework, profile):
    """GUI defaults are explicit export choices, not hidden runtime sampling."""
    settings = ((profile or {}).get('settings') or {}).get('assessment_options') or {}
    saved = settings.get(framework['prefix'], settings.get(framework['id'], {}))
    result = {}
    for phase in ('entry', 'final'):
        count = (24 if phase == 'entry' else 39) if framework['prefix'] == 'DigComp' else 12
        result[phase] = {'mode': 'balanced', 'questionCount': count, 'equalSampling': True,
                         'showMarking': True, 'showModelAnswer': False}
        result[phase].update(framework.get('defaultAssessmentOptions', {}).get(phase, {}))
        result[phase].update(saved.get(phase, {}))
    return result


def _lxp_assessment_options(frameworks, profile):
    """Read only the known controls; omitted controls retain the saved choice.

    A per-phase marker distinguishes an unchecked rendered checkbox from an old
    or partial POST that does not yet include these controls.
    """
    controls = {'present', 'mode', 'questionCount', 'equalSampling', 'showMarking', 'showModelAnswer'}
    allowed = {f'assessment_{framework["prefix"]}_{phase}_{key}'
               for framework in frameworks for phase in ('entry', 'final') for key in controls}
    if any(name.startswith('assessment_') and name not in allowed for name in request.form):
        raise ValueError('Unbekannte Testeinstellung. Bitte das Formular erneut öffnen.')
    result = {}
    for framework in frameworks:
        prefix = framework['prefix']
        defaults = _lxp_assessment_defaults(framework, profile)
        result[prefix] = {}
        for phase in ('entry', 'final'):
            name = f'assessment_{prefix}_{phase}_'
            for key in controls:
                if len(request.form.getlist(name + key)) > 1:
                    raise ValueError('Doppelte Testeinstellung. Bitte das Formular erneut öffnen.')
            present = request.form.get(name + 'present')
            if present not in (None, 'yes'):
                raise ValueError('Ungültige Testeinstellung.')
            values = dict(defaults[phase])
            mode = request.form.get(name + 'mode', values['mode'])
            if mode not in ('balanced', 'all', 'reference'):
                raise ValueError('Ungültige Fragen-Auswahl.')
            if mode == 'reference' and (prefix != 'DigComp' or request.form.get('version_' + prefix, framework['version']) not in ('', '2.2')):
                raise ValueError('Die öffentliche Fragen-Auswahl ist nur für DigComp 2.2 verfügbar.')
            raw_count = request.form.get(name + 'questionCount')
            if raw_count is not None:
                if raw_count == '' and mode in ('all', 'reference'):
                    count = None
                elif not re.fullmatch(r'[0-9]+', raw_count or '') or int(raw_count) < 1:
                    raise ValueError('Bitte eine positive ganze Fragenanzahl angeben.')
                else:
                    count = int(raw_count)
            else:
                count = values['questionCount']
            if mode == 'reference':
                count = 24 if phase == 'entry' else 39
            if mode == 'balanced' and count is None:
                raise ValueError('Bitte für die eigene Auswahl eine positive ganze Fragenanzahl angeben.')
            values.update(mode=mode, questionCount=None if mode == 'all' else count)
            for key in ('equalSampling', 'showMarking', 'showModelAnswer'):
                value = request.form.get(name + key)
                if value is not None:
                    if value not in ('yes', 'no'):
                        raise ValueError('Ungültige Ja-/Nein-Testeinstellung.')
                    values[key] = value == 'yes'
                elif present:
                    values[key] = False
            result[prefix][phase] = values
    return result


def _lxp_fingerprint(builds, metadata, settings):
    """Bind confirmation to live inputs as well as the backend's frozen copy."""
    digest = hashlib.sha256(json.dumps({'metadata': metadata, 'settings': settings,
                                      'catalogue': lxp_catalogue()}, sort_keys=True, default=str).encode())
    for root in [pathlib.Path(value) for value in builds] + [pathlib.Path(app.config['LXP_WRAPPER_ZIP'])]:
        paths = sorted(root.rglob('*')) if root.is_dir() else [root]
        for path in paths:
            if path.is_file():
                digest.update(str(path.relative_to(root) if root.is_dir() else path.name).encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _lxp_prune_previews():
    previews = app.extensions['lxp_previews']
    for token, value in list(previews.items()):
        if time.time() - value['created'] > app.config['LXP_PREVIEW_TTL_SECONDS']:
            previews.pop(token, None)
            value['prepared'].close()
    db.lxp_export_previews.delete_many({'created': {'$lt': time.time() - app.config['LXP_PREVIEW_TTL_SECONDS']}})


def _lxp_preview(token):
    _lxp_prune_previews()
    record = db.lxp_export_previews.find_one({'_id': token, 'owner': current_user.get_id(), 'state': 'pending'})
    if not record:
        abort(410, description='Diese Vorschau ist abgelaufen oder bereits bestätigt. Bitte eine neue Vorschau erstellen.')
    value = app.extensions['lxp_previews'].get(token)
    if not value:
        # A different WSGI worker (or a restarted process) can reconstruct the
        # same frozen bytes only after all original live inputs match. The
        # persisted exact hash, never a browser-provided contract, is authority.
        from lxp_export import prepare_multi_framework_export
        prepared = None
        try:
            metadata = _lxp_metadata(_lxp_selected(record['selected']))
            if record['fingerprint'] != _lxp_fingerprint(record['builds'], metadata, record['settings']):
                raise ValueError('Quellen seit der Vorschau geändert')
            prepared = prepare_multi_framework_export(record['builds'], app.config['LXP_WRAPPER_ZIP'],
                app.config['LXP_ASSESSMENT_TEMPLATE'], source_metadata=metadata,
                registry_path=app.config['LXP_REGISTRY_PATH'], catalogue=lxp_catalogue(),
                expected_wrapper_sha256=app.config.get('LXP_WRAPPER_SHA256'), **record['settings'])
            if prepared.preflight_sha256 != record['preflight_sha256']:
                raise ValueError('Der gespeicherte Preflight stimmt nicht mehr überein')
            value = {key: item for key, item in record.items() if key not in ('_id', 'state', 'preflight_sha256')}
            value['prepared'] = prepared
            app.extensions['lxp_previews'][token] = value
        except (ValueError, OSError, zipfile.BadZipFile) as error:
            if prepared:
                prepared.close()
            db.lxp_export_previews.delete_one({'_id': token})
            abort(410, description='Bitte eine neue Vorschau erstellen. ' + str(error))
    return value


@app.route('/lxp/export', methods=['GET', 'POST'])
@login_required
def lxp_export_setup():
    from lxp_export import prepare_multi_framework_export
    owner = current_user.get_id()
    profiles = list(db.lxp_export_profiles.find({'owner': owner}))
    profile_id = request.values.get('profile_id', '')
    profile = next((value for value in profiles if value['_id'] == profile_id), None)
    selected = request.form.getlist('course_id') if request.method == 'POST' else (request.args.getlist('course_id') or (profile or {}).get('course_ids', []))
    catalogue = lxp_catalogue()
    frameworks = catalogue.get('frameworks', [])
    if isinstance(frameworks, dict):
        frameworks = list(frameworks.values())
    if request.method == 'POST':
        _lxp_csrf()
        prepared = None
        try:
            if profile_id and not profile:
                raise ValueError('Das gewählte Exportprofil ist nicht verfügbar.')
            if not app.config.get('LXP_WRAPPER_ZIP'):
                raise ValueError('Bitte die Administration einen geprüften Multi-Framework-Wrapper konfigurieren lassen.')
            courses = _lxp_selected(selected)
            builds = [os.path.join(app.config['BUILDS_DIR'], str(course['_id']), 'build') for course in courses]
            metadata = _lxp_metadata(courses)
            diagnostics, versions = {}, {}
            saved_diagnostics = ((profile or {}).get('settings') or {}).get('diagnostics') or {}
            for framework in frameworks:
                prefix = framework['prefix']
                diagnostics[prefix] = {}
                for phase in ('entry', 'final'):
                    default = framework.get('defaultDiagnosticIntents', {}).get(phase, 'ifAvailable')
                    saved = saved_diagnostics.get(prefix, saved_diagnostics.get(framework['id'], {}))
                    intent = request.form.get(f'test_{prefix}_{phase}', saved.get(phase, default))
                    if intent not in ('omit', 'ifAvailable', 'require'):
                        raise ValueError('Ungültiger Testauftrag.')
                    diagnostics[prefix][phase] = intent
                chosen_version = request.form.get('version_' + prefix)
                if chosen_version:
                    versions[prefix] = chosen_version
            required = {entry['originId'] + '::' + entry['courseId']: str(course['_id']) in request.form.getlist('required_course') for entry, course in zip(metadata, courses)}
            package_id = profile['package_id'] if profile else 'lxp-package-' + str(uuid.uuid4())
            revision = str(int((profile or {}).get('revision', 0)) + 1)
            title = request.form.get('title', '').strip()
            if not title:
                raise ValueError('Bitte einen Titel für das Lernangebot angeben.')
            settings = dict(package_id=package_id, revision=revision, title=title, diagnostics=diagnostics,
                            assessment_options=_lxp_assessment_options(frameworks, profile),
                            framework_versions=versions, required_courses=required,
                            target_profile_id=app.config['LXP_TARGET_PROFILE_ID'],
                            legacy_import=request.form.get('legacy_import') == 'yes',
                            confirm_reassignment=request.form.get('confirm_reassignment') == 'yes')
            if settings['legacy_import']:
                from lxp_export import published_course
                from lxp_multi_export import approve_legacy_source
                for build, entry in zip(builds, metadata):
                    if re.match(r'^\d+\.', entry['currentTitle'].strip()):
                        if published_course(build)['course'].get('title') != entry['currentTitle']:
                            raise ValueError('Bitte Kurs erneut veröffentlichen: aktueller Title stimmt nicht mit der veröffentlichten Altquelle überein.')
                        approve_legacy_source(app.config['LXP_REGISTRY_PATH'], origin_id=entry['originId'],
                            course_id=entry['courseId'], current_title=entry['currentTitle'],
                            catalogue=catalogue, framework_version=versions.get('DigComp'))
            fingerprint = _lxp_fingerprint(builds, metadata, settings)
            prepared = prepare_multi_framework_export(builds, app.config['LXP_WRAPPER_ZIP'],
                app.config['LXP_ASSESSMENT_TEMPLATE'], source_metadata=metadata,
                registry_path=app.config['LXP_REGISTRY_PATH'], catalogue=catalogue,
                expected_wrapper_sha256=app.config.get('LXP_WRAPPER_SHA256'), **settings)
            if fingerprint != _lxp_fingerprint(builds, _lxp_metadata(_lxp_selected(selected)), settings):
                raise ValueError('Quellen wurden während der Vorschau geändert. Bitte erneut veröffentlichen und Vorschau wiederholen.')
            if not profile:
                profile_id = secrets.token_urlsafe(18)
                profile = {'_id': profile_id, 'owner': owner, 'package_id': package_id, 'revision': 0}
                db.lxp_export_profiles.insert_one(profile)
            token = secrets.token_urlsafe(32)
            _lxp_prune_previews()
            app.extensions['lxp_previews'][token] = dict(prepared=prepared, owner=owner,
                created=time.time(), selected=selected, builds=builds, settings=settings,
                fingerprint=fingerprint, profile_id=profile_id, previous_revision=profile['revision'])
            db.lxp_export_previews.insert_one({'_id': token, 'state': 'pending',
                'preflight_sha256': prepared.preflight_sha256,
                **{key: value for key, value in app.extensions['lxp_previews'][token].items() if key != 'prepared'}})
            return redirect(url_for('lxp_export_preview', token=token))
        except (ValueError, OSError, zipfile.BadZipFile) as error:
            if prepared:
                prepared.close()
            flash('LXP-Export gestoppt: ' + str(error), 'error')
    courses = list(db.courses.find({'_isShared': True}))
    courses.sort(key=assignment_sort_key)
    for course, source in zip(courses, _lxp_metadata(courses)):
        course['_lxp_required_key'] = source['originId'] + '::' + source['courseId']
    return render_template('lxp_export.html', courses=courses, frameworks=frameworks, profiles=profiles,
        profile=profile, selected=selected, csrf_token=generate_csrf(),
        assessment_form={framework['prefix']: _lxp_assessment_defaults(framework, profile) for framework in frameworks})


@app.route('/lxp/preview/<token>')
@login_required
def lxp_export_preview(token):
    value = _lxp_preview(token)
    return render_template('lxp_preview.html', token=token, preview=value['prepared'].preview,
        contract=value['prepared'].contract, csrf_token=generate_csrf())


@app.route('/lxp/confirm/<token>', methods=['POST'])
@login_required
def lxp_export_confirm(token):
    from lxp_export import build_multi_framework_export
    _lxp_csrf()
    value = _lxp_preview(token)
    prepared = value['prepared']
    claimed = db.lxp_export_previews.find_one_and_update({'_id': token, 'owner': current_user.get_id(),
        'state': 'pending'}, {'$set': {'state': 'exporting'}})
    if not claimed:
        abort(410, description='Diese Vorschau wird bereits exportiert. Bitte eine neue Vorschau erstellen.')
    try:
        if request.form.get('preflight_sha256') != prepared.preflight_sha256:
            raise ValueError('Die bestätigte Vorschau stimmt nicht überein. Bitte neue Vorschau erstellen.')
        metadata = _lxp_metadata(_lxp_selected(value['selected']))
        if value['fingerprint'] != _lxp_fingerprint(value['builds'], metadata, value['settings']):
            raise ValueError('Quelle, Titel, Katalog oder Wrapper wurde seit der Vorschau geändert. Bitte eine neue Vorschau erstellen und Kurse gegebenenfalls erneut veröffentlichen.')
        profile = db.lxp_export_profiles.find_one({'_id': value['profile_id'], 'owner': current_user.get_id()})
        if not profile or profile['revision'] != value['previous_revision']:
            raise ValueError('Dieses Lernangebot wurde inzwischen exportiert. Bitte eine neue Vorschau erstellen.')
        stream = build_multi_framework_export(prepared, confirmed_preflight_sha256=prepared.preflight_sha256,
                                               source_metadata=metadata)
        updated = db.lxp_export_profiles.update_one({'_id': profile['_id'], 'owner': current_user.get_id(),
            'revision': value['previous_revision']}, {'$set': {'revision': int(value['settings']['revision']),
            'title': value['settings']['title'], 'course_ids': value['selected'], 'settings': value['settings']}})
        if updated.modified_count != 1:
            raise ValueError('Exportprofil wurde gleichzeitig geändert. Bitte eine neue Vorschau erstellen.')
        return send_file(stream, as_attachment=True, download_name=secure_filename(value['settings']['title']) + '.zip', mimetype='application/zip')
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        flash('LXP-Export gestoppt: ' + str(error), 'error')
        return redirect(url_for('lxp_export_setup', profile_id=value['profile_id']))
    finally:
        app.extensions['lxp_previews'].pop(token, None)
        db.lxp_export_previews.delete_one({'_id': token})
        prepared.close()

# Here, we only have one user (admin user)
users = [User(1, app.config['USERNAME'], app.config['PASSWORD'])]

@login_manager.user_loader
def load_user(user_id):
    return next((user for user in users if user.id == int(user_id)), None)

@app.before_request
def before_request():
    if not current_user.is_authenticated and request.endpoint not in ['login', 'static']:
        return redirect(url_for('login'))

@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('coursesview.index_view'))
    else:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = next((user for user in users if user.username == request.form['username']), None)
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('coursesview.index_view'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

def filter_components_and_blocks(id_list):
    components = db.components.find({'_id': {'$in': id_list}})
    comp_json = json.dumps(list(components), default=json_util.default)

    parent_ids = [comp['_parentId']['$oid'] for comp in json.loads(comp_json)]
    blocks = db.blocks.find({'_id': {'$in': [ObjectId(_id) for _id in parent_ids]}})
    block_json = json.dumps(list(blocks), default=json_util.default)

    return comp_json, block_json

def get_related_content_index(content_object, collection='articles'):
    if not content_object:
        return ''

    # Fetch the parent content object of the specific content object
    parent_content_object_id = content_object.get('_parentId', '')
    parent_content_object = db.contentobjects.find_one({"_id": parent_content_object_id})

    if not parent_content_object and collection == 'contentobjects':
        parent_content_object = db.courses.find_one({'_id': parent_content_object_id})
    if not parent_content_object:
        return ''

    # Fetch all related content objects that share the same parent
    parent_id = parent_content_object['_id']
    related_content_objects = db[collection].find({"_parentId": parent_id}).sort([('_sortOrder', 1), ('_id', 1)])
    related_content_objects_list = list(related_content_objects)

    # Get the list of IDs of all related content objects
    related_content_ids = [related_content['_id'] for related_content in related_content_objects_list]

    # Find the index of the specific content object within the list of related content objects
    specific_content_id = content_object['_id']
    related_content_index = related_content_ids.index(specific_content_id)

    return related_content_index

def fetch_and_nest_content(content_id, components, db, panel=None):
    """Recursively fetch and nest content based on _parentId."""
    current_object = db.blocks.find_one({'_id': content_id})

    if current_object:
        panel = {
            "title": Markup("<h3>" + current_object["title"] + "</h3>"),
            "body": Markup(current_object["body"] + "<br><br>")
        }

        # Fetch and process components for the current block
        components_list = [comp for comp in components if comp['_parentId'] == current_object['_id']]
        # Sort components by _sortOrder
        components_list = sorted(components_list, key=lambda x: x.get('_sortOrder', 0))

        for idx, component in enumerate(components_list):
            if component["_component"] in ['accordion', 'narrative']:
                body = "<br><br>".join(
                    f"<section><header><h4>{item.get('title', '')}</h4></header>: " + "<div>" + f"{item.get('body', '')}".replace("<p>", "").replace("</p>", "") + "</div>" + "</section>"
                    for item in component.get('properties', {}).get('_items', [])
                )
            else:
                body = Markup(component["body"])

            # Append the component inside the div
            panel["body"] += Markup('<div>') + Markup(str(idx + 1) + ". " + component["title"] + "<br><br>") + body + Markup('</div>')

        return fetch_and_nest_content(current_object['_parentId'], None, db, panel=panel)
    else:
        # Get all blocks for the current object, sorted by _sortOrder
        blocks = list(db.blocks.find({'_parentId': content_id}).sort('_sortOrder', 1))

        # Process each block and append to the wrapper
        wrapper = panel
        wrapper["title"] = ""
        wrapper["body"] = ""
        for block in blocks:
            wrapper["body"] += Markup('<div>') + block["title"] + Markup("".join(block["body"])) + Markup('</div>')

        return wrapper

def prepare_quizzes(directory, quiz_ids, zip_file):
    quiz_folder = compose_quiz(quiz_ids)
    zip_target = f"scos/{directory}"

    add_files_to_zip(zip_file, quiz_folder, zip_target)
    shutil.rmtree(quiz_folder)

def compose_quiz(ids):
    quiz_template = f'static/assessment-template.zip'

    selected = list(db.components.find({'_id': {'$in': [ObjectId(value) for value in ids]}}))
    if not selected or len(selected) != len(set(ids)):
        raise ValueError('Mindestens eine ausgewählte Frage fehlt.')
    for question in selected:
        course = db.courses.find_one({'_id': question.get('_courseId'), '_isShared': True})
        if not course:
            raise ValueError('Eine Frage gehört nicht zu einem freigegebenen zugänglichen Kurs.')
        if not re.match(r'^\d+\.', course.get('title', '')):
            # Resolve through the same normalizer so unknown prefixes are never
            # silently treated as DigComp by this older action.
            course_assignment(course)
            raise ValueError('Der alte Einzelquiz-Export unterstützt keine Kompetenzrahmen. Bitte die gemeinsame LXP-Vorschau mit getrennten Tests verwenden.')
        course_assignment(course, legacy_import=True)
        if question.get('_component') not in ('slider', 'mcq', 'matching', 'infai-dragndrop') or 'self-assessment' in question.get('_classes', '').split():
            raise ValueError('Die ausgewählte Frage wird von diesem Legacy-Einzelquiz nicht unterstützt; es werden keine Fragen übersprungen.')

    # Create a temporary directory
    tmpdirname = tempfile.mkdtemp()

    try:
        # Extract the quiz template
        with ZipFile(quiz_template, 'r') as zip_ref:
            zip_ref.extractall(tmpdirname)

        # Filter and save the components and blocks
        components, blocks = filter_components_and_blocks([ObjectId(_id) for _id in ids])

        # Save the filtered data to the specified paths:
        with open(os.path.join(tmpdirname, 'course/en/components.json'), 'r+', encoding="utf-8") as cf:
            transpiled_components = transpile_data(json.loads(components))
            # white list components
            # Step 1: Filter the components
            transpiled_components = [
                comp for comp in transpiled_components
                if (comp['_component'] in ['slider', 'mcq', 'matching', 'infai-dragndrop']
                        and 'self-assessment' not in comp['_classes'])
            ]

            # Step 2: Modify specific slider components
            for comp in transpiled_components:
                comp['_canShowFeedback'] = False

                if comp['_component'] == 'slider' and 'attitude' in comp['_classes']:
                    # Set the specified properties to False
                    comp['_attempts'] = False
                    comp['_canShowModelAnswer'] = False
                    comp['_canShowMarking'] = False
                    comp['_shouldDisplayAttempts'] = False

                    # Update the _correctRange
                    comp['_correctRange'] = {
                        '_bottom': comp['_scaleStart'],
                        '_top': comp['_scaleEnd']
                    }

            with open(os.path.join(tmpdirname, 'course/en/blocks.json'), 'r+', encoding='utf-8') as bf:
                transpiled_blocks = transpile_data(json.loads(blocks))
                # only blocks of relevant components
                transpiled_blocks = [block for block in transpiled_blocks if block['_id'] in [component['_parentId'] for component in transpiled_components]]

                for idx, block in enumerate(transpiled_blocks):
                    article_parent_id = db.articles.find_one({'_id': ObjectId(block['_parentId'])})['_parentId']
                    content_object = db.contentobjects.find_one({'_id': article_parent_id})
                    domain = db.contentobjects.find_one({'_id': content_object['_parentId']})
                    if not domain:
                        raise ValueError('Dieser alte Einzelquiz-Export benötigt eine explizite A/B/C-Domäne. Für flache Kurse bitte die gemeinsame LXP-Vorschau verwenden.')
                    course_title = db.courses.find_one({'_id': domain['_parentId']})['title']

                    if not re.match(r'^\d+\.', course_title):
                        raise ValueError('Der alte Einzelquiz-Export unterstützt keine Kompetenzrahmen. Bitte die gemeinsame LXP-Vorschau mit getrennten Tests verwenden.')
                    assignment = course_assignment({'title': course_title}, legacy_import=True)
                    domain_number = {'A': 1, 'B': 2, 'C': 3}.get(str(domain.get('title', '')).strip()[:1])
                    if assignment['prefix'] != 'DigComp' or not domain_number or int(assignment['moduleNumber']) > 9 or int(assignment['courseNumber']) > 9:
                        raise ValueError('Die Legacy-Quizbank unterstützt diese Zuordnung nicht. Bitte den gemeinsamen LXP-Export verwenden.')
                    result = int(assignment['moduleNumber'] + assignment['courseNumber'] + str(domain_number))

                    block["_assessment"] = {
                        "_quizBankID": result
                    }

                    block["_attempts"] = 1

                    block['_parentId'] = '32550f88-351c-4a6e-8475-dd87686cc273'  # parentId given in the template
                    block['_trackingId'] = idx # generate unique trackingId

                for component in transpiled_components:
                    if component['_component'] == 'mcq':
                        # Count the number of items that should be selected
                        should_be_selected_count = sum(1 for x in component['_items'] if x.get('_shouldBeSelected', False))
                        component['_selectable'] = 1 if should_be_selected_count == 1 else len(component['_items'])

                        component["_id"] = str(uuid.uuid4())
                        component['_layout'] = "full"
                        component["_onScreen"]["_percentInviewVertical"] = 100

                    component['_attempts'] = 1

                transpiled_components.append(json.load(cf)[0])
                cf.truncate(0)
                cf.seek(0)
                cf.write(json.dumps(transpiled_components, indent=2))

                results_block = json.load(bf)[0]
                results_block['_trackingId'] = idx + 1
                transpiled_blocks.append(results_block)

                bf.truncate(0)
                bf.seek(0)
                bf.write(json.dumps(transpiled_blocks, indent=2))

    except Exception:
        shutil.rmtree(tmpdirname)
        raise

    return tmpdirname


def parse_taxonomy_string(taxonomy):
    """
    Erwartet z.B. '1.2.A.3' und liefert einen stabil sortierbaren Schlüssel.
    Nicht passende Werte werden ans Ende sortiert.
    """
    match = re.match(r"^(\d+)\.(\d+)\.([A-Z])\.(\d+)$", str(taxonomy))
    if not match:
        return (999, 999, 'Z', 999)
    return (int(match.group(1)), int(match.group(2)), match.group(3), int(match.group(4)))


def get_learning_unit_taxonomy(unit, *, legacy_import=False):
    raw_index = get_related_content_index(unit, 'contentobjects')
    if not isinstance(raw_index, int):
        return None

    learning_unit = str(raw_index + 1)
    course = db.courses.find_one({'_id': unit['_courseId']})
    domain = db.contentobjects.find_one({'_id': unit['_parentId']})

    if not course:
        return None
    assignment = course_assignment(course, legacy_import=legacy_import)
    address = ('' if legacy_import else assignment['prefix']) + assignment['moduleNumber'] + '.' + assignment['courseNumber']
    domain_abbrev = str((domain or {}).get('title', '')).strip()[:1].upper()
    return address + ('.' + domain_abbrev if domain_abbrev in ('A', 'B', 'C') else '') + '.' + learning_unit


def get_domain_letter_for_unit(unit):
    domain = db.contentobjects.find_one({'_id': unit['_parentId']})
    if not domain:
        return None
    return str(domain.get('title', '')).strip()[:1].upper()


def make_24hex(prefix, old_id):
    h = hashlib.sha1(f"{prefix}:{old_id}".encode("utf-8")).hexdigest()
    return h[:24]


def replace_refs(node, mapping):
    if isinstance(node, str):
        return mapping.get(node, node)
    if isinstance(node, list):
        return [replace_refs(x, mapping) for x in node]
    if isinstance(node, dict):
        return {k: replace_refs(v, mapping) for k, v in node.items()}
    return node


def collect_asset_paths_from_node(node, collected=None):
    """
    Sammelt rekursiv referenzierte Adapt-Assets unter course/en/assets/.
    """
    if collected is None:
        collected = set()

    if isinstance(node, dict):
        for value in node.values():
            collect_asset_paths_from_node(value, collected)

    elif isinstance(node, list):
        for item in node:
            collect_asset_paths_from_node(item, collected)

    elif isinstance(node, str):
        normalized = node.replace("\\", "/").strip()

        if normalized.startswith("course/en/assets/"):
            collected.add(normalized)

    return collected


def collect_course_asset_paths(*roots):
    """
    Sammelt alle referenzierten Adapt-Assets aus beliebig vielen JSON-Strukturen.
    """
    asset_paths = set()

    for root in roots:
        collect_asset_paths_from_node(root, asset_paths)

    return asset_paths


def copy_graphic_to_menu(menu, page):
    """
    Übernimmt die Grafik einer Lerneinheit für das übergeordnete Menü.
    """
    graphic = page.get('_graphic')

    if graphic:
        menu['_graphic'] = copy.deepcopy(graphic)


def copy_used_assets_to_template(asset_paths, source_course_ids, tmp_project_dir):
    """
    Kopiert referenzierte Assets aus den Quellkursen in die Zielvorlage.
    Erwartet Asset-Pfade relativ zu build/, z.B.:
    course/en/assets/foo.png
    course/en/assets/bar.mp4
    """
    if not asset_paths:
        return

    target_root = os.path.join(tmp_project_dir, 'src')

    for asset_path in sorted(asset_paths):
        copied = False

        for course_id in source_course_ids:
            source_root = os.path.join(app.config['BUILDS_DIR'], str(course_id), 'build')
            source_file = os.path.join(source_root, asset_path)

            if os.path.exists(source_file):
                target_file = os.path.join(target_root, asset_path)
                os.makedirs(os.path.dirname(target_file), exist_ok=True)
                shutil.copy2(source_file, target_file)
                copied = True
                break

        if not copied:
            app.logger.warning(f"Asset nicht gefunden und daher nicht kopiert: {asset_path}")


def add_project_files_to_zip(zip_file, base_dir):
    """
    Packt ein Adapt-Projekt für den Authoring-Import.
    build/, dist/, node_modules/ usw. werden ausgeschlossen.
    """
    excluded_prefixes = [
        'build/',
        'dist/',
        'node_modules/',
        '.git/',
        '.cache/',
        '.grunt/',
        'tmp/'
    ]
    excluded_names = {'.DS_Store'}

    for foldername, _, filenames in os.walk(base_dir):
        for filename in filenames:
            file_path = os.path.join(foldername, filename)
            arcname = os.path.relpath(file_path, base_dir).replace("\\", "/")

            if filename in excluded_names:
                continue

            if any(arcname.startswith(prefix) for prefix in excluded_prefixes):
                continue

            zip_file.write(file_path, arcname, ZIP_DEFLATED)


def load_built_course_structure(course_id):
    """
    Lädt die vorhandenen Kurs-JSONs eines veröffentlichten Kurses aus:
    <BUILDS_DIR>/<course_id>/build/course/en/
    """
    build_root = os.path.join(app.config['BUILDS_DIR'], str(course_id), 'build')
    base_dir = os.path.join(build_root, 'course', 'en')

    if not os.path.exists(base_dir):
        raise ValueError(f"Build-Struktur nicht gefunden: {base_dir}")

    data = {
        'build_root': build_root,
        'base_dir': base_dir,
        'course': read_json_file(os.path.join(base_dir, 'course.json')) or {},
        'contentObjects': read_json_file(os.path.join(base_dir, 'contentObjects.json')) or [],
        'articles': read_json_file(os.path.join(base_dir, 'articles.json')) or [],
        'blocks': read_json_file(os.path.join(base_dir, 'blocks.json')) or [],
        'components': read_json_file(os.path.join(base_dir, 'components.json')) or [],
    }

    return data


def extract_learning_unit_subtree(course_data, unit_id):
    """
    Schneidet aus den Build-JSONs den vollständigen Unterbaum einer Lerneinheit heraus:
    contentObject (unit) -> articles -> blocks -> components
    """
    contentobjects = course_data['contentObjects']
    articles = course_data['articles']
    blocks = course_data['blocks']
    components = course_data['components']

    unit = next((obj for obj in contentobjects if obj['_id'] == str(unit_id)), None)
    if not unit:
        raise ValueError(f"Lerneinheit {unit_id} nicht in contentObjects.json gefunden.")

    unit_articles = [a for a in articles if a.get('_parentId') == unit['_id']]
    article_ids = {a['_id'] for a in unit_articles}

    unit_blocks = [b for b in blocks if b.get('_parentId') in article_ids]
    block_ids = {b['_id'] for b in unit_blocks}

    unit_components = [c for c in components if c.get('_parentId') in block_ids]

    return {
        'unit': copy.deepcopy(unit),
        'articles': copy.deepcopy(unit_articles),
        'blocks': copy.deepcopy(unit_blocks),
        'components': copy.deepcopy(unit_components),
    }


def remap_subtree_ids(subtree, new_menu_id, new_unit_title, prefix, base_course_id):
    """
    Nimmt einen Teilbaum aus den Build-/JSON-Dateien:
    page -> articles -> blocks -> components
    und hängt ihn unter ein Ziel-menu.
    """
    id_map = {}

    old_page_id = subtree['unit']['_id']
    new_page_id = make_24hex(prefix, old_page_id)
    id_map[old_page_id] = new_page_id

    for article in subtree['articles']:
        id_map[article['_id']] = make_24hex(prefix, article['_id'])

    for block in subtree['blocks']:
        id_map[block['_id']] = make_24hex(prefix, block['_id'])

    for component in subtree['components']:
        id_map[component['_id']] = make_24hex(prefix, component['_id'])

    new_page = replace_refs(copy.deepcopy(subtree['unit']), id_map)
    new_page['_type'] = 'page'
    new_page['_id'] = new_page_id
    new_page['_parentId'] = new_menu_id
    new_page['title'] = new_unit_title
    new_page['displayTitle'] = new_unit_title

    new_articles = []
    for article in subtree['articles']:
        new_article = replace_refs(copy.deepcopy(article), id_map)
        new_article['_id'] = id_map[article['_id']]
        new_article['_parentId'] = new_page_id
        if '_courseId' in new_article:
            new_article['_courseId'] = base_course_id
        new_articles.append(new_article)

    new_blocks = []
    for block in subtree['blocks']:
        new_block = replace_refs(copy.deepcopy(block), id_map)
        new_block['_id'] = id_map[block['_id']]
        if '_courseId' in new_block:
            new_block['_courseId'] = base_course_id
        new_blocks.append(new_block)

    new_components = []
    for component in subtree['components']:
        new_component = replace_refs(copy.deepcopy(component), id_map)
        new_component['_id'] = id_map[component['_id']]
        if '_courseId' in new_component:
            new_component['_courseId'] = base_course_id
        new_components.append(new_component)

    return new_page, new_articles, new_blocks, new_components


def write_assembled_course_jsons(tmp_project_dir, contentobjects, articles, blocks, components):
    course_en_dir = os.path.join(tmp_project_dir, 'src', 'course', 'en')
    os.makedirs(course_en_dir, exist_ok=True)

    with open(os.path.join(course_en_dir, 'contentObjects.json'), 'w', encoding='utf-8') as f:
        json.dump(contentobjects, f, ensure_ascii=False, indent=2)

    with open(os.path.join(course_en_dir, 'articles.json'), 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    with open(os.path.join(course_en_dir, 'blocks.json'), 'w', encoding='utf-8') as f:
        json.dump(blocks, f, ensure_ascii=False, indent=2)

    with open(os.path.join(course_en_dir, 'components.json'), 'w', encoding='utf-8') as f:
        json.dump(components, f, ensure_ascii=False, indent=2)


def extract_adapt_course_template():
    """
    Entpackt die statische Adapt-Projektvorlage aus static/adapt-course-template.zip
    und liefert das Temp-Verzeichnis zurück.
    """
    template_zip = ADAPT_COURSE_TEMPLATE

    if not os.path.exists(template_zip):
        raise ValueError(f"Adapt-Kursvorlage nicht gefunden: {template_zip}")

    tmp_project_dir = tempfile.mkdtemp()

    with ZipFile(template_zip, 'r') as zip_ref:
        zip_ref.extractall(tmp_project_dir)

    expected_course_dir = os.path.join(tmp_project_dir, 'src', 'course', 'en')
    if not os.path.exists(expected_course_dir):
        raise ValueError(
            f"Adapt-Kursvorlage enthält kein src/course/en: {expected_course_dir}"
        )

    return tmp_project_dir


def load_template_course_json(tmp_project_dir):
    course_json_path = os.path.join(tmp_project_dir, 'src', 'course', 'en', 'course.json')
    course_json = read_json_file(course_json_path)
    if not course_json:
        raise ValueError(f"Vorlagen-course.json nicht gefunden oder leer: {course_json_path}")
    return course_json


def load_template_content_objects(tmp_project_dir):
    path = os.path.join(tmp_project_dir, 'src', 'course', 'en', 'contentObjects.json')
    data = read_json_file(path)
    if data is None:
        raise ValueError(f"Vorlagen-contentObjects.json nicht gefunden: {path}")
    return data


def find_template_menu(contentobjects, domain_letter):
    domain_titles = {
        'A': 'A - Verstehen',
        'B': 'B - Anwenden',
        'C': 'C - Bewerten'
    }

    wanted_title = domain_titles[domain_letter]

    for obj in contentobjects:
        if obj.get('_type') == 'menu' and obj.get('title') == wanted_title:
            return obj

    raise ValueError(f"Menü '{wanted_title}' in der Vorlage nicht gefunden.")


def get_course_id_from_template_course_json(course_json):
    if isinstance(course_json, dict):
        return course_json.get('_id')

    if isinstance(course_json, list) and course_json and isinstance(course_json[0], dict):
        return course_json[0].get('_id')

    raise ValueError("Vorlagen-course.json hat ein unerwartetes Format.")


def build_assembled_course_zip(selected_unit_ids):
    units = list(db.contentobjects.find({'_id': {'$in': [ObjectId(i) for i in selected_unit_ids]}}))
    if not units:
        raise ValueError("Keine Lerneinheiten gefunden.")

    if len(units) != len(set(selected_unit_ids)):
        raise ValueError('Mindestens eine gewählte Lerneinheit fehlt.')
    for unit in units:
        course = db.courses.find_one({'_id': unit['_courseId'], '_isShared': True})
        if not course or not re.match(r'^\\d+\\.', course.get('title', '')):
            raise ValueError('Die bisherige Standalone-Kurszusammenstellung unterstützt ausschließlich bestätigte numerische DigComp-Altquellen. Präfixierte oder gemischte Kompetenzrahmen bitte als gemeinsame LXP exportieren.')
        course_assignment(course, legacy_import=True)
    enriched_units = []
    for unit in units:
        taxonomy = get_learning_unit_taxonomy(unit, legacy_import=True)
        domain_letter = get_domain_letter_for_unit(unit)

        if not taxonomy or domain_letter not in ['A', 'B', 'C']:
            raise ValueError('Die gewählte Lerneinheit hat keine unterstützte A/B/C-Domäne; es werden keine Einheiten übersprungen.')

        enriched_units.append({
            'unit': unit,
            'taxonomy': taxonomy,
            'domain_letter': domain_letter,
            'sort_key': parse_taxonomy_string(taxonomy),
            'course_id': str(unit['_courseId']),
            'unit_id': str(unit['_id'])
        })

    if not enriched_units:
        raise ValueError("Keine gültigen Lerneinheiten mit A/B/C-Taxonomie gefunden.")

    enriched_units = sorted(enriched_units, key=lambda x: x['sort_key'])

    tmp_project_dir = extract_adapt_course_template()

    assets_dir = os.path.join(tmp_project_dir, 'src', 'course', 'en', 'assets')
    os.makedirs(assets_dir, exist_ok=True)

    course_json = copy.deepcopy(load_template_course_json(tmp_project_dir))
    base_course_id = get_course_id_from_template_course_json(course_json)
    if not base_course_id:
        raise ValueError("Kurs-ID in der Adapt-Kursvorlage nicht gefunden.")

    template_contentobjects = copy.deepcopy(load_template_content_objects(tmp_project_dir))

    grouped = defaultdict(list)
    for entry in enriched_units:
        grouped[entry['domain_letter']].append(entry)

    used_domain_letters = set(grouped.keys())

    # Nur tatsächlich verwendete Menüs aus der Vorlage übernehmen,
    # bestehende Pages vollständig entfernen.
    new_contentobjects = [
        obj for obj in template_contentobjects
        if (
            obj.get('_type') == 'menu'
            and str(obj.get('title', '')).strip()[:1].upper() in used_domain_letters
        )
    ]

    new_articles = []
    new_blocks = []
    new_components = []
    source_course_ids = sorted({entry['course_id'] for entry in enriched_units})

    for domain_letter in ['A', 'B', 'C']:
        domain_entries = grouped.get(domain_letter, [])
        if not domain_entries:
            continue

        template_menu = find_template_menu(new_contentobjects, domain_letter)
        template_menu_id = template_menu['_id']

        for idx, entry in enumerate(domain_entries, start=1):
            source_course_data = load_built_course_structure(entry['course_id'])
            subtree = extract_learning_unit_subtree(source_course_data, entry['unit_id'])

            old_title = subtree['unit'].get('displayTitle') or subtree['unit'].get('title') or 'Ohne Titel'
            new_taxonomy = f"9.9.{domain_letter}.{idx}"
            new_unit_title = f"{new_taxonomy} {old_title}"

            prefix = f"{domain_letter}-{idx}-{entry['unit_id']}"

            new_page, unit_articles, unit_blocks, unit_components = remap_subtree_ids(
                subtree=subtree,
                new_menu_id=template_menu_id,
                new_unit_title=new_unit_title,
                prefix=prefix,
                base_course_id=base_course_id
            )

            if idx == 1:
                copy_graphic_to_menu(template_menu, new_page)

            new_contentobjects.append(new_page)
            new_articles.extend(unit_articles)
            new_blocks.extend(unit_blocks)
            new_components.extend(unit_components)

    used_asset_paths = collect_course_asset_paths(
        course_json,
        new_contentobjects,
        new_articles,
        new_blocks,
        new_components
    )

    write_assembled_course_jsons(
        tmp_project_dir=tmp_project_dir,
        contentobjects=new_contentobjects,
        articles=new_articles,
        blocks=new_blocks,
        components=new_components
    )

    copy_used_assets_to_template(
        asset_paths=used_asset_paths,
        source_course_ids=source_course_ids,
        tmp_project_dir=tmp_project_dir
    )

    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zip_file:
        add_project_files_to_zip(zip_file, tmp_project_dir)

    zip_buffer.seek(0)
    shutil.rmtree(tmp_project_dir)

    return zip_buffer


class BasicForm(form.Form):
    title = fields.StringField('Title')

class MyAdminIndexView(AdminIndexView):
    @expose('/')
    @login_required
    def index(self):
        return super(MyAdminIndexView, self).index()

class MyModelView(ModelView):
    # set page_size = 10000 for production use
    page_size = 1000
    column_default_sort = ('framework', False)

    form = BasicForm

    def is_accessible(self):
        return current_user.is_authenticated

    def get_query(self):
        # Fetch IDs of shared courses
        shared_course_ids = [ObjectId(str(course['_id'])) for course in db.courses.find({'_isShared': True})]
        if hasattr(self, 'get_init_query') and self.get_init_query:
            return {'$and': [self.get_init_query(), {"_courseId": {"$in": shared_course_ids}}]}
        else:
            return {"_courseId": {"$in": shared_course_ids}}

    def get_list(self, page, sort_column, sort_desc, search, filters,
                 execute=True, page_size=None):
        query = self.get_query()

        # forked code
        ########################################################################
        # Filters
        if self._filters and filters:
            data = []

            for flt, flt_name, value in filters:
                f = self._filters[flt]
                data = f.apply(data, f.clean(value))

            if data:
                query = {'$and': [query] + data}

        # Search
        if self._search_supported and search:
            query = self._search(query, search)

        # Get count
        count = self.coll.count_documents(query) if not self.simple_list_pager else None

        # Sorting
        sort_by = None

        if sort_column:
            sort_by = [(sort_column, pymongo.DESCENDING if sort_desc else pymongo.ASCENDING)]
        else:
            order = self._get_default_order()

            if order:
                sort_by = [(col, pymongo.DESCENDING if desc else pymongo.ASCENDING)
                           for (col, desc) in order]

        # Pagination
        if page_size is None:
            page_size = self.page_size

        skip = 0

        if page and page_size:
            skip = page * page_size

        # Derived addresses must be sorted numerically before pagination. A
        # list (not a taxonomy-keyed dict) preserves identical display rows.
        results = list(self.coll.find(query, sort=sort_by))
        for model in results:
            add_assignment_columns(model)
        if not sort_column or sort_column in ('taxonomy', 'framework', 'framework_version', 'module_number', 'course_number'):
            results.sort(key=assignment_sort_key, reverse=bool(sort_desc))
        results = results[skip:skip + page_size] if page_size else results[skip:]
        #########################################################################

        return count, results

    can_delete = False
    can_edit = False
    can_create = False

    column_type_formatters = dict(typefmt.BASE_FORMATTERS)
    column_type_formatters[datetime.date] = lambda view, value: format_date(value)

    @expose("/download", methods=("GET",))
    def download_view(self):
        course_id = request.args.get('id')
        if not course_id:
            flash("Keine Kurs-ID übergeben.", "error")
            return redirect(url_for('coursesview.index_view'))

        build_dir = os.path.join(app.config['BUILDS_DIR'], course_id, 'build')

        if not os.path.isdir(build_dir):
            flash("Für diesen Kurs wurde noch kein veröffentlichter Build gefunden.", "warning")
            return redirect(url_for('coursesview.index_view'))

        zip_buffer = BytesIO()

        try:
            with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zip_file:
                add_files_to_zip(zip_file, build_dir)

            zip_buffer.seek(0)
            return send_file(zip_buffer, as_attachment=True, download_name=f"{course_id}.zip")

        except Exception as e:
            flash(f"Download fehlgeschlagen: {e}", "error")
            return redirect(url_for('coursesview.index_view'))


class Meta(MyModelView.__class__, type):
    def __init__(cls, name, bases, attrs):
        model_json = f'./columns/{name}.json'
        if os.path.exists(model_json):
            with open(model_json, 'r') as f:
                column_data = json.load(f)
                setattr(cls, 'column_list', column_data.keys())
                setattr(cls, 'column_sortable_list', column_data.keys())
                setattr(cls, 'column_labels', column_data)

        super().__init__(name, bases, attrs)

class CourseView(MyModelView, metaclass=Meta):
    list_template = 'admin/courses_list.html'
    column_filters = [
        CustomFilter(column="tags", name="Tags",
            options=lambda: [(str(tag['_id']), tag['title']) for tag in db.tags.find()])
    ] + [AssignmentFilter(column=key, name=label, courses=lambda: db.courses.find({'_isShared': True}),
                         normalize=course_assignment, course_field='_id')
         for key, label in [('prefix', 'Kompetenzrahmen'), ('version', 'Rahmenfassung'), ('moduleNumber', 'Modul'), ('courseNumber', 'Kursnummer')]]

    def download_formatter(view, context, model, name):
        course_id = str(model['_id'])
        build_dir = os.path.join(app.config['BUILDS_DIR'], course_id, 'build')

        if os.path.isdir(build_dir):
            url = url_for('.download_view', id=course_id)
            return Markup(f'<a href="{url}" title="Download"><i class="fa fa-download"></i></a>')

        return Markup('<span style="color:#999;" title="Noch nicht veröffentlicht">—</span>')

    column_formatters = {
        'download': download_formatter
    }

    @action('batch_download', 'Kurse herunterladen', 'Ihr Download beginnt nach der Bestätigung. Laden Sie die Kurse in den Kurskonfigurator und schnüren Sie ein individuelles Lernpaket!')
    def action_batch_download(self, ids):
        try:
            courses = [os.path.join(app.config['BUILDS_DIR'], course) for course in ids]

            # Create the ZIP archive
            zip_buffer = BytesIO()
            with ZipFile(zip_buffer, 'w') as zip_file:
                for course in courses:
                    shutil.make_archive(course, "zip", course)
                    zip_file.write(course+".zip")

            zip_buffer.seek(0)

            return send_file(zip_buffer, as_attachment=True, download_name='batch_download.zip')

        except Exception as ex:
            flash('Failed to batch download files. {}'.format(str(ex)), 'error')

    @action('package_download', 'Gemeinsame LXP vorbereiten (DigComp / AILit)')
    def package_download(self, ids):
        return redirect(url_for('lxp_export_setup', course_id=ids))

    def get_tags(self, tags):
        query = {"_id": {"$in": tags}}
        tags = db.tags.find(query)

        return [tag['title'].lower() for tag in tags]

    def get_list(self, *args, **kwargs):
        count, data = super(CourseView, self).get_list(*args, **kwargs)

        for course in data:
            tags = self.get_tags(course.get('tags', []))
            course['tags'] = tags

        return count, data

    def get_query(self):
        # Fetch IDs of shared courses
        shared_course_ids = [ObjectId(str(course['_id'])) for course in db.courses.find({'_isShared': True})]

        if hasattr(self, 'init_query') and self.init_query:
            return {'$and': [copy.deepcopy(self.init_query), {"_id": {"$in": shared_course_ids}}]}
        else:
            return {"_id": {"$in": shared_course_ids}}

class ContentsView(MyModelView, metaclass=Meta):
    column_filters = [AssignmentFilter(column=key, name=label, courses=lambda: db.courses.find({'_isShared': True}),
                                      normalize=course_assignment)
         for key, label in [('prefix', 'Kompetenzrahmen'), ('version', 'Rahmenfassung'), ('moduleNumber', 'Modul'), ('courseNumber', 'Kursnummer')]]
    column_extra_row_actions = [
        template.EndpointLinkRowAction("fa fa-print", ".preview")
    ]

    def get_init_query(self):
        return {'_type': 'page'}

    def get_query(self):
        shared_course_ids = [course['_id'] for course in db.courses.find({'_isShared': True}, {"_id": 1})]
        query = self.get_init_query()
        query["_courseId"] = {"$in": shared_course_ids}
        return query

    def get_course(self, _id):
        query = {"_id": _id}
        course = db.courses.find(query)[0]['displayTitle']

        return course

    def get_taxonomy_for_id(self, model):
        """
        This method will execute tasks the taxonomy_formatter did before.
        :param model:
        :return: (taxonomy, is_abbrev_valid)
        """
        try:
            return get_learning_unit_taxonomy(model) or 'Nicht zugeordnet', True
        except (ValueError, OSError):
            return 'Nicht zugeordnet – technischen Kurstitel prüfen', False


    def get_list(self, *args, **kwargs):
        """
        Overwrite method to handele sorting over taxonomies.
        :param args:
        :param kwargs:
        :return: (count, results)
        """
        count, results = super(ContentsView, self).get_list(*args, **kwargs)

        for r in results:
            taxonomy, abbrev_valid = self.get_taxonomy_for_id(r)
            r['taxonomy_string'] = taxonomy
            r['abbrev_valid'] = abbrev_valid
        return count, results

    @action('export_unit_in_course', 'Lerneinheit mit Kurs exportieren', 'Der Kurs wird mit Startpunkt auf die ausgewählte Lerneinheit exportiert.')
    def export_unit_with_course_context(self, ids):
        def get_zip_for_contentobject(id):
            unit_id = ObjectId(id)
            unit = db.contentobjects.find_one({'_id': unit_id})
            course_id = unit['_courseId']
            course_dir = os.path.join(app.config['BUILDS_DIR'], str(course_id), 'build')

            if not os.path.exists(course_dir):
                flash("Build-Verzeichnis nicht gefunden.", "error")
                return

            # 1. Load course.json
            course_json_path = os.path.join(course_dir, 'course', 'en', 'course.json')
            with open(course_json_path, 'r', encoding='utf-8') as f:
                course_data = json.load(f)

            # 2. Inject _start section
            course_data['_start'] = {
                "_isEnabled": True,
                "_startIds": [{"_id": str(unit_id)}],
                "_force": True,
                "_isMenuDisabled": True
            }

            # 3. Write modified course.json into a temporary copy of the build
            tmp_build_dir = tempfile.mkdtemp()
            shutil.copytree(course_dir, tmp_build_dir, dirs_exist_ok=True)

            with open(os.path.join(tmp_build_dir, 'course', 'en', 'course.json'), 'w', encoding='utf-8') as f:
                json.dump(course_data, f, indent=2, ensure_ascii=False)

            # 4. Zip the build folder
            zip_buffer = BytesIO()

            with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zip_file:
                add_files_to_zip(zip_file, tmp_build_dir)

            shutil.rmtree(tmp_build_dir)
            zip_buffer.seek(0)

            return (zip_buffer, f"{unit['title']}_kurs_export.zip")

        try:
            if len(ids) < 1:
                flash("Bitte mindestens eine Lerneinheit auswählen.", "error")
                return
            if len(ids) == 1:
                zip_buffer, download_name = get_zip_for_contentobject((ids[0]))
                return send_file(zip_buffer, as_attachment=True, download_name=download_name)

            if len(ids) > 1:
                dir_of_zips = tempfile.mkdtemp()

                for n, id in enumerate(ids):
                    zip_buffer, download_name = get_zip_for_contentobject(id)

                    with open(os.path.join(dir_of_zips, f'{n:02d}_' + download_name), 'wb') as zipfile:
                        zipfile.write(zip_buffer.getbuffer())

                zip_buffer = BytesIO()

                with ZipFile(zip_buffer, 'w', ZIP_DEFLATED) as zip_file:
                    add_zip_to_zip(zip_file, dir_of_zips, flat=True)

                zip_buffer.seek(0)
                shutil.rmtree(dir_of_zips)

                return send_file(zip_buffer, as_attachment=True, download_name='lerneinheiten.zip')

        except Exception as e:
                flash(f"Export fehlgeschlagen: {e}", "error")

    @action('assemble_new_course', 'Als neuen Kurs zusammenstellen', 'Die ausgewählten Lerneinheiten werden zu einem neuen Kurs zusammengeführt und als ZIP exportiert.')
    def assemble_new_course(self, ids):
        try:
            if not ids:
                flash("Bitte mindestens eine Lerneinheit auswählen.", "error")
                return

            zip_buffer = build_assembled_course_zip(ids)
            return send_file(
                zip_buffer,
                as_attachment=True,
                download_name='9.9_zusammengestellter_kurs.zip'
            )

        except Exception as e:
            flash(f"Zusammenstellen fehlgeschlagen: {e}", "error")

    @expose("/preview", methods=("GET",))
    def preview(*args, **kwargs):
        def get_learning_unit_from_db(unit_id):
            return db.contentobjects.find_one({'_id': ObjectId(unit_id)})

        def get_articles_for_unit(unit_id):
            return list(db.articles.find({'_parentId': unit_id}))

        def construct_panel_body(main_article):
            body_content = []

            # Add main article content
            body_content.append("<div>")
            #body_content.append(f"<h1>{main_article.get('displayTitle', '')}</h1>")
            #body_content.append(f"<p>{main_article.get('body', '')}</p>")

            # Add blocks and their components
            blocks = db.blocks.find({'_parentId': main_article['_id']}).sort([("_sortOrder", 1)])
            component_formatter = ComponentFormatter(db)

            for block in blocks:
                body_content.append("<div>")

                if block.get('displayTitle', False):
                    body_content.append(f"<h2>{block.get('displayTitle', '')}</h2>")

                body_content.append(f"<p>{block.get('body', '')}")

                components = db.components.find({"_parentId": block["_id"]})
                for component in components:
                    formatted_question = component_formatter.question_formatter(None, None, component, None)
                    formatted_answer = component_formatter.answer_formatter(None, None, component, None)
                    body_content.append("<div class='qa-container'>")
                    if formatted_question != '':
                        body_content.append(f"<div class='question'>{formatted_question}</div>")
                    if formatted_answer != '':
                        body_content.append(f"<div class='answer'>{formatted_answer}</div>")
                    body_content.append("</div>")

                body_content.append("</p></div>")

            return "".join(body_content)

        # 1. Extract the learning unit
        learning_unit = get_learning_unit_from_db(request.args['id'])

        # 2. Initialize panels
        panels = [{"title": Markup(f'<h1>{learning_unit["displayTitle"]}</h1>'), "body": ""}]

        # 3. Fetch articles
        articles = get_articles_for_unit(learning_unit['_id'])
        main_article = articles[0]
        additional_content = None

        if len(articles) > 1:
            additional_content = articles[1]

        # 5. Construct the body of the main panel
        panels[0]["body"] = construct_panel_body(main_article)
        panels[0]["body"] += "<h1>Zusatzfragen</h1>"

        if additional_content:
            panels[0]["body"] += construct_panel_body(additional_content)

        # 6. Convert to Markup
        panels[0]["body"] = Markup(panels[0]["body"])
        """unique_parent_ids = list(set([component['_parentId'] for component in components]))

        for parent_id in unique_parent_ids:
            nested_panel = fetch_and_nest_content(parent_id, components, db)
            if nested_panel:
                panels.append(nested_panel)"""

        return render_template("handout.html", panels=panels, handout_title=learning_unit["displayTitle"])

    def taxonomy_formatter(view, context, model, name):
        label = model.get('taxonomy_string', 'Nicht zugeordnet')
        return Markup('<a href="{}">{}</a>').format(get_editor_url(model['_courseId'], model['_id']), label)

    def id_formatter(view, context, model, name):
        # Fetch the block that the model belongs to
        model_id = model['_id']

        return model_id

    column_formatters = {
        'taxonomy': taxonomy_formatter,
    '_id': id_formatter
    }

class ComponentView(MyModelView, metaclass=Meta):
    column_filters = [AssignmentFilter(column=key, name=label, courses=lambda: db.courses.find({'_isShared': True}),
                                      normalize=course_assignment)
         for key, label in [('prefix', 'Kompetenzrahmen'), ('version', 'Rahmenfassung'), ('moduleNumber', 'Modul'), ('courseNumber', 'Kursnummer')]]
    component_formatter = ComponentFormatter(db)
    question_formatter = component_formatter.question_formatter
    answer_formatter = component_formatter.answer_formatter

    def entry_formatter(view, context, model, name):
        classes = model.get('_classes', '')
        if classes == '':
            return Markup('<span style="color: red;">?</span>')
        classes = classes.split(' ')
        return 'et' in classes

    def final_formatter(view, context, model, name):
        classes = model.get('_classes', '')
        if classes == '':
            return Markup('<span style="color: red;">?</span>')
        classes = classes.split(' ')
        return 'at' in classes

    def fact_formatter(view, context, model, name):
        classes = model.get('_classes', '')
        if ',' in classes:
            return Markup(f'<span style="color: red;">{classes + " → "}</span>')
        if classes == '':
            return Markup('<span style="color: red;">?</span>')
        classes = classes.split(' ')
        return 'facts' in classes

    column_formatters = {
        'instruction': question_formatter,
        'options': answer_formatter,
        'entry': entry_formatter,
        'final': final_formatter,
        'fact': fact_formatter
    }

    @action('handout', 'Handout erstellen', 'Ihr Download beginnt nach der Bestätigung.')
    def handout(self, ids):
        try:
            # Assuming `ids` is a list of strings or ObjectId and `db` is a database connection object
            ids = [ObjectId(i) for i in ids]
            components = list(db.components.find({'_id': {'$in': ids}}))
            panels = []
            unique_parent_ids = list(set([component['_parentId'] for component in components]))

            for parent_id in unique_parent_ids:
                nested_panel = fetch_and_nest_content(parent_id, components, db)
                if nested_panel:
                    panels.append(nested_panel)

            return render_template("handout.html", panels=panels)

        except Exception as ex:
            raise Exception(ex)

    def get_list(self, *args, **kwargs):
        count, results = super(ComponentView, self).get_list(*args, **kwargs)
        for component in results:
            block = db.blocks.find_one({'_id': component.get('_parentId')}) or {}
            article = db.articles.find_one({'_id': block.get('_parentId')}) or {}
            unit = db.contentobjects.find_one({'_id': article.get('_parentId')})
            component['additional'] = bool(get_related_content_index(article)) if article else False
            component['taxonomy'] = 'Nicht zugeordnet'
            component['domain'] = '—'
            if unit:
                try:
                    label = get_learning_unit_taxonomy(unit) or 'Nicht zugeordnet'
                    component['taxonomy'] = Markup('<a href="{}">{}</a>').format(get_editor_url(unit['_courseId'], unit['_id']), label)
                    domain = db.contentobjects.find_one({'_id': unit.get('_parentId')})
                    component['domain'] = (domain or {}).get('title', 'Flache Kursstruktur – keine didaktische Domäne abgeleitet')
                except (ValueError, OSError):
                    pass
        return count, results

class QuestionView(ComponentView, metaclass=Meta):
    def get_init_query(self=None):
        return {'_component': { '$in': ['mcq', 'matching', 'slider', 'confidenceSlider', 'textinput', 'openTextInput', 'dragndrop', 'infai-dragndrop']}}

    init_query = get_init_query()

    component_types = db.components.find(init_query).distinct('_component')
    classes_distinct = db.components.find(init_query).distinct('_classes')

    column_filters = [
        CustomFilter(column="_component", name="Typ",
                    options=[(str(component), component) for component in component_types]),
        CustomClassesFilter(column="entry", name="Klassifizierung",
                    options=[("et", "Einstieg"), ("at", "Abschluss"), ("facts", "Fakt"), ("self-assessment", "Meinung")])
    ] + ComponentView.column_filters

    # Explicitly remove "Handout erstellen" by overriding and disabling the method
    def handout(self, ids):
        abort(404)

    @action('batch_download', 'Quiz zusammenstellen', 'Ihr Download beginnt nach der Bestätigung. Danach können Sie das Lernpaket direkt in ein SCORM 1.2 kompatibles LMS laden!')
    def action_batch_download(self, ids):
        try:
            quiz_folder = compose_quiz(ids)
            print(f"Quiz folder created at: {quiz_folder}")  # Debugging line

            zip_buffer = BytesIO()
            with ZipFile(zip_buffer, 'w') as zip_file:
                print(f"Files to be added: {os.listdir(quiz_folder)}")  # Debugging line
                add_files_to_zip(zip_file, quiz_folder)

            shutil.rmtree(quiz_folder)

            zip_buffer.seek(0)
            return send_file(zip_buffer, as_attachment=True, download_name='quiz.zip')

        except Exception as ex:
            flash(f'Failed to batch download quiz files. {str(ex)}', 'error')
            print(f"Error: {str(ex)}")  # Print error for debugging

class PresentationView(ComponentView):
    # TODO: Als Anki-Deck exportieren
    def get_init_query(self=None):
        return {
        '_component': {
            '$in': ['accordion', 'narrative', 'text']
        },
        'title': {
            '$not': re.compile('^Quellen')  # Using Python's re module to compile a regex pattern
        }
    }

    init_query = get_init_query()

    component_types = db.components.find(init_query).distinct('_component')
    column_filters = [
        CustomFilter(column="_component", name="Typ",
                    options=[(str(component), component) for component in component_types])
    ] + ComponentView.column_filters

class GraphicView(ComponentView):
    def get_init_query(self=None):
        return {'_component': { '$in': ['graphic', 'hotgraphic']}}

    init_query = get_init_query()

    component_types = db.components.find(init_query).distinct('_component')
    column_filters = [
        CustomFilter(column="_component", name="Typ",
                    options=[(str(component), component) for component in component_types])
    ] + ComponentView.column_filters

class GlossaryForm(BaseForm):
    term = fields.StringField('Term', validators=[validators.DataRequired()])
    definition = fields.TextAreaField('Definition', validators=[validators.DataRequired()])

# Custom model view for JSON data
class GlossaryView(BaseModelView):
    column_list = ('term', 'definition')
    form = GlossaryForm

    def get_pk_value(self, model):
        return model['id']

    def scaffold_list_columns(self):
        return ['term', 'definition']

    def scaffold_sortable_columns(self):
        return ['term', 'definition']

    def init_search(self):
        return False

    def _get_list_value(self, context, model, name, column_formatters,
                        column_type_formatters):
        return model.get(name, '')

    def get_list(self, page, sort_field, sort_desc, search, filters, page_size=20):
        data = load_glossary()
        entries = list(data.values())
        # Add custom sorting and pagination here if needed
        return len(entries), entries

    def get_one(self, id):
        data = load_glossary()
        return data.get(id)

    def create_model(self, form):
        data = load_glossary()
        model_id = str(uuid.uuid4())
        model = {
            'id': model_id,
            'term': form.term.data,
            'definition': form.definition.data
        }
        data[model_id] = model
        save_glossary(data)
        return True

    def update_model(self, form, model):
        data = load_glossary()
        model['term'] = form.term.data
        model['definition'] = form.definition.data
        data[model['id']] = model
        save_glossary(data)
        return True

    def edit_form(self, obj=None):
        form = self.form()
        if request.method == 'POST':
            form = self.form(request.form)
            if form.validate():
                return form
        elif request.method == 'GET':
            form.term.data = obj['term']
            form.definition.data = obj['definition']
        return form

    def delete_model(self, model):
        data = load_glossary()
        data.pop(model['id'], None)
        save_glossary(data)
        return True

    @action('delete', 'Delete', 'Are you sure you want to delete selected terms?')
    def action_delete(self, ids):
        try:
            data = load_glossary()
            for model_id in ids:
                data.pop(model_id, None)
            save_glossary(data)
            flash('Term(s) were successfully deleted.')
        except Exception as e:
            flash('Failed to delete term(s): ' + str(e), 'error')


admin = Admin(app, name='ADAPT|OER', index_view=MyAdminIndexView(url='/admin'), template_mode='bootstrap4')
admin.add_view(CourseView(db['courses'], 'Kurse'))
admin.add_view(ContentsView(db['contentobjects'], 'Lerneinheiten'))
admin.add_view(QuestionView(db['components'], 'Fragen', endpoint='questionsview'))
admin.add_view(PresentationView(db['components'], 'Texte', endpoint='presentationview'))
admin.add_view(GraphicView(db['components'], 'Bilder', endpoint='pictureview'))
admin.add_view(GlossaryView(load_glossary(), name="Glossar", endpoint='glossary'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
