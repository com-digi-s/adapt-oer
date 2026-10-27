import os

FLASK_ADMIN_SWATCH = 'cosmo'
SECRET_KEY = os.environ.get("SECRET_KEY")
MONGO_URI = 'mongodb://{}:{}/{}'.format(
    os.environ.get("DB_HOST"),
    os.environ.get("MONGODB_PORT"),
    os.environ.get("DB_NAME")
)
BABEL_DEFAULT_LOCALE = 'de'
BABEL_DEFAULT_TIMEZONE = 'Europe/Berlin'
USERNAME = os.environ.get("USER")
PASSWORD = os.environ.get("PASS")
BUILDS_DIR = os.path.expanduser("{}".format(
    os.environ.get("BUILDS_DIR")))
GLOSSARY_JSON = "./static/glossary.json"
ADDITIONAL_COMPONENTS = "./static/additional_components.json"
WTF_CSRF_ENABLED = True  # enables CSRF protection globally
AUTHORING_DOMAIN = os.environ.get("AUTHORING_DOMAIN")
# A tested deployment can use an immutable, locally built wrapper artifact.
# ZIP root must contain index.html/config.json and no already packaged scos.
LXP_WRAPPER_ZIP = os.environ.get('LXP_WRAPPER_ZIP')
LXP_WRAPPER_SHA256 = os.environ.get('LXP_WRAPPER_SHA256')
LXP_WRAPPER_URL = os.environ.get('LXP_WRAPPER_URL', 'https://github.com/com-digi-s/adapt-lxp/archive/refs/heads/main.zip')
