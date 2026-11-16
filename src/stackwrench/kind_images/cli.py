"""Command line entry point. Never executes generated argv or user content."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .inputs import InputError
from .planner import build_report


def _render_evidence_node(node: dict) -> str:
    values = " ".join(f"{key}={json.dumps(node[key]) if node[key] is not None else 'MISSING'}"
                      for key in ("snapshot", "capturedAt", "source"))
    return (f"{node['name']} schedulable={str(node['schedulable']).lower()} {values} "
            f"missingEvidence={json.dumps(node['missingEvidence'])}")


def render_diff(diff: dict) -> list[str]:
    lines = ["\nInventory comparison (explicit baseline -> current):", "  " + diff["evidenceNote"]]
    for side in ("before", "after"):
        evidence = diff[side]
        lines.append(f"  {side}: {json.dumps(evidence['inventory'])}")
        for node in evidence["nodes"]:
            lines.append("    " + _render_evidence_node(node))
    lines.append("  Nodes added: " + (", ".join(diff["nodesAdded"]) or "(none)"))
    lines.append("  Nodes removed: " + (", ".join(diff["nodesRemoved"]) or "(none)"))
    for change in diff["schedulabilityChanges"]:
        lines.append(f"  Schedulability {change['node']}: {change['before']} -> {change['after']}")
    for change in diff["imageChanges"]:
        lines.append(f"  Image comparison {change['node']}:")
        lines.extend(f"    Added reference: {image}" for image in change["addedReferences"])
        lines.extend(f"    Removed reference: {image}" for image in change["removedReferences"])
        for tag in change["tags"]:
            lines.append(f"    {tag['image']}: {tag['comparison']} "
                         f"{json.dumps(tag['beforeRepoDigests'])} -> {json.dumps(tag['afterRepoDigests'])}")
            records = tag["recordSetComparison"]
            lines.append(f"      Temporal tag record sets: {records['comparison']}: {records['reason']}")
            for side in ("before", "after"):
                evidence = records[side]
                lines.append(f"        {side}: {evidence['status']} repoDigestSets={json.dumps(evidence['repoDigestSets'])} "
                             f"missingRecordCount={evidence['missingRecordCount']}")
            for side in ("before", "after"):
                evidence = tag["recordEvidence"][side]
                lines.append(f"      Temporal tag record evidence {side}: captureRef={json.dumps(evidence['captureRef'])} "
                             f"records={json.dumps(evidence['records'])}")
            lines.append("      Supplied record positions only; no cross-capture record identity, runtime content or freshness inferred.")
    for tag in diff["tagRecordSetTransitions"]:
        lines.append(f"  Temporal tag record-set transitions {tag['image']} (retained captured nodes, including schedulable=false):")
        for cohort in tag["cohorts"]:
            lines.append(f"    Complete temporal tag record-set cohort: nodes={json.dumps(cohort['nodes'])} "
                         f"beforeRepoDigestSets={json.dumps(cohort['beforeRepoDigestSets'])} "
                         f"afterRepoDigestSets={json.dumps(cohort['afterRepoDigestSets'])}")
        lines.append(f"    Unassessed temporal tag record-set nodes: {json.dumps(tag['unassessedNodes'])}; "
                     "see per-node temporal comparisons; partial pairs cannot establish equality or difference.")
    for tag in diff["tagRecordSetParticipation"]:
        lines.append(f"  Observed tag temporal participation {tag['image']} (all declared nodes, including schedulable=false):")
        lines.append(f"    Complete retained pair nodes: {json.dumps(tag['completePairNodes'])}; see per-node temporal comparisons.")
        lines.append(f"    Incomplete retained pair nodes: {json.dumps(tag['incompletePairNodes'])}; see per-node temporal comparisons.")
        lines.append("    Missing capture nodes (tag availability unknown on missing sides): "
                     + json.dumps(tag["missingCaptureNodes"]))
        lines.append("    Captured tag absence nodes (two captures required): "
                     + json.dumps(tag["capturedTagAbsenceNodes"]))
        lines.append(f"    Lifecycle node references (no tag presence claim): nodesAdded={json.dumps(tag['nodesAdded'])} "
                     f"nodesRemoved={json.dumps(tag['nodesRemoved'])}")
        lines.append("    Participation evidence: see imageChanges, unassessedComparisons, before/after capture evidence and nodesAdded/nodesRemoved; "
                     "beforeOnly/afterOnly/both identify missing or absent sides; no new tag transition or workload intent inferred.")
    for tag in diff["tagAvailabilityEvidence"]:
        lines.append(f"  Observed tag side availability {tag['image']} (nodes without a retained tag pair, including schedulable=false):")
        for node in tag["nodes"]:
            lines.append(f"    Side availability node={node['node']} before={node['before']['state']} after={node['after']['state']}")
            for side in ("before", "after"):
                evidence = node[side]
                lines.append(f"      Side availability {side}: captureRef={json.dumps(evidence['captureRef'])} "
                             f"matches={json.dumps(evidence['matches'])}")
            lines.append(f"      Side availability evidenceRefs={json.dumps(node['evidenceRefs'])}")
        for cohort in tag["cohorts"]:
            lines.append(f"    Observed tag availability state cohort: beforeState={cohort['beforeState']} "
                         f"afterState={cohort['afterState']} nodes={json.dumps(cohort['nodes'])}")
        lines.append("    State cohorts reference the per-node side evidence only; unknown/lifecycle pairs "
                     "do not imply tag gains or losses, content transitions or workload intent.")
        lines.append("    Side availability note: captured states describe exact tag names in supplied captures only. "
                     "Unknown sides do not assert availability; no tag transition or workload intent inferred.")
    for node in diff["nodeTagEvidenceIndex"]:
        lines.append(f"  Observed tag evidence index node={json.dumps(node['node'])} (all declared nodes, including schedulable=false):")
        for tag in node["tags"]:
            lines.append(f"    {tag['image']}: participationBucket={tag['participationBucket']} "
                         f"participationRef={json.dumps(tag['participationRef'])} "
                         f"evidenceRefs={json.dumps(tag['evidenceRefs'])}")
        if not node["tags"]:
            lines.append("    (no observed tags)")
    lines.append("  Index pointers reference existing report rows only; no new availability, tag gains/losses, comparisons or workload intent inferred.")
    for gap in diff["unassessedComparisons"]:
        lines.append(f"  UNASSESSED COMPARISON {gap['node']}: missing snapshots {', '.join(gap['missingSnapshots'])}")
    lines.append("  Coverage (supplied schedulable nodes only):")
    for row in diff["coverage"]:
        lines.append(f"    {row['image']}: {json.dumps(row['before'])} -> {json.dumps(row['after'])}")
    return lines


def render_text(report: dict) -> str:
    lines = [f"Cluster: {report['cluster']}",
             "Nodes: " + ", ".join(node["name"] for node in report["nodes"])]
    if report.get("mode") == "inventory-only":
        lines.append("Inventory-only comparison: no workload readiness assessed; load proposals require workload intent.")
    if report["excludedNodes"]:
        lines.append("Excluded (inventory schedulable=false): " + ", ".join(report["excludedNodes"]))
    for node in report["unassessedNodes"]:
        lines.append(f"UNASSESSED NODE {node['node']}: {node['reason']}")
    evidence = report["currentInventoryEvidence"]
    lines.append("\nCurrent inventory evidence (caller-supplied):")
    lines.append("  " + evidence["evidenceNote"])
    lines.append("  Inventory: " + json.dumps(evidence["inventory"]))
    for node in evidence["nodes"]:
        lines.append("  " + _render_evidence_node(node))
    lines.append("\nImage overview:")
    if not report["imageOverview"]:
        lines.append("  (no assessed images)")
    for group in report["imageOverview"]:
        lines.append(f"\nImage: {group['image']} ({group['referenceType']})")
        availability = {node["node"]: node for node in group["nodes"]}
        for node in group["nodes"]:
            present = "unknown" if node["present"] is None else str(node["present"]).lower()
            lines.append(f"  {node['node']}:")
            lines.append(f"    Availability: {node['availability']} (present={present}): {node['availabilityReason']}")
        if "tagDigestEvidence" in group:
            evidence = group["tagDigestEvidence"]
            lines.append(f"  Tag repository-digest evidence: {evidence['comparison']}: {evidence['reason']}")
            lines.append(f"    Known difference between complete nodes: {str(evidence['knownDifference']).lower()}")
            lines.append("    " + evidence["note"])
            groupings = evidence["recordSetComparison"]
            lines.append(f"  Cross-node tag record sets: {groupings['comparison']}: {groupings['reason']}")
            for cohort in groupings["cohorts"]:
                lines.append(f"    Complete tag record-set cohort: nodes={json.dumps(cohort['nodes'])} "
                             f"repoDigestSets={json.dumps(cohort['repoDigestSets'])}")
            lines.append(f"    Unassessed tag record-set nodes: {json.dumps(groupings['unassessedNodes'])}")
            for node in groupings["nodes"]:
                lines.append(f"    Node record sets {node['node']}: {node['status']} "
                             f"repoDigestSets={json.dumps(node['repoDigestSets'])} "
                             f"missingRecordCount={node['missingRecordCount']}: {node['reason']}")
            for node in evidence["nodes"]:
                lines.append(f"    {node['node']}: {node['status']} repoDigests={json.dumps(node['repoDigests'])}")
                lines.append("      " + node["reason"])
                comparison = node["recordComparison"]
                lines.append(f"      Within-node tag records: {comparison['comparison']}: {comparison['reason']}")
                lines.append(f"        Known difference between complete records: {str(comparison['knownDifference']).lower()}")
                for record in node["records"]:
                    lines.append(f"      record {record['imageIndex']}: {json.dumps(record['repoDigests'])}")
        lines.append("  Uses:")
        for item in group["uses"]:
            lines.append(f"  [{item['assessmentIndex']}] {item['kind']}/{item['namespace']}/{item['name']} "
                         f"{item['containerType']}/{item['container']}"
                         f" [{item['source']} doc {item['document']}, {item['path']}]")
            lines.append(f"    {item['image']} | {item['effectivePullPolicy']} ({item['policySource']})")
            if item["policyRisk"]:
                lines.append(f"    Policy risk: {item['policyRisk']}")
            for node in item["nodeVerdicts"]:
                lines.append(f"    {node['node']}: {node['verdict']}")
                if node["reason"] not in (availability[node["node"]]["availabilityReason"], item["policyRisk"]):
                    lines.append(f"      {node['reason']}")
    for item in report["unassessed"]:
        lines.append(f"UNASSESSED {item['kind']}/{item['name']} {item['path']}: {item['reason']}")
    lines.append("\nProposed load argv (JSON only; NOT executed):")
    for item in report["loadPlan"]:
        lines.append(json.dumps(item["argv"]))
        if item["requiresPolicyReview"]:
            lines.append("  Policy review required: loading does not fix Always.")
    if not report["loadPlan"]:
        lines.append("  (none)")
    for item in report["loadPlanOmissions"]:
        lines.append(f"NO LOAD PROPOSAL {item['image']}: {item['reason']}")
    if "inventoryDiff" in report:
        lines.extend(render_diff(report["inventoryDiff"]))
    lines.append("\nAssumptions / scope:")
    lines.extend("  " + assumption for assumption in report["assumptions"])
    lines.append("\nSummary: " + json.dumps(report["summary"], sort_keys=True))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stackwrench kind-images", description="Join rendered Kubernetes YAML with explicit per-node crictl JSON snapshots, or compare two inventories, offline.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--manifests", nargs="+", type=Path, help="rendered multi-document YAML file(s); may be omitted with --baseline-inventory")
    parser.add_argument("--inventory", required=True, type=Path, help="versioned node-to-snapshot JSON inventory")
    parser.add_argument("--baseline-inventory", type=Path, help="compare this explicit earlier inventory with --inventory; no live access")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument("--check", action="store_true", help="exit 1 unless workload intent is assessed, every check is local-available and nothing is unassessed")
    args = parser.parse_args(argv)
    if args.manifests is None and args.baseline_inventory is None:
        parser.error("--manifests or --baseline-inventory is required")
    try:
        report = build_report(args.manifests, args.inventory, args.baseline_inventory)
    except InputError as exc:
        print(f"stackwrench kind-images: {exc}", file=sys.stderr)
        return 2
    try:
        print(json.dumps(report, indent=2, ensure_ascii=True) if args.json else render_text(report))
    except BrokenPipeError:
        return 2
    return 1 if args.check and not report["summary"]["localUsePossibleEverywhere"] else 0
