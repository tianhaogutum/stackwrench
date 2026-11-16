# Known divergences from the Node.js implementation

`stackwrench devcontainer` is a Python port of the Node.js `devcontainer-wirecheck`
tool, which used the npm packages `jsonc-parser` 3.3.1 and `yaml` 2.9.1. Report
formats, rule IDs, messages, locations and exit codes are meant to be identical.
The known exceptions:

- **Program name and version.** Usage and error text say `stackwrench devcontainer`
  instead of `devcontainer-wirecheck`; `--help` starts with a `usage:` line;
  `--version` and the SARIF `tool.driver.version` report the stackwrench version.
  The SARIF `tool.driver.name` stays `devcontainer-wirecheck`.
- **YAML syntax errors.** PyYAML (a YAML 1.1 parser) replaces npm `yaml`. Whether a
  document is rejected (`parse-error`) and the first problem's position match for
  ordinary inputs, but the *message* text of syntax errors comes from PyYAML
  (formatted with npm `yaml`'s "at line L, column C" excerpt). Duplicate keys,
  unresolved explicit tags and multiple documents reproduce npm `yaml`'s own
  messages. PyYAML rejects some characters npm `yaml` accepts (C0/C1 control
  characters and U+FFFE/U+FFFF anywhere in the file), treats U+0085/U+2028/U+2029 as
  line breaks inside scalars, and may accept or reject a few tab/flow-context edge
  cases differently.
- **YAML 1.2 semantics.** Scalars always resolve with the YAML 1.2 core schema. A
  `%YAML 1.1` directive, which switches npm `yaml` to its YAML 1.1 schema, is not
  honoured. Explicit `!!binary`/`!!timestamp` values are not decoded (tagged nodes
  are rejected as `unsupported-yaml` anyway), so a malformed `!!timestamp` reports
  `unsupported-yaml` rather than `parse-error`.
- **YAML locations of unusual nodes.** Node positions come from PyYAML's event
  marks, adjusted to npm `yaml` conventions (content start after anchors/tags;
  empty values after the indicator and following spaces). A key without any value
  (`? key` with no `:`, or `{key}` in flow style) is a present null value here,
  while npm `yaml` treats it as absent, so its location is not marked `inferred`.
- **Parse-error excerpts with astral characters.** Line/column in locations and
  SARIF are UTF-16 code units, as before. Only the column and caret inside a YAML
  parse-error *message* are counted in code points.
- **JSONC parser.** The port reproduces `jsonc-parser`'s scanner, error recovery,
  error codes and first-error offsets for the API the checker uses (`parseTree`,
  `getNodeValue`, `findNodeAtLocation`, `printParseErrorCode`). Nesting deeper than
  about 12,000 levels raises an internal error (exit 2) where V8 would overflow at a
  similar depth.
- **Library-only edge cases.** `check(daemon="")` reports `hostAssumption: null`
  where JavaScript omitted the key. Invalid UTF-8 is replaced with U+FFFD per
  Python's decoder, which follows the same maximal-subpart rule as Node.js in all
  common cases. Filesystem error messages are rebuilt in Node.js style
  (`ENOENT: no such file or directory, stat '/path'`); the system call named may
  differ.
