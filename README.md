# Adapt|OER

**Flask-Admin based web application** that serves as an **dashboard** for **e-learning content** produced with the **Adapt Authoring Tool**. The **Adapt Authoring Tool** integrates with **MongoDB** to store and manage learning materials, in different layers: courses, articles, blocks and components (interactive and presentative). The Authoring Tool only gives you an overview of courses, not their contents, which makes it hard to reuse them for Open Educational Resources (OERs) or any other format. Below is a breakdown of its **architecture, design patterns, and dependencies**.

---

## Environment variables

For the existing Docker stack, use [comdigi-compose](https://git.infai.org/n.radtke/comdigi-compose):
copy its `.env.dist` to `.env`, set your credentials and AAT tenant ID, and use
its documented local source mount. Start with `docker compose up -d`.
Do not create a second stack for Adapt|OER.

The tested `static/lxp-wrapper.zip` and matching SHA-256 are included. No separate
LXP checkout, wrapper download/build or manual hash calculation is necessary.
`static/lxp-wrapper-source.json` pins its upstream commit and original notices.

For a native installation, [credentials.env.dist](credentials.env.dist) lists
the supported settings, including wrapper, registry and catalogue paths. Supply
these environment variables through your process manager; the application does
not load a dotenv file itself. Persist the entire `instance/` directory (registry,
locks, catalogue edits and backups) and retain MongoDB with its export profiles.
Keep `LXP_ORIGIN_ID` stable across updates. Custom wrappers need their own verified
`LXP_WRAPPER_SHA256`; GitHub's repository source ZIP is not a built wrapper.

## Shared multi-framework LXP export

Technical AAT titles such as `DigComp1.1 – …` and `AILit1.1 – …` assign
published courses to the registered framework/module/course address. Display
Title is not parsed. Republish after changing the technical Title. The shared
LXP action provides a frozen preview before downloading one ZIP; reuse its
saved offer profile for updates so the package ID remains stable.

The course selection also scopes glossary links and learning pathways. The
preview lists omitted terms/paths and shortened destinations before confirmation;
the configured catalogue is not changed. Unknown sources, foreign-framework
links and missing content IDs remain errors, including in excluded courses.
The GUI verifies excluded destinations against accessible shared publications.
Programmatic subset exports with catalogue links to excluded courses must pass
`navigation_sources=[{'build': path, 'metadata': source_metadata}, ...]` for those
explicitly authorized publications. Referenced JSON snapshots are bound to the
preview; excluded assets and question text are not included in the package.

Administrators configure `LXP_WRAPPER_ZIP` and `LXP_WRAPPER_SHA256` with the
new hash-bound multi-framework LXP build, a stable `LXP_ORIGIN_ID`, and a durable
`LXP_REGISTRY_PATH`. Registered framework versions and modules come from
`static/lxp-catalogue.json` (or `LXP_CATALOGUE`), not from guessed prefixes.
New course numbers inside those modules are derived from the selected published
titles, e.g. `AILit1.5 – …`, without editing static slots. This extends only the
export-local catalogue: the configured catalogue remains unchanged. The preview
binds its configured hash separately from the delivered derived catalogue hash;
existing source IDs and explicit reassignment/version guards remain authoritative.
Prefix registration uses ASCII letters; module/course numbers stay separate
positive integer addresses, never decimal identities.
This does not require a new AAT plugin. Registered native sources are a separate
administrative path, not an arbitrary ZIP import. The explicit
`aat-core-build-v1` adapter validates known manifestless Core publications.

### Maintain frameworks in the GUI

Use **Kompetenzrahmen verwalten** next to **LXP-Export** while signed in with
the application's existing single administrator account:

1. Add the prefix, version and display name of a new framework.
2. Add its numbered modules. New published course numbers are discovered during export.
3. Add cognitive domains and the exact AAT parent-menu titles, one per line.
   Existing technical matching rules are preserved; no regular expressions are required.
4. Set entry/final test availability, counts, distribution and answer marking.
5. Save, then return to the export form and inspect its frozen preview.

New frameworks start with bounded 12-question tests only when suitable tagged
questions are available. Existing AILit/DigComp requirements are not changed.

Existing prefixes, versions, module numbers and technical IDs cannot be renamed
or deleted here. Add a new version rather than reinterpreting old identities.
An optional default domain applies only to flat sources without parent menus;
unmatched menu titles still fail rather than being guessed. Explicit settings
in saved offer profiles take precedence over new global defaults.

Edits use a separate `instance/lxp-catalogue.json` override (or
`LXP_CATALOGUE_STORE_PATH`). Keep that file and its adjacent `.backups` directory
with the persistent registry. The configured source catalogue is never overwritten.
Every real save preserves the previous bytes and assigns a new revision;
simultaneous edits require reloading instead of silently overwriting each other.
Existing pending export previews must be recreated after a catalogue change.

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

### Configure tests directly in Adapt|OER

There is no additional Adaptmin upload step. In the shared LXP export form:

1. Select the published courses and load the existing offer profile for an update.
2. Set entry and final tests separately for each framework.
3. Choose a bounded selection, all tagged questions, or the reviewed public
   ComDigiS* reference selection for DigComp 2.2 (24 entry / 39 final questions).
4. Set question count, balanced distribution and answer-marking preferences.
5. Check the preview's available/selected counts, module/course/domain coverage
   and the selected question list. Uncovered areas are **not assessed**, not 0%.
6. Optionally open **Einzelne Fragen auswählen**. Leave most questions on
   automatic, pin individual questions with **Fest aufnehmen**, or exclude them.
   Search by prompt, course or domain when the pool is large.
7. Apply those changes to calculate a new preview. Check its actual selected
   questions and coverage, then confirm it to download one shared LXP ZIP.

Manual choices belong to the current offer, framework and test phase. Other
offers do not inherit them. Pins take priority; the remaining eligible pool
fills the requested count using the existing policy. Excluded questions are
never re-added to fill a shortfall. Too many pins for the requested count are
an explicit error. The preview reports original/eligible/selected counts, not
a fabricated full test or a claim of representative sampling.

Unapplied changes disable normal confirmation in the browser. Without
JavaScript, submitting changed choices still creates a new preview instead
of downloading the old test. Confirmation remains bound to the exact frozen
questions; changed source data cannot be exported through an old preview.
No-longer-shared sources also block viewing the old preview. Invalid input retains the previous valid preview and
the author's unsaved choices without updating the offer profile.

Offer profiles retain fixed choices after export. A missing question, removed
`et`/`at` tag, or changed question content requires a new review. In the setup
form, **Gespeicherte Fragenfeinauswahl** offers an explicit reset of all pins
and exclusions for the new preview; count and marking choices remain intact.
The saved profile changes only after successful confirmation/export. The fixed
public DigComp reference selection is read-only: switch to a custom selection
and explicitly clear incompatible manual choices before changing it.

The GUI defaults to 24/39 DigComp questions and 12/12 AILit questions; smaller
pools produce an explicit notice rather than fabricated questions. Stored
profile choices take precedence. `et`/`at` still define the pool, not the final
test length. The reviewed public preset requires its actual unchanged source
questions; missing/edited questions stop that preset rather than being silently
substituted. Its labels, options, keys, domain and effective question weight are
bound to the reviewed source fingerprints.

The balanced policy is new, versioned `adapt-oer-balanced-selection@1`, **not**
a claimed reconstruction of Adaptmin's undocumented backend. It distributes
questions across available modules/courses and favours underrepresented
configured domains, redistributing exhausted capacities. Selection is stable
for unchanged inputs and is frozen at export time, not redrawn for each learner.
Balanced coverage is not psychometric validation.

Programmatic callers can pass `assessment_options`, keyed by framework ID or
prefix and then `entry`/`final`. Allowed phase keys are `mode` (`balanced`, `all`,
`reference`), `questionCount` (positive integer or null), `equalSampling`,
`showMarking` and `showModelAnswer` (explicit booleans), plus optional
`includeQuestions`/`excludeQuestions`. Each manual record has exactly
`{'identity': [originId, courseId, sourceComponentId], 'fingerprint': sha256}`,
using the current tagged pool's source-content fingerprint. Duplicate,
contradictory, unknown and changed records fail rather than being guessed.
Empty manual arrays are omitted canonically. Manual records cannot be used
as global catalogue defaults or modify the public reference preset.

The content fingerprint covers the authored question, options, keys and any
explicit question weight. Inherited course-wide grading settings are separately
bound to the full source/preflight snapshot and verified in native test banks;
changing them invalidates an old preview, not the identity of an unchanged
question in a newly calculated offer. Old API callers omitting
these options retain the full-pool/template behaviour. The exported
`assessment-selection.json` and provenance record the exact chosen identities,
fingerprints and coverage. Changed test sets produce changed native test
fingerprints; old responses are not relabelled as answers to new questions.
New exports also contain a safe `availableQuestions` inventory with selection
status and manual choices, never native answer payloads. The validator rebuilds
it against the original source pool. Older automatic exports without that
inventory remain valid; manually refined exports require it.

### Checked central assessment template

`static/assessment-template.zip` contains a versioned capabilities manifest.
Before generating a test, the exporter checks its actual bundled models,
renderers, dependencies, navigation and file hashes against an explicitly
approved content hash. Six input types use five native question runtimes;
GMCQ uses the existing image-preserving MCQ adapter. Re-hashing a changed bundle
does not approve it. Rebuild the existing template deterministically with
`python tools/rebuild_assessment_template.py`; unknown content requires review,
and failed validation leaves the input ZIP unchanged.

This template supplies generated diagnostics only. Do not remove real course
navigation components or plugin/build anchors before AAT publication: the test
template does not replace the learning course's own runtime or navigation.

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
