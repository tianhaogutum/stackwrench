"""Compare explicit captures without interpreting tags or CRI IDs as content identity."""

from .inputs import InputError
from .inventory import Inventory
from .evidence import tag_digest_evidence, tag_record_set_evidence
from .references import parse_reference


EVIDENCE_NOTE = (
    "capturedAt/source are caller-supplied evidence only; a timestamp does not guarantee freshness, "
    "completeness, cluster identity or chronological order. Missing evidence is explicit. "
    "Tag presence is name availability only. Even same-digest-evidence is not a runtime/content guarantee."
)


def inventory_evidence(inventory: Inventory) -> dict:
    nodes = []
    for node in inventory.nodes:
        values = {"snapshot": node.snapshot, "capturedAt": node.captured_at, "source": node.source}
        nodes.append({"name": node.name, "schedulable": node.schedulable, **values,
                      "missingEvidence": [key for key, value in values.items() if value is None]})
    return {"inventory": str(inventory.path), "nodes": nodes}


def _coverage(inventory: Inventory, image: str) -> dict:
    result = {"presentNodes": [], "missingNodes": [], "unassessedNodes": []}
    for node in inventory.schedulable_nodes:
        field = ("unassessedNodes" if node.references is None else
                 "presentNodes" if image in node.references else "missingNodes")
        result[field].append(node.name)
    return result


def _compare_record_sets(before: dict, after: dict) -> dict:
    """Compare complete recorded groupings, ignoring record identity/multiplicity."""
    sides = {}
    for label, tag in (("before", before), ("after", after)):
        sides[label] = tag_record_set_evidence(tag["records"])
    comparison = "unassessed"
    if any(side["status"] == "unassessed" for side in sides.values()):
        reason = "Matching tag records lack same-repository repoDigests; temporal record-set comparison is unassessed."
    else:
        same = sides["before"]["repoDigestSets"] == sides["after"]["repoDigestSets"]
        comparison = "same-digest-evidence" if same else "different-digest-evidence"
        reason = ("Complete snapshots have the same distinct recorded tag record digest sets." if same else
                  "Complete snapshots have different distinct recorded tag record digest sets.")
    return {**sides, "comparison": comparison, "reason": reason}


def _tag_record_set_transitions(changes: list[dict]) -> list[dict]:
    """Group existing complete temporal pairs across all retained captured nodes."""
    tags = {}
    for change in changes:
        for tag in change["tags"]:
            cohorts, unassessed = tags.setdefault(tag["image"], ({}, []))
            pair = tag["recordSetComparison"]
            if any(pair[side]["status"] != "complete" for side in ("before", "after")):
                unassessed.append(change["node"])
                continue
            key = tuple(tuple(tuple(digests) for digests in pair[side]["repoDigestSets"])
                        for side in ("before", "after"))
            cohorts.setdefault(key, []).append(change["node"])
    return [{"image": image, "cohorts": [
                {"beforeRepoDigestSets": [list(digests) for digests in before],
                 "afterRepoDigestSets": [list(digests) for digests in after], "nodes": sorted(nodes)}
                for (before, after), nodes in sorted(cohorts.items())],
             "unassessedNodes": sorted(unassessed)}
            for image, (cohorts, unassessed) in sorted(tags.items())]


def _tag_record_set_participation(old: dict, new: dict, changes: list[dict], gaps: list[dict]) -> list[dict]:
    """Partition all declared nodes per observed tag; reference existing evidence."""
    captured_tags = {
        side: {name: {image for image in node.references if not parse_reference(image).digest}
               for name, node in nodes.items() if node.references is not None}
        for side, nodes in (("before", old), ("after", new))}
    images = {image for nodes in captured_tags.values() for tags in nodes.values() for image in tags}
    pairs = {(change["node"], tag["image"]): tag["recordSetComparison"]
             for change in changes for tag in change["tags"]}
    missing = {gap["node"]: ("both" if len(gap["missingSnapshots"]) == 2 else
                             gap["missingSnapshots"][0] + "Only") for gap in gaps}
    rows = []
    for image in sorted(images):
        row = {"image": image, "completePairNodes": [], "incompletePairNodes": [],
               "missingCaptureNodes": {"beforeOnly": [], "afterOnly": [], "both": []},
               "capturedTagAbsenceNodes": {"beforeOnly": [], "afterOnly": [], "both": []},
               "nodesAdded": [], "nodesRemoved": []}
        for name in sorted(old.keys() | new.keys()):
            # Lifecycle/capture gaps take precedence: never assign tag presence to unknown sides.
            if name not in old:
                row["nodesAdded"].append(name)
            elif name not in new:
                row["nodesRemoved"].append(name)
            elif name in missing:
                row["missingCaptureNodes"][missing[name]].append(name)
            elif (name, image) in pairs:
                pair = pairs[name, image]
                complete = all(pair[side]["status"] == "complete" for side in ("before", "after"))
                row["completePairNodes" if complete else "incompletePairNodes"].append(name)
            else:
                absent = [side for side in ("before", "after") if image not in captured_tags[side][name]]
                key = "both" if len(absent) == 2 else absent[0] + "Only"
                row["capturedTagAbsenceNodes"][key].append(name)
        rows.append(row)
    return rows


def _tag_availability_evidence(old: dict, new: dict, participation: list[dict],
                               changes: list[dict], gaps: list[dict]) -> list[dict]:
    """Describe name evidence on each side without synthesizing temporal pairs."""
    captures = {side: {name: f"/inventoryDiff/{side}/nodes/{i}" for i, name in enumerate(nodes)}
                for side, nodes in (("before", old), ("after", new))}
    references = {name: [] for name in old.keys() | new.keys()}
    for key, items in (("nodesAdded", sorted(new.keys() - old.keys())),
                       ("nodesRemoved", sorted(old.keys() - new.keys())),
                       ("imageChanges", [row["node"] for row in changes]),
                       ("unassessedComparisons", [row["node"] for row in gaps])):
        for i, name in enumerate(items):
            references[name].append(f"/inventoryDiff/{key}/{i}")
    result = []
    for tag in participation:
        image = tag["image"]
        pairs = set(tag["completePairNodes"] + tag["incompletePairNodes"])
        rows = []
        for name in sorted((old.keys() | new.keys()) - pairs):
            row = {"node": name}
            refs = list(references[name])
            for side, nodes in (("before", old), ("after", new)):
                node = nodes.get(name)
                capture_ref = captures[side].get(name)
                matches = []
                if node is None:
                    state = "node-not-declared"
                elif node.references is None:
                    state = "missing-capture"
                elif image in node.references:
                    state = "captured-present"
                    matches = [{"imageIndex": index, "field": field, "reference": raw}
                               for index, field, raw in sorted({
                                   (match["imageIndex"], match["field"], match["reference"])
                                   for match in node.references[image]})]
                else:
                    state = "captured-absent"
                row[side] = {"state": state, "captureRef": capture_ref, "matches": matches}
                if capture_ref is not None:
                    refs.append(capture_ref)
            row["evidenceRefs"] = sorted(refs)
            rows.append(row)
        cohorts = {}
        for row in rows:
            key = (row["before"]["state"], row["after"]["state"])
            cohorts.setdefault(key, []).append(row["node"])
        result.append({"image": image, "nodes": rows, "cohorts": [
            {"beforeState": before, "afterState": after, "nodes": sorted(nodes)}
            for (before, after), nodes in sorted(cohorts.items())]})
    return result


def _node_tag_evidence_index(names: set[str], participation: list[dict],
                             changes: list[dict], availability: list[dict]) -> list[dict]:
    """Transpose existing participation memberships, pointing to authoritative rows."""
    evidence = {(change["node"], tag["image"]): f"/inventoryDiff/imageChanges/{i}/tags/{j}"
                for i, change in enumerate(changes) for j, tag in enumerate(change["tags"])}
    evidence.update({(row["node"], tag["image"]): f"/inventoryDiff/tagAvailabilityEvidence/{i}/nodes/{j}"
                     for i, tag in enumerate(availability) for j, row in enumerate(tag["nodes"])})
    nodes = {name: [] for name in sorted(names)}
    for i, tag in enumerate(participation):
        for field in ("completePairNodes", "incompletePairNodes", "missingCaptureNodes",
                      "capturedTagAbsenceNodes", "nodesAdded", "nodesRemoved"):
            values = tag[field]
            groups = values.items() if isinstance(values, dict) else [(None, values)]
            for side, members in groups:
                bucket = field + ("." + side if side else "")
                path = field + ("/" + side if side else "")
                for j, name in enumerate(members):
                    nodes[name].append({"image": tag["image"], "participationBucket": bucket,
                                        "participationRef": f"/inventoryDiff/tagRecordSetParticipation/{i}/{path}/{j}",
                                        "evidenceRefs": [evidence[name, tag["image"]]]})
    return [{"node": name, "tags": sorted(tags, key=lambda tag: tag["image"])}
            for name, tags in nodes.items()]


def compare_inventories(before: Inventory, after: Inventory, required_images: set[str]) -> dict:
    if before.cluster != after.cluster:
        raise InputError("baseline and current inventories must declare the same cluster")
    old = {node.name: node for node in before.nodes}
    new = {node.name: node for node in after.nodes}
    captures = {side: {node.name: f"/inventoryDiff/{side}/nodes/{i}"
                       for i, node in enumerate(inventory.nodes)}
                for side, inventory in (("before", before), ("after", after))}
    schedulability = []
    changes = []
    unassessed = []
    for name in sorted(old.keys() & new.keys()):
        a, b = old[name], new[name]
        if a.schedulable != b.schedulable:
            schedulability.append({"node": name, "before": a.schedulable, "after": b.schedulable})
        if a.references is None or b.references is None:
            unassessed.append({"node": name, "missingSnapshots": [
                label for label, node in (("before", a), ("after", b)) if node.references is None]})
            continue
        tags = []
        old_tags, new_tags = tag_digest_evidence(a), tag_digest_evidence(b)
        for image in sorted(old_tags.keys() & new_tags.keys()):
            old_digests, old_complete = old_tags[image]["repoDigests"], old_tags[image]["complete"]
            new_digests, new_complete = new_tags[image]["repoDigests"], new_tags[image]["complete"]
            comparison = "unassessed"
            if old_complete and new_complete:
                comparison = "same-digest-evidence" if old_digests == new_digests else "digest-evidence-changed"
            tags.append({"image": image, "beforeRepoDigests": old_digests,
                         "afterRepoDigests": new_digests, "comparison": comparison,
                         "recordEvidence": {
                             side: {"captureRef": captures[side][name], "records": evidence[image]["records"]}
                             for side, evidence in (("before", old_tags), ("after", new_tags))},
                         "recordSetComparison": _compare_record_sets(old_tags[image], new_tags[image])})
        changes.append({"node": name,
                        "addedReferences": sorted(b.references.keys() - a.references.keys()),
                        "removedReferences": sorted(a.references.keys() - b.references.keys()), "tags": tags})
    # Include requested-but-absent images as well as observed references, so that
    # after.missingNodes is directly comparable with current tag/digest load output.
    images = set(required_images)
    for inventory in (before, after):
        for node in inventory.schedulable_nodes:
            if node.references is not None:
                images.update(node.references)
    participation = _tag_record_set_participation(old, new, changes, unassessed)
    availability = _tag_availability_evidence(old, new, participation, changes, unassessed)
    return {
        "schemaVersion": 1, "before": inventory_evidence(before), "after": inventory_evidence(after),
        "evidenceNote": EVIDENCE_NOTE,
        "nodesAdded": sorted(new.keys() - old.keys()), "nodesRemoved": sorted(old.keys() - new.keys()),
        "schedulabilityChanges": schedulability, "imageChanges": changes,
        "tagRecordSetTransitions": _tag_record_set_transitions(changes),
        "tagRecordSetParticipation": participation,
        "tagAvailabilityEvidence": availability,
        "nodeTagEvidenceIndex": _node_tag_evidence_index(old.keys() | new.keys(), participation, changes, availability),
        "unassessedComparisons": unassessed,
        "coverage": [{"image": image, "before": _coverage(before, image), "after": _coverage(after, image)}
                     for image in sorted(images)],
    }
