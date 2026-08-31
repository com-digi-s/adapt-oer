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

    if not parent_content_object:
        return ''

    # Fetch all related content objects that share the same parent
    parent_id = parent_content_object['_id']
    related_content_objects = db[collection].find({"_parentId": parent_id})
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
                    course_title = db.courses.find_one({'_id': domain['_parentId']})['title']

                    result = int(course_title[:3].replace('.', '') + str(domain['_sortOrder']))

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

    except Exception as e:
        flash(f"An error occurred: {e}")

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


def get_learning_unit_taxonomy(unit):
    """
    Erzeugt die bestehende Taxonomie einer Lerneinheit im Format X.Y.A.N
    auf Basis der vorhandenen Logik in app.py.
    """
    raw_index = get_related_content_index(unit, 'contentobjects')
    if not isinstance(raw_index, int):
        return None

    learning_unit = str(raw_index + 1)
    course = db.courses.find_one({'_id': unit['_courseId']})
    domain = db.contentobjects.find_one({'_id': unit['_parentId']})

    if not course or not domain:
        return None

    course_prefix_match = re.match(r"^(\d+)\.(\d+)", str(course.get('title', '')))
    if not course_prefix_match:
        return None

    course_prefix = f"{course_prefix_match.group(1)}.{course_prefix_match.group(2)}"
    domain_abbrev = str(domain.get('title', '')).strip()[:1].upper()

    return f"{course_prefix}.{domain_abbrev}.{learning_unit}"


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


def build_index(items):
    return {item['_id']: item for item in items}


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


def create_domain_contentobject(domain_letter, sort_order=0):
    domain_titles = {
        'A': 'A - Verstehen',
        'B': 'B - Anwenden',
        'C': 'C - Bewerten'
    }

    return {
        "_type": "menu",
        "_id": make_24hex("menu", domain_letter),
        "_parentId": "course",
        "title": domain_titles[domain_letter],
        "displayTitle": domain_titles[domain_letter]
    }


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


def find_template_menu_id(contentobjects, domain_letter):
    domain_titles = {
        'A': 'A - Verstehen',
        'B': 'B - Anwenden',
        'C': 'C - Bewerten'
    }
    wanted_title = domain_titles[domain_letter]

    for obj in contentobjects:
        if obj.get('_type') == 'menu' and obj.get('title') == wanted_title:
            return obj.get('_id')

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

    enriched_units = []
    for unit in units:
        taxonomy = get_learning_unit_taxonomy(unit)
        domain_letter = get_domain_letter_for_unit(unit)

        if not taxonomy or domain_letter not in ['A', 'B', 'C']:
            continue

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

    # Nur Menüs aus der Vorlage behalten, bestehende Pages entfernen
    new_contentobjects = [
        obj for obj in template_contentobjects
        if obj.get('_type') == 'menu'
    ]

    grouped = defaultdict(list)
    for entry in enriched_units:
        grouped[entry['domain_letter']].append(entry)

    new_articles = []
    new_blocks = []
    new_components = []
    source_course_ids = sorted({entry['course_id'] for entry in enriched_units})

    for domain_letter in ['A', 'B', 'C']:
        domain_entries = grouped.get(domain_letter, [])
        if not domain_entries:
            continue

        template_menu_id = find_template_menu_id(template_contentobjects, domain_letter)

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
    column_default_sort = ('updatedAt', True)

    form = BasicForm

    def is_accessible(self):
        return current_user.is_authenticated

    def get_query(self):
        # Fetch IDs of shared courses
        shared_course_ids = [ObjectId(str(course['_id'])) for course in db.courses.find({'_isShared': True})]
        if hasattr(self, 'get_init_query') and self.get_init_query:
            self.init_query = self.get_init_query()
            self.init_query["_courseId"] = {"$in": shared_course_ids}
            return self.init_query
        else:
            return {"_courseId": {"$in": shared_course_ids}}

    def get_list(self, page, sort_column, sort_desc, search, filters,
                 execute=True, page_size=None):
        query = self.get_query()

        # forked code
        ########################################################################
        # Filters
        if self._filters:
            data = []

            for flt, flt_name, value in filters:
                f = self._filters[flt]
                data = f.apply(data, f.clean(value))

            if data:
                if len(data) == 1:
                    query = data[0]
                else:
                    query['$and'] = data

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

        results = self.coll.find(query, sort=sort_by, skip=skip, limit=page_size)

        if execute:
            results = list(results)
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
            return redirect(url_for('courseview.index_view'))


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
    column_filters = [
        CustomFilter(column="tags", name="Tags",
            options=lambda: [(str(tag['_id']), tag['title']) for tag in db.tags.find()])
    ]

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

    @action('package_download', 'Kurse als Lernpaket herunterladen (Adapt LXP)', 'Ihr Download beginnt nach der Bestätigung. Sie können das Lernpaket direkt in das LMS Ihrer Wahl laden!')
    def package_download(self, ids):
        def extract_course_id(title):
            match = re.search(r"^\d\.\d", title)
            return f"M{match.group(0).replace('.', '/')}" if match else title

        def download_and_overwrite_zip_with_dist(wrapper_zip_path):
            # GitHub URL for downloading the entire repo as a ZIP file
            repo_url = "https://github.com/com-digi-s/adapt-lxp/archive/refs/heads/main.zip"

            # Send request to download the repository as a ZIP file
            response = requests.get(repo_url, stream=True)

            if response.status_code == 200:
                # Save the ZIP file temporarily
                temp_zip_path = "repo_main.zip"
                with open(temp_zip_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)

                # Extract the ZIP file into a temporary directory
                with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall("repo_extracted")

                    # Define the path of the 'dist' folder in the extracted repository
                    dist_folder_path = os.path.join("repo_extracted", "adapt-lxp-main", "dist")

                    if os.path.exists(dist_folder_path):
                        # Create a new empty ZIP file (this will overwrite the existing ZIP)
                        with zipfile.ZipFile(wrapper_zip_path, 'w') as wrapper_zip:
                            # Walk through the 'dist' folder and add both files and directories
                            for root, dirs, files in os.walk(dist_folder_path):
                                for dir_name in dirs:
                                    # Create the directory structure in the ZIP
                                    dir_path = os.path.join(root, dir_name)
                                    arcname = os.path.relpath(dir_path, dist_folder_path)
                                    wrapper_zip.write(dir_path, arcname)
                                    print(f"Added directory to ZIP: {arcname}")
                                for file_name in files:
                                    # Add files to the ZIP
                                    file_path = os.path.join(root, file_name)
                                    arcname = os.path.relpath(file_path, dist_folder_path)
                                    wrapper_zip.write(file_path, arcname)
                                    print(f"Added file to ZIP: {arcname}")
                    else:
                        print("The 'dist' folder was not found in the repository.")

                # Clean up the temporary files
                os.remove(temp_zip_path)
                shutil.rmtree("repo_extracted")

            else:
                    print(f"Failed to download the repository. Status code: {response.status_code}")

        def delete_descendants(data, parent_id, hierarchy):
            if not hierarchy:
                return data

            current_key = hierarchy[0]
            child_hierarchy = hierarchy[1:]

            if current_key in data:
                new_list = []
                for obj in data[current_key]:
                    if obj.get('_parentId') == parent_id:
                        data = delete_descendants(data, obj['_id'], child_hierarchy)
                    else:
                        new_list.append(obj)
                data[current_key] = new_list

            return data

        def find_and_delete_root_object(data):
            for obj in data.get('contentObjects', []):
                if obj["title"] == "Adapt|OER Komponenten":
                    root_id = obj["_id"]
                    data['contentObjects'] = [o for o in data['contentObjects'] if o["_id"] != root_id]
                    hierarchy = ['articles', 'blocks', 'components']
                    data = delete_descendants(data, root_id, hierarchy)
                    break
            return data

        wrapper_dir = "static/adapt-lxp-latest.zip"
        download_and_overwrite_zip_with_dist(wrapper_dir)
        courses = [os.path.join(app.config['BUILDS_DIR'], course, 'build') for course in ids]
        ids_for_et, ids_for_at = [], []
        zip_buffer = BytesIO()

        with ZipFile(zip_buffer, 'w') as zip_file:
            curr_ids = []
            components = []
            terms_in_co = []

            for course in courses:
                course_name = json.load(open(os.path.join(course, 'course/en/course.json'))).get('title')
                curr_ids.append(extract_course_id(course_name))

                file_names = ['contentObjects', 'articles', 'blocks', 'components']
                course_directory = 'course/en'

                preloaded_data = {}
                for file_name in file_names:
                    json_path = os.path.join(course, course_directory, f'{file_name}.json')
                    file_data = read_json_file(json_path)
                    preloaded_data[file_name] = file_data

                preloaded_data = find_and_delete_root_object(preloaded_data)

                for file_name in file_names:
                    items = preloaded_data[file_name]
                    json_path = os.path.join(course, course_directory, f'{file_name}.json')

                    modified_items = []
                    for item in items:
                        item["body"], found_terms = annotate_terms(item["body"])

                        classes = item['_classes'].split(' ')
                        if 'et' in classes:
                            ids_for_et.append(item['_id'])
                        if 'at' in classes:
                            ids_for_at.append(item['_id'])

                        if found_terms and found_terms != [None]:
                            if file_name == "components":
                                block = next((i for i in preloaded_data['blocks'] if i["_id"] == item["_parentId"]), None)
                            else:
                                block = item
                            terms_in_co.append(process_content_objects(course, course_name, course_directory, block, found_terms))

                        modified_items.append(item)

                    with open(json_path, 'w', encoding='utf-8') as file:
                        json.dump(modified_items, file, indent=4)

            prepare_quizzes("M0", ids_for_et, zip_file)
            prepare_quizzes("MX", ids_for_at, zip_file)
            curr_ids.append("M0")
            curr_ids.append("MX")

            for course_dir, curr_id in zip(courses, curr_ids):
                components_path = os.path.join(course_dir, 'course/en/components.json')

                with open(ADDITIONAL_COMPONENTS, 'r', encoding="utf-8") as f:
                    new_component_template = json.loads(f.read())['pageNav']

                with open(components_path, 'r', encoding="utf-8") as f:
                    data = json.load(f)

            # Verarbeitung der transpiled_components
                data = process_transpiled_components(data)

            # Verarbeitung der Quellen-Komponenten
                data = process_quellen_components(data, new_component_template)

                with open(components_path, 'w', encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                add_files_to_zip(zip_file, course_dir, f"scos/{curr_id}")

            with tempfile.TemporaryDirectory() as tmpdirname:
                ZipFile(wrapper_dir, 'r').extractall(tmpdirname)
                lxp_config = json.load(open(os.path.join(tmpdirname, 'config.json')))
                enabled_modules = set()
                enabled_courses = set()

                for curr_id in curr_ids:
                    mod, cur = curr_id.split('/') if ("/" in curr_id) else (curr_id, "-")

                    for key, value in lxp_config["SCOS"].items():
                        if key == mod:
                            value["disabled"] = False
                            enabled_modules.add(key)
                        elif key not in enabled_modules:
                            value["disabled"] = True

                        if value.get("courses"):
                            for idx, course in enumerate(value["courses"]):
                                course_key = f"{key}/{course['id']}"
                                if key == mod and course["id"] == cur:
                                    value["courses"][idx]["disabled"] = False
                                    enabled_courses.add(course_key)
                                elif course_key not in enabled_courses:
                                    value["courses"][idx]["disabled"] = True

                """
        lxp_config['glossary'] = list(load_glossary().values())
                if terms_in_co:
                    for idx, entry in enumerate(lxp_config['glossary']):
                        lxp_config['glossary'][idx]["in"] = retrieve_content_objects_by_term(terms_in_co, entry["term"])
        """

                json.dump(lxp_config, open(os.path.join(tmpdirname, 'config.json'), "w"))
                add_files_to_zip(zip_file, tmpdirname)

        zip_buffer.seek(0)
        return send_file(zip_buffer, as_attachment=True, download_name='scorm_package.zip')

    def get_tags(self, tags):
        query = {"_id": {"$in": tags}}
        tags = db.tags.find(query)

        return [tag['title'].lower() for tag in tags]

    def get_list(self, *args, **kwargs):
        count, data = super(CourseView, self).get_list(*args, **kwargs)

        for course in data:
            tags = self.get_tags(course['tags'])
            course['tags'] = tags

        return count, data

    def get_query(self):
        # Fetch IDs of shared courses
        shared_course_ids = [ObjectId(str(course['_id'])) for course in db.courses.find({'_isShared': True})]

        if hasattr(self, 'init_query') and self.init_query:
            self.init_query["_id"] = {"$in": shared_course_ids}
            return self.init_query
        else:
            return {"_id": {"$in": shared_course_ids}}

class ContentsView(MyModelView, metaclass=Meta):
    column_extra_row_actions = [
        template.EndpointLinkRowAction("fa fa-print", ".preview")
    ]

    def get_init_query(self):
        all_ids = [doc['_id'] for doc in db.contentobjects.find({}, {"_id": 1})]
        return {"_parentId": {"$in": all_ids}}

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
        is_abbrev_valid = False

        # Check if parent content object exists
        raw_index = get_related_content_index(model, 'contentobjects')

        if type(raw_index) == int:
            learning_unit = str(raw_index + 1)
            course_id = model['_courseId']

            # Fetch the domain information
            domain_title = str(db.contentobjects.find_one({'_id': model['_parentId']}).get("title"))
            domain_abbrev_str = domain_title[0]

            # If domain abbreviation is not A, B, or C, color it red
            if domain_abbrev_str in ['A', 'B', 'C']:
                is_abbrev_valid = True

            # Construct taxonomy with course title, domain abbreviation, and learning unit
            taxonomy = db.courses.find_one({'_id': course_id})['title'][:3] + '.' + domain_abbrev_str + '.' + learning_unit
            return taxonomy, is_abbrev_valid
        else:
            return raw_index, is_abbrev_valid


    def get_list(self, *args, **kwargs):
        """
        Overwrite method to handele sorting over taxonomies.
        :param args:
        :param kwargs:
        :return: (count, results)
        """
        count, results = super(ContentsView, self).get_list(*args, **kwargs)

        sort_dict = {} # helper dictionary

        for r in results: # cursor
            taxonomy, abbrev_valid = self.get_taxonomy_for_id(r)
            r['taxonomy_string'] = taxonomy
            r['abbrev_valid'] = abbrev_valid
            sort_dict[taxonomy] = r

        ret_val = [sort_dict[k] for k in sorted(sort_dict.keys())]

        if args[2] is False: # args[2] contains sort_desc variable value
            return count, ret_val
        else:
            return count, reversed(ret_val)

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
        """
        Adds HTML to the taxonomy column.
        Can use model information of get_list() method from now on
        TODO: Make use of model information, remove unneeded handlings and check which fallbacks should remain
        TODO: Handle markup handling of abbrevation
        :param context:
        :param model:
        :param name:
        :return:
        """
        # use string set in get_list() if given
        if 'taxonomy_string' in model:
            if model['abbrev_valid']:
                taxonomy = Markup(f"<a href='{get_editor_url(model['_courseId'], model['_id'])}'>{model['taxonomy_string']}</a>")
            else:
                colored = f"<span style=\"color: red\">{model['taxonomy_string']}</span>"
                taxonomy = Markup(
                    f"<a href='{get_editor_url(model['_courseId'], model['_id'])}'>{colored}</a>")
            model['taxonomy'] = taxonomy
            return taxonomy

        # Check if parent content object exists
        raw_index = get_related_content_index(model, 'contentobjects')
        if isinstance(raw_index, int):
            learning_unit = str(raw_index + 1)
            course_id = model['_courseId']

            # Fetch the domain information
            domain_title = str(db.contentobjects.find_one({'_id': model['_parentId']}).get("title"))
            domain_abbrev = domain_title[0]

            # If domain abbreviation is not A, B, or C, color it red
            if domain_abbrev not in ['A', 'B', 'C']:
                domain_abbrev = f'<span style="color: red">{domain_abbrev}</span>'
                domain_title = Markup(f'<span style="color: red">{domain_title}</span>')

            # Construct taxonomy with course title, domain abbreviation, and learning unit
            taxonomy = Markup(db.courses.find_one({'_id': course_id})['title'][:3] + '.' + domain_abbrev + '.' + learning_unit)
            taxonomy = Markup(f"<a href='{get_editor_url(course_id, model['_id'])}'>{taxonomy}</a>")

            # Optionally, you can also return or store the domain title if needed
            model['taxonomy'] = taxonomy
            model['domain'] = domain_title
        else:
            model['taxonomy'] = raw_index

        return model['taxonomy']

    def id_formatter(view, context, model, name):
        # Fetch the block that the model belongs to
        model_id = model['_id']

        return model_id

    column_formatters = {
        'taxonomy': taxonomy_formatter,
    '_id': id_formatter
    }

class ComponentView(MyModelView, metaclass=Meta):
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

        new_results = []

        for component in results:
            # Fetch the block that the component belongs to
            component_parent_id = component['_parentId']
            block = db.blocks.find_one({"_id": component_parent_id})

            # Fetch the article that the block belongs to
            block_parent_id = block['_parentId']
            article = db.articles.find_one({"_id": block_parent_id})

            if article:
                # Add 'additional' field to the component if it has a related article
                component['additional'] = True if get_related_content_index(article) else ''

                # Fetch the parent content object of the article
                article_parent_id = article['_parentId']
                parent_content_objects = list(db.contentobjects.find({"_id": article_parent_id}))

                # Add 'taxonomy' field to the component if it has a parent content object
                if len(parent_content_objects) > 0:
                    raw_index = get_related_content_index(parent_content_objects[0], 'contentobjects')
                    if type(raw_index) == int:
                        learning_unit = str(raw_index + 1)
                        course_id = article['_courseId']
                        domain_title = str(db.contentobjects.find_one({'_id': parent_content_objects[0]['_parentId']}).get("title"))
                        domain_abbrev = domain_title[0]
                        if domain_abbrev not in ['A', 'B', 'C']:
                            domain_abbrev = f'<span style="color: red">{domain_abbrev}</span>'
                            domain_title = Markup(f'<span style="color: red">{domain_title}</span>')
                        component['taxonomy'] = db.courses.find_one({'_id': course_id})['title'][:3] + '.' + domain_abbrev + '.' + learning_unit
                        component['taxonomy'] = Markup(f"<a href='{get_editor_url(course_id, article_parent_id)}'>{component['taxonomy']}</a>")
                        component['domain'] = domain_title
                    else:
                        component['taxonomy'] = '-'

                new_results.append(component)

        return count, new_results

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
    ]

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
    ]

class GraphicView(ComponentView):
    def get_init_query(self=None):
        return {'_component': { '$in': ['graphic', 'hotgraphic']}}

    init_query = get_init_query()

    component_types = db.components.find(init_query).distinct('_component')
    column_filters = [
        CustomFilter(column="_component", name="Typ",
                    options=[(str(component), component) for component in component_types])
    ]

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