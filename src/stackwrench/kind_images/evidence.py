"""Recorded tag/digest associations, separate from exact-reference availability."""

from .inventory import Node
from .references import parse_reference


TAG_EVIDENCE_NOTE = "Recorded repository-digest evidence only; no runtime content-equivalence or freshness guarantee."


def tag_digest_evidence(node: Node) -> dict[str, dict]:
    """Index same-repository digests on every distinct matching CRI tag record."""
    refs = node.references or {}
    parsed = {key: parse_reference(key) for key in refs}
    record_digests = {}
    for key, matches in refs.items():
        ref = parsed[key]
        if ref.digest:
            for match in matches:
                record_digests.setdefault((match["imageIndex"], ref.repository), set()).add(key)
    result = {}
    for key, matches in refs.items():
        ref = parsed[key]
        if not ref.digest:
            records = [{"imageIndex": index,
                        "repoDigests": sorted(record_digests.get((index, ref.repository), set()))}
                       for index in sorted({match["imageIndex"] for match in matches})]
            result[key] = {
                "records": records,
                "repoDigests": sorted({digest for record in records for digest in record["repoDigests"]}),
                "complete": all(record["repoDigests"] for record in records),
            }
    return result


def compare_tag_records(records: list[dict]) -> dict:
    """Compare recorded sets within one node, independently of its union."""
    complete_sets = {tuple(record["repoDigests"]) for record in records if record["repoDigests"]}
    known_difference = len(complete_sets) > 1
    comparison = "unassessed"
    if any(not record["repoDigests"] for record in records):
        reason = "Some matching records lack same-repository repoDigests; complete-record differences remain in knownDifference."
    elif len(records) < 2:
        reason = "At least two matching tag records with same-repository repoDigests are required for within-node comparison."
    else:
        comparison = "different-digest-evidence" if known_difference else "same-digest-evidence"
        reason = ("Complete matching records have different recorded repository-digest sets." if known_difference else
                  "All matching records have the same recorded repository-digest set.")
    return {"comparison": comparison, "knownDifference": known_difference, "reason": reason}


def tag_record_set_evidence(records: list[dict]) -> dict:
    """Retain distinct nonempty sets and gaps, ignoring record identity/multiplicity."""
    sets = sorted({tuple(record["repoDigests"]) for record in records if record["repoDigests"]})
    missing = sum(not record["repoDigests"] for record in records)
    return {"status": "complete" if sets and not missing else "unassessed",
            "repoDigestSets": [list(digests) for digests in sets], "missingRecordCount": missing}


def _compare_node_record_sets(rows: list[dict]) -> dict:
    """Compare whole complete node groupings, never partial sets or record IDs."""
    nodes = [{"node": row["node"], "reason": row["reason"],
              **tag_record_set_evidence(row["records"])} for row in rows]
    comparison = "unassessed"
    if any(node["status"] == "unassessed" for node in nodes):
        reason = "Some nodes lack complete tag record groupings; partial groupings cannot establish equality or difference."
    elif len(nodes) < 2:
        reason = "At least two nodes with complete tag record groupings are required for cross-node comparison."
    else:
        same = all(node["repoDigestSets"] == nodes[0]["repoDigestSets"] for node in nodes[1:])
        comparison = "same-digest-evidence" if same else "different-digest-evidence"
        reason = ("All nodes have the same distinct recorded tag record digest sets." if same else
                  "Complete nodes have different distinct recorded tag record digest sets.")
    cohorts = {}
    for node in nodes:
        if node["status"] == "complete":
            key = tuple(tuple(digests) for digests in node["repoDigestSets"])
            cohorts.setdefault(key, []).append(node["node"])
    return {"comparison": comparison, "reason": reason, "nodes": nodes,
            "cohorts": [{"repoDigestSets": [list(digests) for digests in key],
                         "nodes": sorted(members)} for key, members in sorted(cohorts.items())],
            "unassessedNodes": sorted(node["node"] for node in nodes if node["status"] == "unassessed")}


def compare_tag_nodes(image: str, nodes: list[Node], evidence: dict[str, dict]) -> dict:
    """Compare complete node unions; retain gaps and established differences separately."""
    rows = []
    complete_sets = set()
    for node in nodes:
        tag = evidence[node.name].get(image)
        complete = tag is not None and tag["complete"]
        if node.references is None:
            reason = "No snapshot supplied; tag digest evidence is unassessed."
        elif tag is None:
            reason = "Exact tag is absent from this snapshot; no tag record to compare."
        elif not complete:
            reason = "At least one matching tag record lacks same-repository repoDigests."
        else:
            reason = "Every matching tag record has same-repository repoDigests."
            complete_sets.add(tuple(tag["repoDigests"]))
        rows.append({"node": node.name, "status": "complete" if complete else "unassessed",
                     "reason": reason, "repoDigests": tag["repoDigests"] if tag else [],
                     "records": tag["records"] if tag else [],
                     "recordComparison": compare_tag_records(tag["records"] if tag else [])})
    known_difference = len(complete_sets) > 1
    comparison = "unassessed"
    if any(row["status"] == "unassessed" for row in rows):
        reason = "Some nodes lack complete tag digest evidence; complete-node differences remain in knownDifference."
    elif len(rows) < 2:
        reason = "At least two nodes with complete tag digest evidence are required for cross-node comparison."
    else:
        comparison = "different-digest-evidence" if known_difference else "same-digest-evidence"
        reason = ("Complete nodes have different recorded repository-digest sets." if known_difference else
                  "All nodes have the same recorded repository-digest set.")
    return {"comparison": comparison, "knownDifference": known_difference,
            "reason": reason, "nodes": rows, "note": TAG_EVIDENCE_NOTE,
            "recordSetComparison": _compare_node_record_sets(rows)}
