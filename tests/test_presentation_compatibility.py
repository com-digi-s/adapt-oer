"""Replay exact generated-display changes without weakening learning identity."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from lxp_catalogue import PackageValidationError, canonical_bytes
from lxp_export import published_course
from lxp_multi_export import learning_compatibility_fingerprint, validate_product_contract
from lxp_presentation_compat import RULE, build_proof, proof_path, validate_presentation_compatibility
import test_multi_framework_export as fixtures


IDENTITY = {"packageId": "pkg-stable", "scoId": "sco-stable", "originId": "native:author", "courseId": "source-course"}


def source():
    return {"language": "de", "config": {"_defaultLanguage": "de", "_isTrackable": True},
            "course": {"_id": "course", "title": "AILit1.1 – KI", "_isComplete": False},
            "data": {
                "contentObjects": [{"_id": "page", "_parentId": "course", "_type": "page", "title": "Seite",
                                    "_markledgeMenuContent": {"_sourceKey": "A/lesson.md"},
                                    "_graphic": {"src": "course/assets/old.svg", "alt": "Seite", "attribution": ""}}],
                "articles": [{"_id": "article", "_parentId": "page", "_type": "article", "title": "Lernziele", "displayTitle": "Lernziele"}],
                "blocks": [{"_id": "block", "_parentId": "article", "_type": "block", "displayTitle": "", "_trackingId": 1}],
                "components": [{"_id": "text", "_parentId": "block", "_type": "component", "_component": "text",
                                "title": "Lernziele", "displayTitle": "Lernziele", "body": "<p>Alle Ziele bleiben erhalten.</p>"},
                               {"_id": "question", "_parentId": "block", "_type": "component", "_component": "mcq",
                                "title": "Frage", "body": "<p>Unveränderte Frage?</p>", "_isQuestionType": True,
                                "_items": [{"text": "Ja", "_shouldBeSelected": True}, {"text": "Nein"}]}]}}


def changed(previous, *, graphic=True, heading=True):
    current = deepcopy(previous)
    if heading:
        current["data"]["components"][0]["displayTitle"] = ""
    if graphic:
        current["data"]["contentObjects"][0]["_graphic"]["src"] = "course/assets/new.svg"
    return current


def write_proof(root, previous, current, identity=IDENTITY):
    envelope, rows = build_proof(previous, current, identity, learning_compatibility_fingerprint)
    raw = canonical_bytes(envelope)
    path = proof_path(identity["scoId"], envelope["previousFingerprint"])
    file = Path(root) / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(raw)
    return {"previousFingerprint": envelope["previousFingerprint"], "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(), "componentDisplayTitles": rows}


class PresentationCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.previous = source()
        self.current = changed(self.previous)

    def test_exact_heading_and_graphic_replay_is_pure_and_uses_existing_fingerprint(self):
        before, after = deepcopy(self.previous), deepcopy(self.current)
        envelope, rows = build_proof(self.previous, self.current, IDENTITY, learning_compatibility_fingerprint)
        self.assertEqual(rows, [{"componentId": "text", "previousDisplayTitle": "Lernziele", "currentDisplayTitle": ""}])
        self.assertEqual(envelope["previousFingerprint"], learning_compatibility_fingerprint(self.previous))
        self.assertEqual(envelope["currentFingerprint"], learning_compatibility_fingerprint(self.current))
        self.assertNotEqual(envelope["previousFingerprint"], envelope["currentFingerprint"])
        self.assertEqual((self.previous, self.current), (before, after))

    def test_graphic_only_proof_has_no_component_alias_rows(self):
        envelope, rows = build_proof(self.previous, changed(self.previous, heading=False), IDENTITY, learning_compatibility_fingerprint)
        self.assertEqual(rows, [])
        self.assertEqual(envelope["componentDisplayTitles"], [])

    def test_heading_only_proof_preserves_every_other_byte_value(self):
        current = changed(self.previous, graphic=False)
        envelope, _ = build_proof(self.previous, current, IDENTITY, learning_compatibility_fingerprint)
        restored = deepcopy(current)
        restored["data"]["components"][0]["displayTitle"] = "Lernziele"
        self.assertEqual(canonical_bytes(restored), canonical_bytes(envelope["previousSource"]))

    def test_two_previous_versions_replay_directly_against_current(self):
        middle = changed(self.previous, graphic=False)
        with tempfile.TemporaryDirectory() as directory:
            records = [write_proof(directory, old, self.current) for old in (self.previous, middle)]
            metadata = {"version": 1, "rule": RULE, "proofs": records}
            sco = {"id": IDENTITY["scoId"], "role": "learning", "source": {
                "originId": IDENTITY["originId"], "courseId": IDENTITY["courseId"],
                "componentFingerprint": learning_compatibility_fingerprint(self.current)}}
            self.assertEqual(validate_presentation_compatibility(metadata, directory, sco, IDENTITY["packageId"],
                             self.current, learning_compatibility_fingerprint), records)

    def test_unchanged_source_cannot_create_a_self_alias(self):
        with self.assertRaisesRegex(PackageValidationError, "self fingerprint"):
            build_proof(self.previous, self.previous, IDENTITY, learning_compatibility_fingerprint)

    def test_real_question_and_feedback_changes_are_not_presentation(self):
        for key, value in (("body", "<p>Andere Frage?</p>"), ("_items", [{"text": "Andere Antwort"}]),
                           ("_feedback", {"correct": "Andere Rückmeldung"}), ("_isQuestionType", False)):
            current = deepcopy(self.current)
            current["data"]["components"][1][key] = value
            with self.subTest(key=key), self.assertRaises(PackageValidationError):
                build_proof(self.previous, current, IDENTITY, learning_compatibility_fingerprint)

    def test_course_config_and_language_are_exact_even_outside_learning_fingerprint(self):
        for collection, key, value in (("course", "title", "Neuer Titel"), ("course", "_isComplete", 0),
                                       ("config", "_defaultLanguage", "en"), ("config", "_isTrackable", 1)):
            current = deepcopy(self.current)
            current[collection][key] = value
            with self.subTest(collection=collection, key=key), self.assertRaises(PackageValidationError):
                build_proof(self.previous, current, IDENTITY, learning_compatibility_fingerprint)
        current = deepcopy(self.current)
        current["language"] = "en"
        with self.assertRaises(PackageValidationError):
            build_proof(self.previous, current, IDENTITY, learning_compatibility_fingerprint)

    def test_json_boolean_integer_and_float_substitutions_are_real_content_changes(self):
        for collection, index, key, before, after in (
                ("components", 1, "_questionWeight", 1, 1.0),
                ("components", 1, "_questionWeight", 1, True),
                ("components", 1, "_isQuestionType", True, 1),
                ("articles", 0, "_isAvailable", True, 1),
                ("blocks", 0, "_trackingId", 1, 1.0),
                ("contentObjects", 0, "_isOptional", False, 0)):
            previous = deepcopy(self.previous)
            previous["data"][collection][index][key] = before
            current = changed(previous)
            current["data"][collection][index][key] = after
            with self.subTest(collection=collection, key=key, after=after), self.assertRaises(PackageValidationError):
                build_proof(previous, current, IDENTITY, learning_compatibility_fingerprint)

    def test_hidden_distinct_nontext_and_later_component_are_not_eligible(self):
        for mutation in (lambda graph: graph["articles"][0].update(_isVisible=False),
                         lambda graph: graph["components"][0].update(title="Andere Überschrift"),
                         lambda graph: graph["components"][0].update(_component="graphic"),
                         lambda graph: graph["components"].reverse()):
            previous = deepcopy(self.previous)
            mutation(previous["data"])
            current = deepcopy(previous)
            next(item for item in current["data"]["components"] if item["_id"] == "text")["displayTitle"] = ""
            with self.subTest(mutation=mutation), self.assertRaises(PackageValidationError):
                build_proof(previous, current, IDENTITY, learning_compatibility_fingerprint)

    def test_graphic_replacement_requires_existing_markledge_header(self):
        for mutation in (lambda node: node.pop("_markledgeMenuContent"), lambda node: node.update(_type="article"),
                         lambda node: node["_markledgeMenuContent"].update(_sourceKey="../lesson.md"),
                         lambda node: node.pop("_graphic")):
            previous = deepcopy(self.previous)
            mutation(previous["data"]["contentObjects"][0])
            current = deepcopy(previous)
            current["data"]["components"][0]["displayTitle"] = ""
            current["data"]["contentObjects"][0]["_graphic"] = {"src": "course/assets/new.svg", "alt": "Seite", "attribution": ""}
            with self.subTest(mutation=mutation), self.assertRaises(PackageValidationError):
                build_proof(previous, current, IDENTITY, learning_compatibility_fingerprint)


class PresentationProductValidationTest(unittest.TestCase):
    setUp = fixtures.MixedPackageTest.setUp
    write_wrapper = fixtures.MixedPackageTest.write_wrapper
    add_source = fixtures.MixedPackageTest.add_source
    prepare = fixtures.MixedPackageTest.prepare
    export = fixtures.MixedPackageTest.export

    def delivered(self, *, capability=True):
        build = self.builds[0]
        old = published_course(build)
        graph = old["data"]
        graph["contentObjects"][0]["_markledgeMenuContent"] = {"_sourceKey": "A/lesson.md"}
        graph["contentObjects"][0]["_graphic"] = {"src": "course/de/assets/test.svg", "alt": "Page"}
        graph["articles"][0].update(_type="article", title="Lernziele", displayTitle="Lernziele")
        graph["blocks"][0].update(_type="block", displayTitle="")
        graph["components"][0]["_type"] = "component"
        graph["components"].insert(0, {"_id": "generated-title", "_parentId": "block", "_type": "component",
            "_component": "text", "title": "Lernziele", "displayTitle": "Lernziele", "body": "<p>Originaler Inhalt.</p>"})
        current = deepcopy(old)
        current["data"]["components"][0]["displayTitle"] = ""
        for name, nodes in current["data"].items():
            fixtures.save(build / ("course/de/" + name + ".json"), nodes)
        with self.export(self.prepare()) as archive:
            root = self.root / "delivered"
            archive.extractall(root)
        contract = json.loads((root / "lxp-contract.json").read_text())
        sco = next(item for item in contract["scos"] if item["role"] == "learning" and item["source"]["originId"] == self.metadata[0]["originId"])
        provenance = json.loads((root / "package-provenance.json").read_text())
        identity = {"packageId": contract["package"]["id"], "scoId": sco["id"],
                    "originId": sco["source"]["originId"], "courseId": sco["source"]["courseId"]}
        record = write_proof(root, old, published_course(root / sco["launch"]["root"]), identity)
        sco["source"]["presentationCompatibility"] = {"version": 1, "rule": RULE, "proofs": [record]}
        next(item for item in provenance["courses"] if item["scoId"] == sco["id"])["source"]["presentationCompatibility"] = deepcopy(sco["source"]["presentationCompatibility"])
        if capability:
            marker = json.loads((root / "adapt-multi-framework-capabilities.json").read_text())
            marker["presentationCompatibility"] = 1
            provenance["wrapperCapability"] = marker
            fixtures.save(root / "adapt-multi-framework-capabilities.json", marker)
        fixtures.save(root / "package-provenance.json", provenance)
        fixtures.save(root / "lxp-contract.json", contract)
        return root, contract, sco

    def test_full_product_validator_replays_bound_proof(self):
        root, contract, _ = self.delivered()
        self.assertTrue(validate_product_contract(contract, root))

    def test_full_product_validator_requires_actual_wrapper_capability(self):
        root, contract, _ = self.delivered(capability=False)
        with self.assertRaisesRegex(PackageValidationError, "presentation compatibility capability"):
            validate_product_contract(contract, root)

    def test_full_product_validator_rejects_generated_assessment_alias(self):
        root, contract, _ = self.delivered()
        generated = next(item for item in contract["scos"] if item["role"] != "learning")
        generated["source"]["presentationCompatibility"] = None
        with self.assertRaisesRegex(PackageValidationError, "generated assessments"):
            validate_product_contract(contract, root)

    def test_full_product_validator_checks_actual_state_and_core_runtime_bytes(self):
        root, contract, _ = self.delivered()
        for name in ("js/multiState.js", "js/corePersistence.js"):
            path = root / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n// changed runtime without a new capability digest\n")
            with self.subTest(file=name), self.assertRaisesRegex(PackageValidationError, "implementation SHA-256 mismatch"):
                validate_product_contract(contract, root)
            path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
