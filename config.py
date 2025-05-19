import os

FLASK_ADMIN_SWATCH = 'cosmo'
SECRET_KEY = '123456790'
MONGO_URI = 'mongodb://172.17.0.2:27017/adapt'
BABEL_DEFAULT_LOCALE = 'de'
BABEL_DEFAULT_TIMEZONE = 'Europe/Berlin'
USERNAME = os.environ.get("USER")
PASSWORD = os.environ.get("PASS")
BUILDS_DIR = os.path.expanduser("/courses/61eacc691f4b290008b1bf46")
GLOSSARY_JSON = "/app/static/glossary.json"
ADDITIONAL_COMPONENTS = "/app/static/additional_components.json"
WTF_CSRF_ENABLED = True  # enables CSRF protection globally
AUTHORING_DOMAIN = "https://authoring.creativeartefact.org"