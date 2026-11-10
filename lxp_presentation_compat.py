"""Bounded, replayable proofs for explicit Markledge display changes.

Learning fingerprints remain unchanged. This module never writes sources or a
registry, and never equates content by stripping presentation-looking keys.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from lxp_catalogue import PackageValidationError, canonical_bytes


RULE = "markledge-generated-display-v1"
MAX_PROOFS = 2
MAX_PROOF_BYTES = 2 * 1024 * 1024
MAX_COMPONENT_TITLES = 512
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SCO_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);")
_DATA = ("contentObjects", "articles", "blocks", "components")
_IDENTITY = {"packageId", "scoId", "originId", "courseId"}
_ENVELOPE = {"format", "version", "rule", "identity", "previousFingerprint", "currentFingerprint",
             "previousSource", "componentDisplayTitles"}
_ROW = {"previousFingerprint", "path", "sha256", "componentDisplayTitles"}
_TITLE_ROW = {"componentId", "previousDisplayTitle", "currentDisplayTitle"}


def _require(value, message):
    if not value:
        raise PackageValidationError("Presentation compatibility: " + message)


def _units(value):
    return sum(2 if ord(character) > 0xffff else 1 for character in value)


def _text(value, maximum=4096):
    return (isinstance(value, str) and bool(value) and value == value.strip() and _units(value) <= maximum
            and not any(ord(character) < 32 or ord(character) == 127 or 0xd800 <= ord(character) <= 0xdfff
                        for character in value))


def _plain_title(value):
    return _text(value) and "<" not in value and ">" not in value and not _ENTITY.search(value)


def _same(left, right):
    # Python equality otherwise equates changed JSON flags/numbers (True == 1).
    return canonical_bytes(left) == canonical_bytes(right)


def _identifier(value):
    return _text(value, 256) and value not in {"__proto__", "prototype", "constructor"}


def _json_data(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        _require(depth <= 64 and count <= 200000, "JSON structure exceeds its bounds")
        if item is None or type(item) in (bool, int):
            continue
        if isinstance(item, str):
            _require(not any(0xd800 <= ord(character) <= 0xdfff for character in item), "invalid Unicode")
        elif type(item) is float:
            _require(math.isfinite(item), "nonfinite JSON number")
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            _require(all(isinstance(key, str) for key in item), "non-string JSON key")
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        else:
            _require(False, "unsupported JSON value")


def _snapshot(source):
    _require(isinstance(source, dict) and set(source) <= {"build", "language", "config", "course", "data"}
             and {"language", "config", "course", "data"} <= set(source), "invalid complete source snapshot")
    result = deepcopy({key: source[key] for key in ("language", "config", "course", "data")})
    _json_data(result)
    _require(_text(result["language"], 64) and isinstance(result["config"], dict)
             and isinstance(result["course"], dict) and isinstance(result["data"], dict)
             and set(result["data"]) == set(_DATA), "incomplete source snapshot")
    _require(len(canonical_bytes(result)) <= MAX_PROOF_BYTES, "source snapshot exceeds 2 MiB")
    return result


def _identity(value):
    _require(isinstance(value, dict) and set(value) == _IDENTITY
             and all(_text(item, 1024) for item in value.values())
             and _SCO_ID.fullmatch(value["scoId"]), "invalid exact package/SCO/source identity")
    return deepcopy(value)


def proof_path(sco_id, previous_fingerprint):
    _require(isinstance(sco_id, str) and _SCO_ID.fullmatch(sco_id)
             and isinstance(previous_fingerprint, str) and _SHA.fullmatch(previous_fingerprint), "invalid proof identity/path")
    return "presentation-compatibility/" + sco_id + "/" + previous_fingerprint + ".json"


def _visible(node):
    return all(type(node.get(key, default)) is bool and node.get(key, default) is default
               for key, default in (("_isAvailable", True), ("_isVisible", True), ("_isHidden", False)))


def _graph(data):
    by_id, groups = {}, {}
    for name in _DATA:
        _require(isinstance(data[name], list), "invalid graph collection")
        groups[name] = {}
        for node in data[name]:
            _require(isinstance(node, dict) and _identifier(node.get("_id"))
                     and _identifier(node.get("_parentId")) and node["_id"] != "course"
                     and node["_id"] not in by_id, "unknown or duplicate graph identity")
            by_id[node["_id"]] = node
            groups[name][node["_id"]] = node
    for node in data["contentObjects"]:
        parent = groups["contentObjects"].get(node["_parentId"])
        _require(node.get("_type") in ("menu", "page") and
                 (node["_parentId"] == "course" or parent is not None and parent.get("_type") == "menu"),
                 "invalid content-object ancestry")
        seen = {node["_id"]}
        while parent is not None:
            _require(parent["_id"] not in seen, "cyclic ancestry")
            seen.add(parent["_id"])
            parent = groups["contentObjects"].get(parent["_parentId"])
    for name, parent_group, expected_type, parent_type in (
            ("articles", "contentObjects", "article", "page"),
            ("blocks", "articles", "block", "article"),
            ("components", "blocks", "component", "block")):
        for node in data[name]:
            parent = groups[parent_group].get(node["_parentId"])
            _require(node.get("_type") == expected_type and parent is not None and parent.get("_type") == parent_type,
                     "invalid " + expected_type + " ancestry")
    return groups


def _eligible_titles(data):
    groups = _graph(data)
    first_blocks, first_components, eligible = {}, {}, {}
    for block in data["blocks"]:
        first_blocks.setdefault(block["_parentId"], block)
    for component in data["components"]:
        first_components.setdefault(component["_parentId"], component)
    for article in groups["articles"].values():
        title = article.get("displayTitle")
        block = first_blocks.get(article["_id"])
        component = first_components.get(block["_id"]) if block else None
        if (_visible(article) and _plain_title(title) and block is not None and _visible(block)
                and block.get("displayTitle") == "" and component is not None and _visible(component)
                and component.get("_component") == "text" and component.get("_isQuestionType", False) is False
                and component.get("title") == title and component.get("displayTitle") == title):
            eligible[component["_id"]] = title
    return eligible


def _source_key(node):
    marker = node.get("_markledgeMenuContent")
    value = marker.get("_sourceKey") if isinstance(marker, dict) else None
    return (_text(value, 1024) and value.endswith(".md") and not value.startswith("/")
            and not re.search(r"[\\%:#?]", value) and all(part and part not in (".", "..") for part in value.split("/")))


def _graphic(value):
    if not isinstance(value, dict) or not set(value) <= {"src", "_src", "alt", "attribution"}:
        return False
    targets = [value[key] for key in ("src", "_src") if key in value]
    return (len(targets) == 1 and isinstance(targets[0], str)
            and bool(re.fullmatch(r"(?:course/(?:assets|[A-Za-z0-9_-]+/(?:assets|images))|assets)/[A-Za-z0-9_.-]+\.(?:svg|png|jpe?g|gif|webp|avif)", targets[0]))
            and all(isinstance(item, str) for item in value.values()))


def _replay(previous, current):
    eligible = _eligible_titles(previous["data"])
    _graph(current["data"])
    replayed, rows = deepcopy(previous), []
    for name in _DATA:
        old_nodes, new_nodes = replayed["data"][name], current["data"][name]
        _require(len(old_nodes) == len(new_nodes), "graph collection length changed")
        for old, new in zip(old_nodes, new_nodes):
            _require(old["_id"] == new["_id"], "graph identity or order changed")
            if _same(old, new):
                continue
            different = {key for key in set(old) | set(new) if key not in old or key not in new or not _same(old[key], new[key])}
            if name == "components" and different == {"displayTitle"}:
                _require(old["_id"] in eligible and new.get("displayTitle") == "", "not an eligible generated heading suppression")
                rows.append({"componentId": old["_id"], "previousDisplayTitle": old["displayTitle"], "currentDisplayTitle": ""})
                old["displayTitle"] = ""
            elif name == "contentObjects" and different == {"_graphic"}:
                _require("_graphic" in old and "_graphic" in new and _source_key(old) and _source_key(new)
                         and _graphic(old["_graphic"]) and _graphic(new["_graphic"]), "not an eligible Markledge header graphic replacement")
                old["_graphic"] = deepcopy(new["_graphic"])
            else:
                _require(False, "real content or unsupported presentation field changed")
    old_menu, new_menu = replayed["course"].get("_boxMenu"), current["course"].get("_boxMenu")
    if isinstance(old_menu, dict) and isinstance(new_menu, dict) and not _same(old_menu, new_menu):
        old_graphic, new_graphic = old_menu.get("_graphic"), new_menu.get("_graphic")
        _require(_graphic(old_graphic) and _graphic(new_graphic), "not an eligible existing course cover replacement")
        old_menu["_graphic"] = deepcopy(new_graphic)
    _require(_same(replayed, current), "course/config/language/content differs after exact replay")
    _require(len(rows) <= MAX_COMPONENT_TITLES, "too many component title changes")
    return rows


def build_proof(previous_source, current_source, identity, fingerprint_fn):
    """Return (proof envelope, derived title rows); perform no filesystem writes."""
    previous, current, context = _snapshot(previous_source), _snapshot(current_source), _identity(identity)
    _require(callable(fingerprint_fn), "missing canonical fingerprint function")
    rows = _replay(previous, current)
    before, after = fingerprint_fn(previous), fingerprint_fn(current)
    _require(isinstance(before, str) and _SHA.fullmatch(before) and isinstance(after, str) and _SHA.fullmatch(after)
             and before != after, "invalid or redundant self fingerprint alias")
    envelope = {"format": "adapt-lxp-presentation-proof", "version": 1, "rule": RULE, "identity": context,
                "previousFingerprint": before, "currentFingerprint": after, "previousSource": previous,
                "componentDisplayTitles": rows}
    _require(len(canonical_bytes(envelope)) <= MAX_PROOF_BYTES, "proof exceeds 2 MiB")
    return envelope, deepcopy(rows)


def registered_proof_payloads(registrations, current_source, identity, fingerprint_fn):
    """Replay explicit package-scoped registry envelopes; return frozen payloads.

    Other offers and content revisions do not inherit these aliases. Historical
    proofs remain available for learners skipping an unchanged-content release.
    This function does not write the source, registry or package.
    """
    context = _identity(identity)
    _require(isinstance(registrations, dict) and 1 <= len(registrations) <= 8
             and all(_text(package, 1024) and isinstance(targets, dict) and 1 <= len(targets) <= 8
                     and all(isinstance(target, str) and _SHA.fullmatch(target) and isinstance(values, list)
                             and 1 <= len(values) <= MAX_PROOFS for target, values in targets.items())
                     for package, targets in registrations.items()), "invalid package/target-scoped registry proof map")
    _json_data(registrations)
    _require(len(canonical_bytes(registrations)) <= 8 * MAX_PROOF_BYTES, "registry proof map exceeds 16 MiB")
    current_fingerprint = fingerprint_fn(current_source)
    result, seen = [], set()
    for envelope in registrations.get(context["packageId"], {}).get(current_fingerprint, []):
        _require(isinstance(envelope, dict) and set(envelope) == _ENVELOPE
                 and envelope.get("identity") == context, "registry proof identity/fields differ")
        _json_data(envelope)
        _require(len(canonical_bytes(envelope)) <= MAX_PROOF_BYTES, "registry proof exceeds 2 MiB")
        rebuilt, rows = build_proof(envelope["previousSource"], current_source, context, fingerprint_fn)
        _require(envelope.get("currentFingerprint") == current_fingerprint and _same(envelope, rebuilt),
                 "altered registry proof or target fingerprint differs")
        previous = rebuilt["previousFingerprint"]
        _require(previous not in seen, "duplicate registry fingerprint alias")
        seen.add(previous)
        raw = canonical_bytes(rebuilt)
        relative = proof_path(context["scoId"], previous)
        record = {"previousFingerprint": previous, "path": relative,
                  "sha256": hashlib.sha256(raw).hexdigest(), "componentDisplayTitles": rows}
        result.append((relative, raw, record))
    return result


def _read_proof(root, relative):
    """Walk only ordinary package-owned directories/files, without following links."""
    directory = None
    descriptor = None
    try:
        directory = os.open(Path(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = relative.split("/")
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= MAX_PROOF_BYTES, "proof is not an admitted ordinary file")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_PROOF_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            _require(size <= MAX_PROOF_BYTES, "proof exceeds its read limit")
        after = os.fstat(descriptor)
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 and size == before.st_size, "proof changed while reading")
        return b"".join(chunks)
    except OSError as error:
        raise PackageValidationError("Presentation compatibility: proof cannot be safely read: " + relative) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise PackageValidationError("Presentation compatibility: nonfinite JSON constant: " + value)


def _title_rows(rows):
    _require(isinstance(rows, list) and len(rows) <= MAX_COMPONENT_TITLES, "invalid title-change list")
    seen = set()
    for row in rows:
        _require(isinstance(row, dict) and set(row) == _TITLE_ROW and _identifier(row["componentId"])
                 and row["componentId"] not in seen and _plain_title(row["previousDisplayTitle"])
                 and row["currentDisplayTitle"] == "", "invalid exact title-change row")
        seen.add(row["componentId"])


def validate_presentation_compatibility(metadata, root, sco, package_id, current_source, fingerprint_fn):
    """Read and replay declared proofs against the actual delivered source."""
    _require(isinstance(sco, dict) and sco.get("role") == "learning", "aliases are learning-only")
    context = _identity({"packageId": package_id, "scoId": sco.get("id"),
                         "originId": sco.get("source", {}).get("originId"), "courseId": sco.get("source", {}).get("courseId")})
    _require(isinstance(metadata, dict) and set(metadata) == {"version", "rule", "proofs"}
             and type(metadata["version"]) is int and metadata["version"] == 1 and metadata["rule"] == RULE,
             "unsupported exact declaration")
    proofs = metadata["proofs"]
    _require(isinstance(proofs, list) and 1 <= len(proofs) <= MAX_PROOFS, "expected one or two explicit proofs")
    current = _snapshot(current_source)
    current_fingerprint = fingerprint_fn(current)
    _require(sco["source"].get("componentFingerprint") == current_fingerprint, "current canonical fingerprint differs")
    seen_fingerprints, seen_paths = set(), set()
    for record in proofs:
        _require(isinstance(record, dict) and set(record) == _ROW, "unsupported proof record")
        previous, digest = record["previousFingerprint"], record["sha256"]
        _require(isinstance(previous, str) and _SHA.fullmatch(previous) and previous != current_fingerprint
                 and previous not in seen_fingerprints and isinstance(digest, str) and _SHA.fullmatch(digest),
                 "invalid, duplicate or self fingerprint alias")
        expected_path = proof_path(context["scoId"], previous)
        _require(record["path"] == expected_path and record["path"] not in seen_paths, "unsafe or duplicate proof path")
        _title_rows(record["componentDisplayTitles"])
        seen_fingerprints.add(previous)
        seen_paths.add(record["path"])
    for record in proofs:
        raw = _read_proof(root, record["path"])
        _require(hashlib.sha256(raw).hexdigest() == record["sha256"], "proof SHA256 differs")
        try:
            envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise PackageValidationError("Presentation compatibility: invalid bounded UTF-8 proof JSON") from error
        _json_data(envelope)
        _require(isinstance(envelope, dict) and set(envelope) == _ENVELOPE and envelope.get("identity") == context,
                 "proof has unknown fields or belongs to another identity")
        rebuilt, rows = build_proof(envelope["previousSource"], current, context, fingerprint_fn)
        _require(_same(envelope, rebuilt) and record["previousFingerprint"] == rebuilt["previousFingerprint"]
                 and _same(record["componentDisplayTitles"], rows), "proof/fingerprint/title changes differ from exact replay")
    return deepcopy(proofs)
