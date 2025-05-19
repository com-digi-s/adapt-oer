import copy
import json
import os
import shutil
from bs4 import BeautifulSoup
from flask import flash

type_mapping = {
	"slider": "mcq",
	"mcq": "mcq",
	"matching": "quiz",
	"openTextInput": "essay",
	"textinput": "essay",
	"accordion": "accordion",
	"infai-dragndrop": "drag-the-words",
	"dragndrop": "drag-the-words"
}

def extract_keywords(html_text):
	soup = BeautifulSoup(html_text, 'html.parser')
	keywords = []
	alternatives = []

	for tag in soup.find_all('b'):
		text = tag.text
		start_parentheses = text.find("(")
		if start_parentheses != -1:
			keyword = text[:start_parentheses].strip()
			alternative = text[start_parentheses+1:text.find(")")].strip()
			alternatives.append(alternative)
		else:
			keyword = text

		keywords.append(keyword)

	return keywords, alternatives

def generate_h5p_content(component_id, component_type, instruction=None, question=None, answers=None, correct_answers=None):
	# Load template
	template_dir = f'static/h5p/{type_mapping[component_type]}'
	# Create a temporary directory
	h5p_dir = f'./static/h5p/{component_id}'
	os.makedirs(h5p_dir, exist_ok=True)
	shutil.copytree(template_dir, h5p_dir, dirs_exist_ok=True)
	output_path = f'{h5p_dir}/content/content.json'

	with open(template_dir + '/' + 'content/content.json', 'r') as f:
		content = json.load(f)

	if component_type in ["mcq", "slider"]:
		# Replace question
		content["question"] = f"<p>{question}</p>\n"
		content["behaviour"]["randomAnswers"] = False

		for idx, answer in enumerate(answers):
			content['answers'].append({
				"text": f"<div>{answer}</div>\n",
				"correct": idx in correct_answers,
    			"points": 1,
				"tipsAndFeedback": {
					"tip": "",
					"chosenFeedback": "",
					"notChosenFeedback": ""
				}
			})
	
	elif component_type == "matching":
		for q_idx, q in enumerate(question):
			if q_idx > 0:
				content["questions"].append(copy.deepcopy(content["questions"][0]))
			else:
				content["questions"].append({
					"params": {}
				})
    
			content["questions"][q_idx]["params"]["question"] = f"<p>{q}</p>\n"
			content["questions"][q_idx]["params"]["answers"] = []

			for idx, answer in enumerate(answers[q_idx]):
				content["questions"][q_idx]["params"]["answers"].append({
					"text": f"<div>{answer}</div>\n",
					"correct": idx in correct_answers[q_idx],
					"tipsAndFeedback": {
						"tip": "",
						"chosenFeedback": "",
						"notChosenFeedback": ""
					}
				})

	elif component_type == "openTextInput":
		content['taskDescription'] = question
		content['solution']['sample'] = answers[0]
		keywords, alternatives = extract_keywords(answers[0])

		for keyword, alternative in zip(keywords, alternatives):
			content['keywords'].append({
				"options": {
					"points": 1,
					"occurrences": 1,
					"caseSensitive": False,
					"forgiveMistakes": True,
					"feedbackIncluded": "✓ " +keyword + f" ({alternative})"
				},
				"keyword": keyword,
				"alternatives": alternative.split(";")
			})

	elif component_type in ["infai-dragndrop", "dragndrop"]:
		content["taskDescription"] = instruction

		# Replace question
		for q_idx, q in enumerate(question):
			content["textField"] += f"{q + ': *' + ' *; *'.join(correct_answers[q_idx]) + '*'}\n"
	
	# Write the generated content back to a new JSON file
	with open(output_path, 'w') as f:
		json.dump(content, f, indent=4)

	print(f'Generated H5P content saved at: {os.path.abspath(output_path)}')
	return h5p_dir

def generate_h5p_from_component(component_id, component):
	component_type = component['_component']
	instruction = component.get('properties', {}).get('instruction', '')
	question = []
	answers = []
	correct_answers = []
 
	if component_type == "mcq":
		question = component.get('title', '')
		answers = [item.get('text', '') for item in component.get('properties', {}).get('_items', [])]
		correct_answers = [i for i, item in enumerate(component.get('properties', {}).get('_items', [])) if item.get('_shouldBeSelected', False)]

	elif component_type == "matching":
		items = component.get('properties', {}).get('_items', [])
		for item in items:
			question.append(item.get('text', ''))
			answers.append([])
			correct_answers.append([])
			for idx, opt in enumerate(item.get('_options', [])):
				answers[-1].append(opt.get('text', ''))
				if opt.get('_isCorrect', False):
					correct_answers[-1].append(idx)

	elif component_type in ["infai-dragndrop", "dragndrop"]:
		items = component.get('properties', {}).get('_items', [])
		for item in items:
			question.append(item.get('text', ''))
			answers.append([])
			correct_answers.append([])
			for idx, accepted in enumerate(item.get('accepted', [])):
				correct_answers[-1].append(accepted)

	elif component_type == "slider":
		properties = component.get('properties', {})
		question = properties.get('instruction', '')
		for item in range(properties["_scaleStart"], properties["_scaleEnd"], properties.get("_scaleStep", 1)):
			answers.append(item)

		if (properties["_correctAnswer"] != ""):
			correct_answers = [properties["_correctAnswer"]]
		else:
			correct_answers = [len(answers)]
		correct_answers = [len(answers)-1]
		
	elif component_type == "openTextInput":
		properties = component.get('properties', {})
		question = properties.get('instruction', '')
		answers = [properties.get('modelAnswer', '')]
		correct_answers = answers
  
	elif component_type == "accordion":
		question = "test"
		answers = ["test"]
		correct_answers = ["test"]

	# generate the h5p content
	return generate_h5p_content(component_id, component_type, instruction=instruction, question=question, answers=answers, correct_answers=correct_answers)
