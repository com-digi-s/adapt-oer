import os
import json
import re
import html
import uuid
import copy
from urls import get_asset_url
from markupsafe import Markup
from bs4 import BeautifulSoup
from zipfile import ZIP_DEFLATED
from config import GLOSSARY_JSON
from flask import flash, render_template

def add_files_to_zip(zip_file, base_dir, prefix=""):
    for foldername, _, filenames in os.walk(base_dir):
        for filename in filenames:
            file_path = os.path.join(foldername, filename)
            arcname = os.path.join(prefix, os.path.relpath(file_path, base_dir))
            zip_file.write(file_path, arcname, ZIP_DEFLATED)

def add_zip_to_zip(zip_file, path, arcname=None):
    for dirpath, dirnames, filenames in os.walk(path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            if arcname: 
                zip_file.write(filepath, arcname=os.path.join(arcname, filename))
            else: 
                zip_file.write(filepath)

def number_to_letter(n):
    return chr(n - 1 + ord('A'))

def encode_string_extended(s):
    def encode_part(part):
        if part.isdigit():
            return part  # Keep numeric parts as they are
        else:
            return str(ord(part.upper()) - ord('A') + 1)  # Convert 'A' to 1, 'B' to 2, etc.

    parts = s.split('.')
    encoded_parts = [encode_part(part) for part in parts]
    concatenated_string = ''.join(encoded_parts)

    return int(concatenated_string)

def decode_string_extended(encoded):
    s = str(encoded)
    # Assuming the pattern 'digit.digit.letter.digit'
    num1, num2, letter_code = s[0], s[1], int(s[2])
    letter = chr(letter_code - 1 + ord('A'))  # Convert the number back to a letter

    return f'{num1}.{num2}.{letter}'

def save_glossary(data):
    with open(GLOSSARY_JSON, 'w') as file:
        json.dump(data, file)

def load_glossary():
    if os.path.exists(GLOSSARY_JSON):
        with open(GLOSSARY_JSON, 'r') as file:
            return json.load(file)
    else:
        return {}

def read_json_file(path):
    try:
        with open(path, 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading {path}: {e}")
        return None

def process_content_objects(course, course_name, course_directory, item, found_terms):
    # Define the file path
    article_path = os.path.join(course, course_directory, 'articles.json')
    co_path = os.path.join(course, course_directory, 'contentObjects.json')

    with open(article_path, 'r') as file:
        articles = json.load(file)

    with open(co_path, 'r') as file:
        contentObjects = json.load(file)

    # Create a mapping from _id to contentObject for quick lookup
    articles_dict = {obj["_id"]: obj for obj in articles}
    contentObjects_dict = {obj["_id"]: obj for obj in contentObjects}

    learningUnit = contentObjects_dict.get(articles_dict.get(item["_parentId"])["_parentId"])
    domainObject = contentObjects_dict.get(learningUnit["_parentId"])

    # Construct the result object
    result = {
        "_id": learningUnit["_id"],
        "title": learningUnit["title"],
        "domain": domainObject["title"],
        "course": course_name,
        "terms": found_terms
    }

    return result

def retrieve_content_objects_by_term(terms_in_co, target_term):
    found_content_objects = []
    seen_ids = set()  # To keep track of content objects already added

    for entry in terms_in_co:
        # Check if the target_term is in the "terms" of the content object
        if target_term in entry["terms"]:
            # Avoid redundancy by checking if we've already seen this "_id"
            if entry["_id"] not in seen_ids:
                # Create a new dictionary excluding the "terms"
                content_object = {
                    "_id": entry["_id"],
                    "title": entry["title"],
                    "domain": entry["domain"],
                    "course": entry["course"]
                }
                found_content_objects.append(content_object)
                seen_ids.add(entry["_id"])

    return found_content_objects

def annotate_terms(text):
    glossary = load_glossary()
    tokens = re.split(r'(\s+|-)', text)
    annotated_text = []
    found = []

    glossary_terms = set(glossary.keys())

    for i, token in enumerate(tokens):
        if token.isspace() or token == "-":
            annotated_text.append(token)
            continue

        replacement = token
        if token in glossary_terms:
            escaped_definition = html.escape(glossary[token])
            replacement = f'<div title="{escaped_definition}" class="glossary-term" style="text-decoration: underline; line-height: 1.5; color: #4d4d4d; display: inline-block;">{token}</div>'
            found.append(token)

        is_hyphenated = i + 1 < len(tokens) and tokens[i + 1] == "-"
        if is_hyphenated:
            annotated_text.append(replacement)
        else:
            annotated_text.append(replacement + ' ')

    # the code does not work as intended so the output = input text

    return text, found #''.join(annotated_text).rstrip(), found

# Funktion zur Verarbeitung von transpiled_components innerhalb der Daten
def process_transpiled_components(data):
    # Identifizieren der transpiled_components innerhalb der Daten
    # Annahme: 'data' ist eine Liste von Komponenten
    transpiled_components = [
        comp for comp in data
        if (comp.get('_component') in ['slider', 'mcq', 'matching', 'infai-dragndrop']
            and 'self-assessment' not in comp.get('_classes', []))
    ]

    # Modifiziere spezifische Slider-Komponenten
    for comp in transpiled_components:            
        comp['_canShowFeedback'] = False

        if comp.get('_component') == 'slider' and 'attitude' in comp.get('_classes', []):
            # Setze die angegebenen Eigenschaften auf False
            comp['_canShowModelAnswer'] = False
            comp['_canShowFeedback'] = False
            comp['_canShowMarking'] = True
            comp['_shouldDisplayAttempts'] = False

            # Aktualisiere den _correctRange
            comp['_scaleStart'] = 1
            comp['_scaleEnd'] = 5
            comp['_correctRange'] = {
                '_bottom': 1,
                '_top': 5
            }

    return data

# Funktion zur Verarbeitung von Quellen-Komponenten innerhalb der Daten
def process_quellen_components(data, new_component_template):
    quellen_components = [component for component in data if component.get('title') == 'Quellen']

    for idx, component in enumerate(quellen_components):
        parent_id = component.get('_parentId')

        if not parent_id:
            print(f"Warnung: Komponente mit ID {component.get('_id')} hat keinen '_parentId'.")
            continue

        # Stelle sicher, dass '_layout' und '_onScreen' existieren
        component['_layout'] = "left"
        if "_onScreen" not in component:
            component["_onScreen"] = {}
        component["_onScreen"]["_percentInviewVertical"] = 50

        # Erstelle eine neue Komponente basierend auf der Vorlage
        new_component = copy.deepcopy(new_component_template)
        new_component['_id'] = str(uuid.uuid4())
        new_component['_parentId'] = parent_id
        new_component['_layout'] = "right"

        # F�ge Logik f�r das Aktivieren/Deaktivieren der n�chsten Schaltfl�che hinzu
        new_component["_buttons"]["_next"]["_isEnabled"] = idx != len(quellen_components) - 1

        # �berpr�fe, ob bereits eine �hnliche pageNav-Komponente existiert
        already_exists = any(
            comp.get('_component') == 'pageNav' and comp.get('_parentId') == parent_id
            for comp in data
        )

        if not already_exists:
            # F�ge die neue Komponente nach der aktuellen Komponente ein
            insert_index = data.index(component) + 1
            data.insert(insert_index, new_component)

    return data

class ComponentFormatter:
    def __init__(self, db):
        self.db = db
        self.glossary = load_glossary()

    def question_formatter(self, view, context, model, name):
        _component = model.get('_component', '')

        if _component == 'matching':
            items = model.get('properties', {}).get('_items', [])
            all_items_markup = []
            for item in items:
                item_markup = f"<div>{item.get('text')}</div>"
                all_items_markup.append(item_markup)
            return Markup(f"<div class='{_component}'>" + "<hr>".join(all_items_markup) + "</div>")
        
        return model.get('properties', {}).get('instruction', {})
    
    def answer_formatter(self, view, context, model, name):
        _component = model.get('_component', '')

        # Presentation Components
        if _component in ['accordion', 'narrative']:
            return Markup(f"<div class='{_component}'>" + "".join(
                f"<section><h4>{item.get('title', '')}</h4>" +
                f"<div>{Markup(item.get('body', ''))}</div>" +  # Render the body text as markup
                (f"<div><img src='{get_asset_url(self.db.assets.find_one({'filename': item.get('_graphic', {}).get('src', '').split('/')[-1]}))}' alt='{item.get('_graphic', {}).get('alt', '')}' width='200'></div>"  # Render the graphic if it exists
                if item.get('_graphic') else '') +
                (f"<p><b>Attribution:</b> {item.get('_graphic', {}).get('attribution', '')}</p>"  # Render attribution if present
                if item.get('_graphic') and item.get('_graphic').get('attribution') else '') +
                (f"<p><b>URL:</b> {item.get('_graphic', {}).get('_url', '')}</p>"  # Render URL if present
                if item.get('_graphic') and item.get('_graphic').get('_url') else '') +
                "</section>"
                for item in model.get('properties', {}).get('_items', [])
            ) + "</div>")

        elif _component in ['text', 'graphic']:
            html = model.get('body', '-')
            soup = BeautifulSoup(html, features="html.parser")
            new_html = str(soup)
            new_html = annotate_terms(new_html)[0]

            if _component == 'graphic':
                options = model.get('properties', {})
                graphic_options = options.get("_graphic", {})
                asset = self.db.assets.find_one({"filename": graphic_options.get("small", "").split("/")[-1]})
                
                # Extract attribution and URL from the graphic options, always returning a string
                attribution = graphic_options.get('attribution', "")
                url = graphic_options.get('_url', "")
                
                # Generate HTML with attribution and URL displayed below the graphic
                attribution_html = f"<p><b>Attribution:</b> {attribution}</p>" if attribution else ""
                url_html = f"<p><b>URL:</b> {url}</p>" if url else ""
                
                return Markup(new_html + f'<br><img width="200" src="{get_asset_url(asset)}">{attribution_html}{url_html}')

            return Markup(f"<div class='{_component}'>" + new_html + "</div>")

        elif _component == 'hotgraphic':
            options = model.get('properties', {})
            graphic_options = options.get("_graphic", {})
            asset = self.db.assets.find_one({"filename": graphic_options.get("src", "").split("/")[-1]})
            
            # Extract attribution and URL from the graphic options, always returning a string
            attribution = graphic_options.get('attribution', "-")
            url = graphic_options.get('_url', "-")
        
            # Generate HTML with attribution and URL displayed below the graphic
            attribution_html = f"<p><b>Attribution:</b> {attribution}</p>"
            url_html = f"<p><b>URL:</b> {url}</p>" if url != "-" else f"<p><b>URL:</b> -</p>"
            
            return Markup(f"<div class='{_component}'>" + f'<img width="200" src="{get_asset_url(asset)}">' + ' = ' +
                        ' + '.join(f'<img width="100" src="{get_asset_url(self.db.assets.find_one({ "filename": item["_graphic"].get("src", "").split("/")[-1] }))}">' for item in options["_items"]) +
                        f"{attribution_html}{url_html}</div>")
        
        # Question Components
        elif _component == 'mcq':
            properties = model.get('properties', {})
            return Markup(f"<div class='{_component}'>" +
                        "<ul>" + "".join(
                            f"<li><b>{item.get('text')}</b></li>" if item.get('_shouldBeSelected', False) else f"<li><s>{item.get('text')}</s></li>"
                            for item in properties.get('_items', [])
                        ) + "</ul></div>")

        elif _component == 'matching':
            items = model.get('properties', {}).get('_items', [])
            all_items_markup = []
            for item in items:
                item_markup = Markup("".join(
                    f"<li><b>{option.get('text')}</b></li>" if option.get('_isCorrect', False) else f"<li><s>{option.get('text')}</s></li>"
                    for option in item.get('_options', [])
                ))
                all_items_markup.append(f"<div>{item_markup}</div>")
            return Markup(f"<div class='{_component}'>" +
                        "<ul>" + "<hr>".join(all_items_markup) + "</ul></div>")

        elif _component in ['slider', 'confidenceSlider']:
            options = model.get('properties', {})
            return Markup(f"<div class='{_component}'>" +
                        f"<i>{options.get('labelStart', '')} ({options.get('_scaleStart', '')})</i><span>...</span>" +
                        f"<i>{options.get('labelEnd', '')} ({str(options.get('_scaleEnd', ''))})</i></div>")

        elif _component == 'textinput':
            options = model.get('body', '-')
            return Markup(f"<div class='{_component}'>" +
                        options + "</div>")  # Ensure body is rendered as markup

        elif _component in ['dragndrop', 'infai-dragndrop']:
            options = model.get('properties', {}).get('_items', [])
            return Markup(f"<div class='{_component}'>" +
                        "<ul>" + "".join(
                            f"<li><b>{option.get('text')}</b><li>" + ' ← ' + f"{'; '.join(option.get('accepted'))}"
                            for option in options
                        ) + "</ul></div>")

        elif _component == 'openTextInput':
            return Markup(f"<div class='{_component}'>" +
                        str(model.get('properties', {}).get('modelAnswer', {})) + "</div>")

        else:
            # implement other formatting here for other _component types
            # this is just a placeholder, replace with actual formatting code
            return Markup(f"<div class='{_component}'>" +
                        f"<h2>{model.get('displayTitle', 'Default Title')}</h2>" +  # Display the title
                        str(model.get('body', '')) + "</div>")
