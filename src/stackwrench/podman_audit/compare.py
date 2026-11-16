"""Compare explicit local profiles without promoting absent evidence to support."""

import hashlib
import json

from .audit import audit, catalog, validate_target
from .input import InputError, json_path

JUDGMENTS = ("reported-risk", "supported", "unknown")


def validate_profile(profile):
    """Strict v1 profile contract. Custom rules never override structural checks."""
    def reject(message, *parts):
        raise InputError("invalid-profile", message, json_path(parts))

    if not isinstance(profile, dict):
        reject("Profile must be a mapping.")
    if type(profile.get("schemaVersion")) is not int or profile["schemaVersion"] != 1:
        reject("Profile schemaVersion must be integer 1.", "schemaVersion")
    if profile.get("kind") not in ("builtin", "custom"):
        reject("Profile kind must be builtin or custom.", "kind")
    expected = {"schemaVersion", "kind", "provider", "version"}
    if profile["kind"] == "custom":
        expected.add("rules")
    if set(profile) != expected:
        reject("Profile must contain exactly schemaVersion, kind, provider, version, and rules only for custom profiles.")
    validate_target(profile["provider"], profile["version"])
    if profile["kind"] == "custom":
        if not isinstance(profile["rules"], dict):
            reject("Custom rules must be a mapping.", "rules")
        provider_rules = {key for key, rule in catalog()["rules"].items() if rule["kind"] == "provider-report"}
        for rule_id, assertion in profile["rules"].items():
            if rule_id not in provider_rules:
                reject("Only catalog provider-report rule IDs may be asserted.", "rules", rule_id)
            parts = ("rules", rule_id)
            if isinstance(assertion, dict):
                for key in assertion:
                    if key not in {"judgment", "evidenceReferences", "observationNotes"}:
                        reject("Unsupported custom assertion field.", *parts, key)
                if assertion.get("judgment") not in JUDGMENTS:
                    reject("Assertion requires judgment: reported-risk, supported or unknown.", *parts, "judgment")
                if "evidenceReferences" in assertion:
                    references = assertion["evidenceReferences"]
                    if not isinstance(references, list) or not references:
                        reject("Evidence references must be a nonempty list of nonblank strings.",
                               *parts, "evidenceReferences")
                    for index, reference in enumerate(references):
                        if not isinstance(reference, str) or not reference.strip():
                            reject("Evidence reference must be a nonblank string.",
                                   *parts, "evidenceReferences", index)
                if "observationNotes" in assertion:
                    notes = assertion["observationNotes"]
                    if not isinstance(notes, str) or not notes.strip():
                        reject("Observation notes must be a nonblank string.", *parts, "observationNotes")
            elif assertion not in JUDGMENTS:
                reject("Judgment must be reported-risk, supported or unknown.", "rules", rule_id)


def _profile_metadata(profile, report):
    metadata = {key: profile[key] for key in ("kind", "provider", "version")}
    if profile["kind"] == "builtin":
        metadata["versionCoverage"] = report["versionCoverage"]
    else:
        metadata["versionCoverage"] = "custom-assertions-only"
        # Hash canonical parsed content, not raw file bytes. Key order/format are inert.
        metadata["sha256"] = hashlib.sha256(
            json.dumps(profile, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return metadata


def _assessment(profile, metadata, finding, explain):
    rule_id = finding["ruleId"]
    if profile["kind"] == "custom":
        explicit = rule_id in profile["rules"]
        assertion = profile["rules"].get(rule_id, "unknown")
        result = {
            "judgment": assertion["judgment"] if isinstance(assertion, dict) else assertion,
            "basis": "custom-assertion" if explicit else "custom-unspecified",
            "evidence": None,
            "localEvidence": {"kind": "custom-profile", "sha256": metadata["sha256"],
                              "jsonPointer": "/rules/" + rule_id} if explicit else None,
        }
        if isinstance(assertion, dict):
            # User context stays separate from catalog evidence, without resolving
            # references. Copy the list so report edits cannot alter the profile.
            if "evidenceReferences" in assertion:
                result["evidenceReferences"] = list(assertion["evidenceReferences"])
            if "observationNotes" in assertion:
                result["observationNotes"] = assertion["observationNotes"]
        return result
    matched = finding["details"]["versionMatch"] == "exact"
    result = {
        "judgment": "reported-risk" if matched else "unknown",
        "basis": "builtin-observation" if matched else "builtin-missing-evidence",
        "versionMatch": "exact" if matched else "unknown",
        "evidence": finding["evidence"],
        "localEvidence": finding["explanation"]["localEvidence"],
    }
    if explain:
        result["explanation"] = finding["explanation"]
    return result


def compare_profiles(model, left, right, *, explain=False):
    """Compare two parsed v1 profiles on one model loaded by input.load_document.

    Rows cover applicable provider-report rules only, not every accepted Compose
    field. Custom assertions are independent of the built-in observations.
    """
    for profile in (left, right):
        validate_profile(profile)
    reports = [audit(model, provider=p["provider"], provider_version=p["version"], explain=True)
               for p in (left, right)]
    metadata = [_profile_metadata(p, r) for p, r in zip((left, right), reports)]
    provider_rows = [{(f["path"], f["ruleId"]): f for f in r["findings"]
                      if f["category"] in ("reported-risk", "unverified-compatibility")
                      and f["ruleId"] != "CAT001"} for r in reports]
    capabilities = []
    for key, finding in provider_rows[0].items():
        assessments = [_assessment(p, m, rows[key], explain)
                       for p, m, rows in zip((left, right), metadata, provider_rows)]
        capabilities.append({
            "path": finding["explanation"]["fieldPaths"][0],
            "ruleId": finding["ruleId"], "service": finding["service"],
            "details": dict(finding["details"]),
            "reportedBehavior": finding["reportedBehavior"], "suggestion": finding["suggestion"],
            "left": assessments[0], "right": assessments[1],
            "changed": assessments[0]["judgment"] != assessments[1]["judgment"],
        })
        # Version matching belongs to the two assessments, not the shared trigger.
        capabilities[-1]["details"].pop("versionMatch", None)
    capabilities.sort(key=lambda row: (row["path"], row["ruleId"]))
    structural = [f for f in reports[0]["findings"] if f["category"] == "structural"]
    # Shared graph/source evidence has no selected profile. Keep its explanation
    # only on request, without the arbitrary left audit's selected target.
    for finding in structural:
        if explain:
            finding["explanation"].pop("target", None)
        else:
            finding.pop("explanation", None)
    profile_findings = {}
    for side, profile, report in zip(("left", "right"), (left, right), reports):
        profile_findings[side] = ([f for f in report["findings"] if f["ruleId"] == "CAT001"]
                                  if profile["kind"] == "builtin" else [])
        if not explain:
            for finding in profile_findings[side]:
                finding.pop("explanation", None)
    warnings = (sum(f["severity"] == "warning" for f in structural)
                + sum(len(items) for items in profile_findings.values())
                + sum(any(row[side]["judgment"] != "supported" for side in ("left", "right"))
                      for row in capabilities))
    return {
        "schemaVersion": 1, "tool": reports[0]["tool"], "toolVersion": reports[0]["toolVersion"],
        "assessment": "limited-static-profile-comparison", "compatibility": "not-established",
        "catalogVersion": reports[0]["catalogVersion"], "evidenceRetrievedAt": reports[0]["evidenceRetrievedAt"],
        "profiles": dict(zip(("left", "right"), metadata)),
        "capabilities": capabilities, "structuralFindings": structural, "profileFindings": profile_findings,
        "scope": reports[0]["scope"] | {
            "notice": "Only applicable provider-report rules are compared. Custom support is a local assertion, not built-in observed evidence. Missing evidence stays unknown. No runtime reproduction or compatibility pass.",
        },
        "summary": {"services": reports[0]["summary"]["services"], "capabilities": len(capabilities),
                    "changed": sum(row["changed"] for row in capabilities),
                    "error": sum(f["severity"] == "error" for f in structural), "warning": warnings},
    }


def render_comparison(report):
    lines = [f"stackwrench podman-audit {report['toolVersion']} | profile comparison",
             "Compatibility: not established.", report["scope"]["notice"],
             f"Evidence: {report['catalogVersion']} (retrieved {report['evidenceRetrievedAt']})"]
    for side, profile in report["profiles"].items():
        lines.append(f"{side}: {profile['kind']} {profile['provider']} {profile['version']}; {profile['versionCoverage']}")
    for row in report["capabilities"]:
        lines.extend(["", f"{row['path']} {row['ruleId']} ({'changed' if row['changed'] else 'unchanged'})",
                      f"  {row['left']['judgment']} -> {row['right']['judgment']}",
                      "  " + row["reportedBehavior"], "  Details: " + json.dumps(row["details"], sort_keys=True)])
        for side in ("left", "right"):
            item = row[side]
            lines.append(f"  {side}: {item['basis']}")
            if item["evidence"] is not None:
                lines.extend(["    Evidence: " + json.dumps(item["evidence"], sort_keys=True),
                              "    Local evidence: " + json.dumps(item["localEvidence"], sort_keys=True)])
            elif item["localEvidence"]:
                lines.append("    Custom profile assertion: " + json.dumps(item["localEvidence"], sort_keys=True))
            if "evidenceReferences" in item:
                lines.append("    Custom evidence references: " + json.dumps(item["evidenceReferences"], ensure_ascii=True))
            if "observationNotes" in item:
                lines.append("    Custom observation notes: " + json.dumps(item["observationNotes"], ensure_ascii=True))
            if "explanation" in item:
                lines.append("    Explanation: " + json.dumps(item["explanation"], sort_keys=True))
        lines.append("  Suggestion: " + row["suggestion"])
    for side, findings in [("shared", report["structuralFindings"]), *report["profileFindings"].items()]:
        for finding in findings:
            lines.extend(["", f"{side}: {finding['severity'].upper()} {finding['ruleId']} {finding['path']}",
                          "  " + json.dumps(finding, sort_keys=True, ensure_ascii=True)])
    if report["scope"]["notEvaluated"]:
        lines.append("Fields not evaluated for schema or provider compatibility:")
        lines.extend("  " + item["path"] for item in report["scope"]["notEvaluated"])
    summary = report["summary"]
    lines.append(f"\n{summary['capabilities']} applicable capabilities; {summary['changed']} changed judgments; "
                 f"{summary['error']} errors; {summary['warning']} warnings.")
    return "\n".join(lines)
