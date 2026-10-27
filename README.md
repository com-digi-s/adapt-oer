# Adapt|OER

**Flask-Admin based web application** that serves as an **dashboard** for **e-learning content** produced with the **Adapt Authoring Tool**. The **Adapt Authoring Tool** integrates with **MongoDB** to store and manage learning materials, in different layers: courses, articles, blocks and components (interactive and presentative). The Authoring Tool only gives you an overview of courses, not their contents, which makes it hard to reuse them for Open Educational Resources (OERs) or any other format. Below is a breakdown of its **architecture, design patterns, and dependencies**.

---

## **Environment Variables**

You need to set the following environment variables to run Adapt|OER.
Therefor copy the given [credentials.env.dist](credentials.env.dist) and apply the variables on your host.

```
USER=your-account
PASS=your-password
GITLAB_ACCESS_TOKEN=...
# mongodb
MONGODB_PORT=27017
DB_HOST=127.0.0.1
DB_NAME=adapt
SECRET_KEY=123456790
# adapt authoring
BUILDS_DIR=/path/to/adapt_authoring/temp/tenantId/adapt_framework/courses
AUTHORING_DOMAIN=https://your.auhtoring.url
```

## Shared multi-framework LXP export

Technical AAT titles such as `DigComp1.1 – …` and `AILit1.1 – …` assign
published courses to the registered framework/module/course address. Display
Title is not parsed. Republish after changing the technical Title. The shared
LXP action provides a frozen preview before downloading one ZIP; reuse its
saved offer profile for updates so the package ID remains stable.

Administrators configure `LXP_WRAPPER_ZIP` and `LXP_WRAPPER_SHA256` with the
new hash-bound multi-framework LXP build, a stable `LXP_ORIGIN_ID`, and a durable
`LXP_REGISTRY_PATH`. Framework versions and permitted addresses come from
`static/lxp-catalogue.json` (or `LXP_CATALOGUE`), not from guessed prefixes.
This does not require a new AAT plugin. Registered native sources are a separate
administrative path, not an arbitrary ZIP import. The explicit
`aat-core-build-v1` adapter validates known manifestless Core publications.

Diagnostics are requested separately for each framework/phase. Missing optional
diagnostics are reported as not provided, never passed or scored zero. Required
diagnostics without supported assigned questions block the export. Completion
requires all required learning courses across both frameworks, not a fabricated
combined competency score. The conservative SCORM1.2 profile limits suspend
data to 4096 characters; local persistence is not a target-LMS release.

In AAT, place additional supported Core questions in ordinary unavailable
(hidden), optional articles under the existing matching A/B/C menus. Keep the
questions themselves available, add the question CSS classes `et` and/or `at`,
and republish. No additional article CSS class is required. These articles remain
in the published learning source without adding a learning-completion duty;
the central assessment template supplies the supported question plugins and
their dependencies for the generated tests. Changed course publications still
need a compatibility check before existing learner state is reused.

### Regression tests

Install test dependencies separately from production requirements, then run:

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

GUI tests exercise actual Flask routes and the exporter using isolated builds
and `mongomock`; no production MongoDB, AAT instance or external LMS is changed.

## **Architecture Overview**

### **Tech Stack**
- **Backend**: Flask (Python) for API and server-side rendering
- **Database**: MongoDB (via PyMongo)
- **Admin Panel**: Flask-Admin for managing SCORM content
- **Frontend**: Flask templates (Jinja2)
- **Authentication**: Flask-Login for user management (assumes User is authenticated in Adapt Authoring Tool)
- **Internationalization**: Flask-Babel for multilingual support
- **SCORM Integration**: SCORM-compatible learning modules
- **Forms & Validation**: Flask-WTF and WTForms
- **File Handling**: Handles ZIP files for packaging quizzes and learning content

---

## **Key Components and Responsibilities**

### **Application Core (`app.py`)**
- Initializes the **Flask app**
- Configures database connection (`MongoDB`)
- Sets up authentication (`Flask-Login`)
- Defines **routes** for user authentication, course content, and quiz generation
- Serves as the entry point

---

### **Authentication & User Management**
- Uses `Flask-Login` to **authenticate a single admin user**.
- `@login_required` decorator ensures restricted access to the admin panel.
- **Session handling** is done via Flask’s built-in session mechanism.

**Key methods:**
- `login()` → Handles login requests and verifies credentials
- `logout()` → Logs out the user
- `load_user()` → Retrieves authenticated user from session

---

### **Admin Panel (`Flask-Admin`)**
- Provides a dashboard for **managing courses, content objects, questions, and media**.
- Uses `ModelView` subclasses to manage MongoDB collections.

**Admin Views:**
- `CourseView` → Manages e-learning courses
- `ContentsView` → Handles learning content and taxonomy
- `QuestionView` → Manages quizzes and assessments
- `PresentationView` → Handles text-based learning components
- `GraphicView` → Manages image-based content
- `GlossaryView` → Stores glossary terms and definitions

---

### **SCORM Integration**
- Uses `pipwerks.SCORM` to track progress and completion within the LMS.
- Learning content is managed in **MongoDB**, where:
  - `contentobjects` store **course units**
  - `blocks` store **sections within units**
  - `components` store **questions, media, and text elements**

---

### **Content Filtering & Organization**
- `filter_components_and_blocks()` extracts and **groups components into learning blocks**.
- `get_related_content_index()` finds related **content items** in a course.
- `fetch_and_nest_content()` recursively structures **content hierarchies**.

---

### **Quiz & Assessment Handling**
- Supports dynamic quiz generation using `prepare_quizzes()` and `compose_quiz()`.
- Uses **Flask routes to generate, bundle, and deliver quizzes as SCORM packages**.
- Uses `transpile_bson.py` to **convert MongoDB BSON data into JSON-compatible format**.

---

## **Dependencies**
| Library | Purpose |
|---------|---------|
| **Flask** | Web framework |
| **Flask-Login** | User authentication |
| **Flask-Admin** | Admin dashboard |
| **Flask-WTF** | Form handling |
| **Flask-Babel** | i18n support |
| **PyMongo** | MongoDB integration |
| **WTForms** | Form validation |
| **ZipFile** | ZIP file handling |
| **Requests** | HTTP requests |
| **BeautifulSoup** | HTML parsing for glossary and text processing |
| **Werkzeug** | File security utilities |

---

## **Key Features**
✅ **SCORM-Ready**: Generates SCORM-compatible packages for LMS integration.  
✅ **User Authentication**: Admin-only access using Flask-Login.  
✅ **MongoDB-Based CMS**: Stores, manages, and retrieves learning content.  
✅ **Dynamic Quiz Generation**: Filters, modifies, and bundles quizzes as ZIP files.  
✅ **Multi-Language Support**: Flask-Babel for translations.  
✅ **Admin Panel**: Manages courses, components, and glossary.  

---

## **Summary**
This project is a **SCORM-compatible e-learning content manager** that integrates **MongoDB, Flask-Admin, and Flask-Login** to provide an **admin dashboard for managing quizzes, courses, and media assets**. It follows **best practices in design patterns** to ensure scalability, modularity, and efficiency.
