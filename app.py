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

app = Flask(__name__, template_folder="templates")
app.config.from_object(config)
babel = Babel(app)
conn = pymongo.MongoClient(app.config['MONGO_URI'])
db = conn.adapt
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
		course_dir = os.path.join(app.config['BUILDS_DIR'], course_id)

		# Create the ZIP file path (this is where the original zip is located)
		with zipfile.ZipFile(course_dir + '.zip', 'r') as zip_ref:
			# Create a temporary directory to extract the contents of 'build'
			temp_dir = tempfile.mkdtemp()

			# Extract only the 'build' folder contents (not the folder itself)
			for file in zip_ref.namelist():
				if file.startswith('build/'):
					zip_ref.extract(file, temp_dir)

			# Create a new temporary zip file to store only the contents of 'build'
			temp_zip = tempfile.mktemp(suffix='.zip')

			with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as temp_zip_ref:
				# Walk through the extracted files and add them to the new zip
				for root, dirs, files in os.walk(temp_dir):
					for file in files:
						file_path = os.path.join(root, file)
						arcname = os.path.relpath(file_path, temp_dir)
						arcname = arcname.replace("build/", "")  # Strip the 'build/' prefix            
						temp_zip_ref.write(file_path, arcname)

			# Send the new zip file containing only the contents of 'build'
			return send_file(temp_zip, as_attachment=True, download_name=f"{course_id}.zip")

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
	column_extra_row_actions = [
		template.EndpointLinkRowAction("fa fa-download", ".download_view", "{row_id}")
#		template.EndpointLinkRowAction("fa fa-print", ".print_view", "{row_id}"),
#		template.EndpointLinkRowAction("fa fa-circle-info", ".info_view", "{row_id}")
	]
	
	column_filters = [
		CustomFilter(column="tags", name="Tags",
			options=lambda: [(str(tag['_id']), tag['title']) for tag in db.tags.find()])
	]
		
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

	def get_init_query(self=None):
		all_ids = [doc['_id'] for doc in db.contentobjects.find({}, {"_id": 1})]
		return {"_parentId": {"$in": all_ids}}
	
	init_query = get_init_query()
		
	def get_course(self, _id):
		query = {"_id": _id}
		course = db.courses.find(query)[0]['displayTitle']

		return course

	def get_query(self):
		# Fetch IDs of shared courses
		shared_course_ids = [ObjectId(str(course['_id'])) for course in db.courses.find({'_isShared': True})]
		
		if hasattr(self, 'init_query') and self.init_query:
			self.init_query["_courseId"] = {"$in": shared_course_ids}
			return self.init_query
		else:
			return {"_courseId": {"$in": shared_course_ids}}

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

				return send_file(zip_buffer, as_attachment=True, download_name='lerneineiten.zip')

		except Exception as e:
				flash(f"Export fehlgeschlagen: {e}", "error")

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
			# Fetch the block that the model belongs to
			model_parent_id = model['_parentId']
			
			parent_content_objects = list(db.contentobjects.find({"_id": model_parent_id}))
			
			# Check if parent content object exists
			raw_index = get_related_content_index(model, 'contentobjects')
			if type(raw_index) == int:
					learning_unit = str(math.ceil(raw_index / 2) + 1)
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
						learning_unit = str(math.ceil(raw_index / 2) + 1)
						course_id = article['_courseId']
						domain_title = str(db.contentobjects.find_one({'_id': parent_content_objects[0]['_parentId']}).get("title"))
						domain_abbrev = domain_title[0]
						if domain_abbrev not in ['A', 'B', 'C']:
							domain_abbrev = f'<span style="color: red">{domain_abbrev}</span>'
							domain_title = Markup(f'<span style="color: red">{domain_title}</span>')
						component['taxonomy'] = Markup(db.courses.find_one({'_id': course_id})['title'][:3] + '.' + domain_abbrev + '.' + learning_unit)
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