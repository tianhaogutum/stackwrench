"""Pure report assembly; a proposed command is data, never an action."""

from collections import Counter
from pathlib import Path

from .diff import EVIDENCE_NOTE, compare_inventories, inventory_evidence
from .evidence import compare_tag_nodes, tag_digest_evidence
from .inputs import InputError
from .inventory import Node, read_inventory_details
from .manifests import read_manifests


ASSUMPTIONS = [
    "Inventory is the trust boundary: snapshots must be complete, fresh and from the declared cluster/nodes.",
    "Declared schedulable nodes without snapshots are unassessed, not missing-image evidence; undeclared nodes cannot be discovered.",
    "All supplied schedulable nodes are evaluated; node selectors, affinity, taints and placement are not simulated.",
    "Default policies model initial API creation only; existing persisted policies and admission mutation may differ.",
    "Tag matches establish name availability only, not identical content across nodes or host.",
    "CRI IDs, tags and repoDigests are not inferred equivalent; missing repoDigest means digest presence is not established.",
    "Snapshot id, size, uid, username, spec and pinned fields are metadata only; matching uses repoTags/repoDigests.",
    "No credential verification, architecture checks, runtime checks or deployment-success guarantee is provided.",
    "Commands require an independently verified matching host image and node/cluster names; nothing is executed.",
]

MISSING_SNAPSHOT_REASON = "No snapshot supplied; local image availability is unassessed."
ALWAYS_RISK = "Always requests a runtime pull; a local copy alone does not remove the registry requirement."


def image_overview(containers: list[dict], nodes: list[Node]) -> list[dict]:
    """Group exact references, sharing availability but never merging use policies."""
    groups = {}
    for index, container in enumerate(containers):
        image = container["canonicalImage"]
        if image not in groups:
            # Availability depends only on this canonical reference and inventory.
            groups[image] = {
                "image": image, "referenceType": container["referenceType"],
                "nodes": [{key: row[key] for key in
                           ("node", "present", "availability", "availabilityReason", "matches")}
                          for row in container["nodes"]],
                "uses": [],
            }
        groups[image]["uses"].append({
            "assessmentIndex": index,
            **{key: value for key, value in container.items()
               if key not in ("canonicalImage", "referenceType", "nodes")},
            "policyRisk": ALWAYS_RISK if container["effectivePullPolicy"] == "Always" else None,
            "nodeVerdicts": [{key: row[key] for key in ("node", "verdict", "reason")}
                             for row in container["nodes"]],
        })
    if any(group["referenceType"] == "tag" for group in groups.values()):
        evidence = {node.name: tag_digest_evidence(node) for node in nodes}
        for image, group in groups.items():
            if group["referenceType"] == "tag":
                group["tagDigestEvidence"] = compare_tag_nodes(image, nodes, evidence)
    return [groups[image] for image in sorted(groups)]


def build_report(manifests: list[Path] | None, inventory: Path, baseline_inventory: Path | None = None) -> dict:
    if not manifests and baseline_inventory is None:
        raise InputError("without manifests, --baseline-inventory is required for inventory-only comparison")
    current = read_inventory_details(inventory)
    cluster, nodes, excluded = current.cluster, current.schedulable_nodes, current.excluded
    containers, unassessed = read_manifests(manifests) if manifests else ([], [])
    inventory_diff = None
    if baseline_inventory is not None:
        baseline = read_inventory_details(baseline_inventory)
        inventory_diff = compare_inventories(baseline, current, {c["canonicalImage"] for c in containers})
    proposed = {}
    digest_omissions = {}
    verdicts = Counter()
    for container in containers:
        matrix = []
        key = container["canonicalImage"]
        policy = container["effectivePullPolicy"]
        policy_risk = ALWAYS_RISK if policy == "Always" else None
        missing = []
        for node in nodes:
            matches = node.references.get(key, []) if node.references is not None else []
            present = bool(matches) if node.references is not None else None
            if present is None:
                availability = "unassessed"
                availability_reason = MISSING_SNAPSHOT_REASON
            elif present:
                availability = "present"
                availability_reason = "Exact normalized reference is present in the supplied snapshot."
            else:
                availability = "missing"
                availability_reason = "This snapshot does not establish local presence of the exact normalized reference."
                missing.append(node.name)
                if container["referenceType"] == "digest":
                    availability_reason += " No matching repository@digest in repoDigests; tags and CRI IDs are not proof."
            if present is None:
                verdict = "unassessed"
                reason = "Local-use verdict cannot be assessed without a snapshot."
            elif policy == "Always":
                verdict = "will-request-registry"
                reason = ALWAYS_RISK
            elif present:
                verdict = "local-available"
                reason = "Exact normalized reference is present in the supplied snapshot."
            elif policy == "Never":
                verdict = "missing-required-local"
                reason = "Never forbids pulling and this snapshot does not establish local presence."
            else:
                verdict = "would-pull-if-scheduled"
                reason = "IfNotPresent would request a pull if scheduled here, based on this snapshot."
            if present is False and container["referenceType"] == "digest":
                reason += " No matching repository@digest in repoDigests; tags and CRI IDs are not proof."
            matrix.append({"node": node.name, "present": present, "matches": matches,
                           "availability": availability, "availabilityReason": availability_reason,
                           "policyRisk": policy_risk,
                           "verdict": verdict, "reason": reason})
            verdicts[verdict] += 1
        container["nodes"] = matrix
        if missing:
            if container["referenceType"] == "digest":
                digest_omissions.setdefault(key, set()).update(missing)
            else:
                plan = proposed.setdefault(key, {"nodes": set()})
                plan["nodes"].update(missing)
    # A later Always occurrence may be fully cached, yet still uses the same
    # image as an earlier missing occurrence: policy warnings are per image.
    always_images = {c["canonicalImage"] for c in containers if c["effectivePullPolicy"] == "Always"}
    load_plan = []
    for image, plan in sorted(proposed.items()):
        names = sorted(plan["nodes"])
        load_plan.append({
            "image": image, "missingNodes": names,
            "requiresPolicyReview": image in always_images,
            "argv": ["kind", "load", "docker-image", image, "--name", cluster, "--nodes", ",".join(names)],
            "note": "Proposal only; requires this tag on the host. Loading cannot fix an Always registry requirement.",
        })
    report = {
        "schemaVersion": 2, "cluster": cluster,
        "currentInventoryEvidence": {**inventory_evidence(current), "evidenceNote": EVIDENCE_NOTE},
        "nodes": [{"name": node.name, "snapshot": node.snapshot} for node in nodes],
        "unassessedNodes": [{"node": node.name, "reason": MISSING_SNAPSHOT_REASON}
                            for node in nodes if node.references is None],
        "excludedNodes": excluded, "assumptions": ASSUMPTIONS,
        "assessments": containers, "unassessed": unassessed,
        "imageOverview": image_overview(containers, nodes),
        "loadPlan": load_plan,
        "loadPlanOmissions": [{"image": image, "missingNodes": sorted(names),
                              "reason": "Digest presence is not established; tag-loading cannot prove digest equivalence."}
                             for image, names in sorted(digest_omissions.items())],
        "summary": {
            "containers": len(containers), "nodeChecks": sum(verdicts.values()),
            "verdicts": dict(sorted(verdicts.items())), "unassessed": len(unassessed),
            "localUsePossibleEverywhere": bool(containers) and not unassessed and
                verdicts["local-available"] == sum(verdicts.values()),
        },
    }
    if inventory_diff is not None:
        report["inventoryDiff"] = inventory_diff
    if not manifests:
        report["mode"] = "inventory-only"
    return report
