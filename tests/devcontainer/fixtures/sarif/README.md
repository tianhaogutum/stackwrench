# SARIF validation fixture

Unmodified OASIS SARIF 2.1.0 JSON Schema (with Errata 01), pinned to upstream
commit `a560296ca8c921f3bdb8d4a8db57ab83dae968a7`:

https://github.com/oasis-tcs/sarif-spec/blob/a560296ca8c921f3bdb8d4a8db57ab83dae968a7/sarif-2.1/schema/sarif-schema-2.1.0.json

SHA-256: `c3b4bb2d6093897483348925aaa73af03b3e3f4bd4ca38cef26dcb4212a2682e`.
The sibling LICENSE.md is the upstream repository's unmodified license terms at
that commit. OASIS attribution and governing policies remain with the fixture;
the project's MIT license does not relicense it. This is test data, excluded from
the npm distribution. Tests validate the complete schema with locked dev-only
Ajv 6.12.6 and its draft-04 metaschema; no schema is fetched during tests or CLI use.
