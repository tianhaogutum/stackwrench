"""The CLI reads one file and writes a report; no subprocesses or network code."""

import argparse
import json
import sys

from . import __version__
from .audit import audit, validate_target
from .compare import compare_profiles, render_comparison
from .input import InputError, read_document
from .impact import dependency_path_query, impact_query, render_dependency_path, render_impact


def render_human(report):
    lines = [f"stackwrench podman-audit {report['toolVersion']} | {report['provider']} {report['providerVersion']}",
             f"Evidence: {report['catalogVersion']} (retrieved {report['evidenceRetrievedAt']})",
             f"Version coverage: {report['versionCoverage']}; compatibility: not established.",
             report["scope"]["notice"], ""]
    for finding in report["findings"]:
        lines.extend([
            f"{finding['severity'].upper()} {finding['ruleId']} {finding['category']} {finding['path']}",
            f"  service={json.dumps(finding['service'], ensure_ascii=True)} confidence={finding['confidence']}",
            "  " + finding["reportedBehavior"],
            "  Details: " + json.dumps(finding["details"], sort_keys=True, ensure_ascii=True),
            "  Evidence: " + finding["evidence"]["evidenceUrl"],
            "  Observed versions: " + (", ".join(finding["evidence"]["observedVersions"]) or "structural / version independent"),
            "  Status at retrieval: " + finding["evidence"]["statusAtRetrieval"],
            "  Context: " + finding["evidence"]["observationContext"],
            "  Uncertainty: " + finding["evidence"]["uncertainty"],
            "  Suggestion: " + finding["suggestion"],
        ])
        if "explanation" in finding:
            explanation = finding["explanation"]
            target, scope, source = (explanation[key] for key in ("target", "evidenceScope", "localEvidence"))
            lines.extend([
                "  Checked fields: " + (", ".join(explanation["fieldPaths"]) or "none (target-wide coverage check)"),
                *["  Declaration: " + item["path"] + " — " + (
                    f"line {item['line']}, column {item['column']}" if item["declaration"] == "present" else
                    "absent declaration" if item["declaration"] == "absent" else "source location unavailable")
                  for item in explanation["fieldLocations"]],
                "  Why: " + explanation["reason"],
                f"  Selected target: {target['provider']} {target['version']}",
                f"  Evidence match: {scope['versionMatch']} ({scope['kind']}); observed provider: {scope['provider'] or 'not applicable'}",
                f"  Local evidence: {source['package']}/{source['resource']}#{source['jsonPointer']} sha256={source['sha256']}",
                "  Advice scope: review steps requiring separate runtime verification; not a runtime success guarantee.",
            ])
        lines.append("")
    if not report["findings"]:
        lines.append("No findings in the evaluated rules; this is not a compatibility guarantee.")
    if report["scope"]["notEvaluated"]:
        lines.append("Fields not evaluated for schema or provider compatibility:")
        lines.extend("  " + item["path"] for item in report["scope"]["notEvaluated"])
    summary = report["summary"]
    lines.append(f"\n{summary['services']} services; {summary['error']} errors; {summary['warning']} warnings.")
    return "\n".join(lines)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Keep the JSON mode usable even for malformed CLI arguments. Avoid
        # echoing untrusted argument values into terminal diagnostics.
        raise InputError("invalid-arguments", "Invalid CLI arguments; use --help for usage.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    json_mode = "--json" in argv
    parser = Parser(prog="stackwrench podman-audit", description="Offline evidence-scoped audit of one Compose file; never starts containers.",
                    allow_abbrev=False)
    parser.add_argument("file", help="One UTF-8 Compose YAML or .json file")
    parser.add_argument("--provider", help="podman-compose (required for single-target audit)")
    parser.add_argument("--provider-version", help="Exact SemVer, e.g. 1.6.0 (required for single-target audit)")
    parser.add_argument("--compare-profiles", nargs=2, metavar=("LEFT", "RIGHT"),
                        help="Compare two explicit local builtin/custom profile files; replaces target flags")
    parser.add_argument("--impact-service", metavar="SERVICE",
                        help="Query declared reverse dependencies; conflicts with path/target/comparison/explain/fail-on flags")
    parser.add_argument("--dependency-path", nargs=2, metavar=("SOURCE", "TARGET"),
                        help="Query a shortest declared dependency path; conflicts with impact/target/comparison/explain/fail-on flags")
    parser.add_argument("--json", action="store_true", help="Deterministic machine-readable report")
    parser.add_argument("--explain", action="store_true",
                        help="Explain checked fields with source locations, target/version match and local catalog evidence")
    parser.add_argument("--fail-on", choices=("error", "warning", "never"),
                        help="Exit 1 at this finding threshold (default: error). Input errors always exit 2.")
    parser.add_argument("--version", action="version", version=__version__)
    try:
        args = parser.parse_args(argv)
        if args.dependency_path is not None:
            if (args.impact_service is not None or args.provider is not None or args.provider_version is not None
                    or args.compare_profiles is not None or args.explain or args.fail_on is not None):
                parser.error("Dependency path queries cannot be combined with other modes or audit flags.")
            report = dependency_path_query(read_document(args.file), *args.dependency_path)
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) if args.json else render_dependency_path(report))
            return 0
        if args.impact_service is not None:
            if (args.provider is not None or args.provider_version is not None or args.compare_profiles is not None
                    or args.explain or args.fail_on is not None):
                parser.error("Impact queries cannot be combined with audit/comparison flags.")
            report = impact_query(read_document(args.file), args.impact_service)
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) if args.json else render_impact(report))
            return 0
        if args.compare_profiles:
            if args.provider is not None or args.provider_version is not None:
                parser.error("Target flags cannot be combined with profile comparison.")
            left, right = (read_document(path) for path in args.compare_profiles)
            report = compare_profiles(read_document(args.file), left, right, explain=args.explain)
            human = render_comparison
        else:
            if args.provider is None or args.provider_version is None:
                parser.error("Single-target audit requires provider and version.")
            validate_target(args.provider, args.provider_version)
            report = audit(read_document(args.file), provider=args.provider, provider_version=args.provider_version,
                           explain=args.explain)
            human = render_human
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) if args.json else human(report))
        if args.fail_on == "never":
            return 0
        return int(report["summary"]["error"] > 0 or
                   (args.fail_on == "warning" and report["summary"]["warning"] > 0))
    except InputError as exc:
        if json_mode:
            print(json.dumps({"schemaVersion": 1, "assessment": "not-evaluated", "error": exc.detail},
                             sort_keys=True, ensure_ascii=True))
        else:
            print(f"Error [{exc.detail['code']}] {exc.detail['path']}: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
