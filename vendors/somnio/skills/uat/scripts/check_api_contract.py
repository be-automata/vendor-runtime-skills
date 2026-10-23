#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0,<7"]
# ///
"""Field-by-field check of an API response against the OpenAPI contract, for /uat's API lane.

Inline dependency: `pyyaml`. OpenAPI documents in the target repos are
routinely authored as YAML (`openapi.yaml`, spec §7 fragments), and the
standard library has no YAML parser; a JSON-only checker would silently
exclude the most common contract source. JSON specs need no YAML and are
loaded with the standard library. Everything else is stdlib.

Resolves local `$ref`s (a chain deeper than 20 is UNCHECKABLE, never an empty
schema that passes), walks `allOf`/`anyOf`/`not`, `oneOf` with exactly-one
semantics, `nullable`, arrays (every element checked), nested objects,
`additionalProperties`, `enum`/`const`, `pattern`, string lengths, numeric
bounds and `multipleOf`, `minItems`/`maxItems`/`uniqueItems`, and the
`format`s that matter in practice (uuid, date-time, date, email, uri). Not
covered: discriminator mapping and OpenAPI 3.1 conditional (`if`/`then`)
schemas — state that in the report when a contract uses them. Every extra,
missing, or mistyped field is reported; `required` is honored; a schema-less
response is UNCHECKABLE, never a pass. A request path is matched exactly, or
by suffix only when the stripped prefix is a declared `servers[].url` path.

Usage:
    uv run check_api_contract.py --spec openapi.yaml --path /api/v1/staff/leasing/newlease/pipeline \
        --method GET --status 200 --response resp.json [--media application/json] [--strict-extra]

stdout: JSON {path, method, status, schema, verdict, missing[], extra[], type_mismatch[], format_mismatch[], checked_fields}
        on EVERY path this script reaches, errors and --help included ({verdict: "ERROR", reason})
stderr: the human summary
exit:   0 CONFORMS · 3 UNCHECKABLE (path/status/schema absent) · 4 MISMATCH · 5 usage or unreadable input · 6 internal error

Exit codes 1 and 2 are deliberately NOT this script's. It runs through `uv run`, and uv
exits 1 (and, since 0.12.14, 2 for operational failures) when it cannot install
`pyyaml` or fetch an interpreter, before this file runs at all; Python also exits 1 on
an uncaught exception. Measured 2026-09-24 on uv 0.10.9 and 0.12.18: an unresolvable
dependency, online or offline, exits 1. With MISMATCH on 1, a checker that never ran
read as a contract failure. So: 1 or 2 with no JSON on stdout means "did not run",
never a verdict, and the verdict is read from the JSON, which the exit code confirms.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

EXIT_CONFORMS, EXIT_UNCHECKABLE, EXIT_MISMATCH, EXIT_USAGE, EXIT_INTERNAL = 0, 3, 4, 5, 6


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; 2 belongs to uv here (see the header)."""

    def print_help(self, file=None) -> None:  # type: ignore[override]
        # stdout carries only the JSON verdict; help is for a person.
        super().print_help(sys.stderr)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        # `--help` ends in exit(0), and 0 is CONFORMS. A help request is not a verdict.
        if message:
            print(message, file=sys.stderr, end="")
        if status == 0:
            print(json.dumps({"verdict": "ERROR", "reason": "usage: --help requested; no response was checked"}))
            status = EXIT_USAGE
        sys.exit(status)

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(json.dumps({"verdict": "ERROR", "reason": f"usage: {message}"}))
        print(f"error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


# Nested $ref chains deeper than this are cyclic or pathological, not a real contract.
MAX_REF_DEPTH = 20

FORMAT_RE = {
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "date-time": re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}:?\d{2})?$"),
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "uri": re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:"),
}


class Report:
    def __init__(self) -> None:
        self.missing: list[str] = []
        self.extra: list[str] = []
        self.type_mismatch: list[dict] = []
        self.format_mismatch: list[dict] = []
        self.checked = 0


def load_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def resolve(schema: dict, spec: dict, depth: int = 0) -> dict:
    """Follow local $ref chains. The depth cap is far beyond any real contract and guards
    against a self-referential schema looping forever — and hitting it is an error
    (UNCHECKABLE), never a silently empty schema that would grade CONFORMS."""
    while isinstance(schema, dict) and "$ref" in schema:
        if depth >= MAX_REF_DEPTH:
            raise ValueError(f"$ref chain deeper than {MAX_REF_DEPTH} at {schema['$ref']} — "
                             "cyclic or pathological schema")
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"only local $ref is supported, got {ref}")
        node: object = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"unresolvable $ref {ref}")
            node = node[part]
        schema = node  # type: ignore[assignment]
        depth += 1
    return schema if isinstance(schema, dict) else {}


def json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def type_ok(expected: str | list[str], actual: str) -> bool:
    exp = expected if isinstance(expected, list) else [expected]
    return actual in exp or (actual == "integer" and "number" in exp)


def check(schema: dict, value: object, at: str, spec: dict, rep: Report, strict_extra: bool) -> None:
    schema = resolve(schema, spec)
    if not schema:
        # An empty schema constrains nothing. Returning quietly here graded the
        # response CONFORMS with zero fields checked — "nothing to check" reported as
        # "everything passed", which is the failure this whole script exists to avoid.
        # Only the TOP level raises: an empty schema nested under a checked object is
        # a legitimately unconstrained field, not an uncontracted response.
        if at == "":
            raise ValueError("the resolved response schema is empty — it constrains nothing, "
                             "so the response cannot be graded against it")
        return
    for combo in ("allOf",):
        for sub in schema.get(combo, []) or []:
            check(sub, value, at, spec, rep, strict_extra)
    for combo in ("oneOf", "anyOf"):
        subs = schema.get(combo) or []
        if subs:
            # anyOf: at least one branch conforms. oneOf: EXACTLY one — a value matching two
            # branches is a contract violation, not a pass.
            trials = []
            for sub in subs:
                trial = Report()
                check(sub, value, at, spec, trial, strict_extra)
                trials.append(trial)
            ok = [t for t in trials if not (t.missing or t.extra or t.type_mismatch or t.format_mismatch)]
            if combo == "anyOf" and ok:
                rep.checked += ok[0].checked
                return
            if combo == "oneOf" and len(ok) == 1:
                rep.checked += ok[0].checked
                return
            if combo == "oneOf" and len(ok) > 1:
                rep.format_mismatch.append({"field": at or "$", "expected": "exactly one oneOf branch",
                                            "actual": f"{len(ok)} branches match"})
                rep.checked += ok[0].checked
                return
            best = min(trials, key=lambda t: len(t.missing) + len(t.extra) + len(t.type_mismatch) + len(t.format_mismatch))
            rep.missing += best.missing
            rep.extra += best.extra
            rep.type_mismatch += best.type_mismatch
            rep.format_mismatch += best.format_mismatch
            rep.checked += best.checked
            return
    if "not" in schema:
        trial = Report()
        check(schema["not"], value, at, spec, trial, strict_extra)
        if not (trial.missing or trial.extra or trial.type_mismatch or trial.format_mismatch):
            rep.format_mismatch.append({"field": at or "$", "expected": "must NOT match the `not` schema", "actual": "matches"})

    actual = json_type(value)
    if value is None:
        if schema.get("nullable") or (isinstance(schema.get("type"), list) and "null" in schema["type"]):
            rep.checked += 1
            return
        if "type" in schema:
            rep.type_mismatch.append({"field": at, "expected": schema["type"], "actual": "null"})
        return

    expected = schema.get("type")
    if expected is None and "properties" in schema:
        expected = "object"
    if expected is None and "items" in schema:
        expected = "array"
    if expected is not None:
        rep.checked += 1
        if not type_ok(expected, actual):
            rep.type_mismatch.append({"field": at, "expected": expected, "actual": actual})
            return

    if "enum" in schema and value not in schema["enum"]:
        rep.format_mismatch.append({"field": at, "expected": f"one of {schema['enum']}", "actual": value})
    if "const" in schema and value != schema["const"]:
        rep.format_mismatch.append({"field": at, "expected": f"const {schema['const']!r}", "actual": value})
    fmt = schema.get("format")
    if fmt in FORMAT_RE and isinstance(value, str) and not FORMAT_RE[fmt].match(value):
        rep.format_mismatch.append({"field": at, "expected": f"format {fmt}", "actual": value[:60]})
    # Value constraints the spec can state and a response can violate silently.
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            rep.format_mismatch.append({"field": at, "expected": f"pattern {schema['pattern']}", "actual": value[:60]})
        if "minLength" in schema and len(value) < schema["minLength"]:
            rep.format_mismatch.append({"field": at, "expected": f"minLength {schema['minLength']}", "actual": len(value)})
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            rep.format_mismatch.append({"field": at, "expected": f"maxLength {schema['maxLength']}", "actual": len(value)})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Two spellings, and `True` is an int in Python, so the boolean form must be
        # separated first or `value <= True` silently compares against 1:
        #   OpenAPI 3.0      exclusiveMinimum: true  — a modifier on `minimum`
        #   OpenAPI 3.1      exclusiveMinimum: 5     — the bound itself
        for bound_key, excl_key, low in (("minimum", "exclusiveMinimum", True),
                                         ("maximum", "exclusiveMaximum", False)):
            bound, excl = schema.get(bound_key), schema.get(excl_key)
            if isinstance(excl, bool):  # 3.0 form: `excl` modifies `bound`
                if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                    violated = (value <= bound if excl else value < bound) if low else \
                               (value >= bound if excl else value > bound)
                    if violated:
                        label = f"{bound_key} {bound}" + (" (exclusive)" if excl else "")
                        rep.format_mismatch.append({"field": at, "expected": label, "actual": value})
                continue
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                if (value < bound) if low else (value > bound):
                    rep.format_mismatch.append({"field": at, "expected": f"{bound_key} {bound}", "actual": value})
            if isinstance(excl, (int, float)):  # 3.1 form: `excl` is the bound
                if (value <= excl) if low else (value >= excl):
                    rep.format_mismatch.append({"field": at, "expected": f"{excl_key} {excl}", "actual": value})
        if schema.get("multipleOf") and value % schema["multipleOf"] != 0:
            rep.format_mismatch.append({"field": at, "expected": f"multipleOf {schema['multipleOf']}", "actual": value})
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            rep.format_mismatch.append({"field": at, "expected": f"minItems {schema['minItems']}", "actual": len(value)})
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            rep.format_mismatch.append({"field": at, "expected": f"maxItems {schema['maxItems']}", "actual": len(value)})
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(set(seen)) != len(seen):
                rep.format_mismatch.append({"field": at, "expected": "uniqueItems", "actual": "duplicates present"})

    if actual == "object":
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        addl = schema.get("additionalProperties", True)
        for key in required:
            if key not in value:  # type: ignore[operator]
                rep.missing.append(f"{at}.{key}".lstrip("."))
        for key, sub in props.items():
            if key in value:  # type: ignore[operator]
                check(sub, value[key], f"{at}.{key}".lstrip("."), spec, rep, strict_extra)  # type: ignore[index]
        for key in value:  # type: ignore[union-attr]
            if key not in props:
                if isinstance(addl, dict):
                    check(addl, value[key], f"{at}.{key}".lstrip("."), spec, rep, strict_extra)  # type: ignore[index]
                elif addl is False or strict_extra or props:
                    rep.extra.append(f"{at}.{key}".lstrip("."))
    elif actual == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            for i, elem in enumerate(value):  # type: ignore[arg-type]
                check(items, elem, f"{at}[{i}]", spec, rep, strict_extra)


def find_schema(spec: dict, path: str, method: str, status: str, media: str) -> tuple[dict | None, str]:
    paths = spec.get("paths", {}) or {}
    item = paths.get(path)
    if item is None:
        # Tolerate a server-prefixed path (spec has /staff/x, the call was /api/v1/staff/x) —
        # but only when the stripped prefix is one the spec's `servers[]` declares. An
        # arbitrary prefix (/attacker/orders → /orders) is not the same operation.
        prefixes = set()
        for server in spec.get("servers", []) or []:
            url = server.get("url", "") if isinstance(server, dict) else ""
            prefixes.add("/" + re.sub(r"^[a-z]+://[^/]+", "", url).strip("/"))
        candidates = [p for p in paths if path.endswith(p) and path[: len(path) - len(p)].rstrip("/") in
                      {x.rstrip("/") for x in prefixes}]
        if len(candidates) == 1:
            item = paths[candidates[0]]
            path = candidates[0]
        elif any(path.endswith(p) for p in paths):
            return None, (f"path {path} matches a spec path only by suffix and the prefix is not a declared "
                          "server url — pass the spec path exactly, or add the prefix to servers[]")
    if item is None:
        return None, f"path {path} not in spec"
    op = item.get(method.lower())
    if op is None:
        return None, f"method {method.upper()} not defined for {path}"
    responses = op.get("responses", {}) or {}
    resp = responses.get(status) or responses.get(int(status) if status.isdigit() else status) or responses.get("default")
    if resp is None:
        return None, f"status {status} not defined for {method.upper()} {path}"
    resp = resolve(resp, spec)
    content = resp.get("content", {}) or {}
    body = content.get(media) or next(iter(content.values()), None) if content else resp.get("schema")
    schema = body.get("schema") if isinstance(body, dict) and "schema" in body else body
    if not schema:
        return None, f"no response schema for {method.upper()} {path} {status}"
    ref = schema.get("$ref", "(inline)") if isinstance(schema, dict) else "(inline)"
    return schema, ref


def main() -> int:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, help="OpenAPI document (.yaml/.yml/.json)")
    parser.add_argument("--path", required=True)
    parser.add_argument("--method", default="GET")
    parser.add_argument("--status", default="200")
    parser.add_argument("--response", required=True, help="JSON file holding the response body")
    parser.add_argument("--media", default="application/json")
    parser.add_argument("--strict-extra", action="store_true",
                        help="report undeclared fields even on schemas without `properties`")
    args = parser.parse_args()

    try:
        spec = load_spec(Path(args.spec))
        body = json.loads(Path(args.response).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps({"verdict": "ERROR", "reason": f"unreadable input: {exc}"}))
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(spec, dict):
        print(json.dumps({"verdict": "ERROR", "reason": "spec is not an OpenAPI object"}))
        print("error: spec is not an OpenAPI object", file=sys.stderr)
        return EXIT_USAGE

    schema, ref = find_schema(spec, args.path, args.method, str(args.status), args.media)
    base = {"path": args.path, "method": args.method.upper(), "status": int(args.status) if str(args.status).isdigit() else args.status}
    if schema is None:
        print(json.dumps({**base, "schema": None, "verdict": "UNCHECKABLE", "reason": ref}))
        print(f"UNCHECKABLE: {ref} — a response without a contract cannot PASS; record it as a finding", file=sys.stderr)
        return EXIT_UNCHECKABLE

    rep = Report()
    try:
        check(schema, body, "", spec, rep, args.strict_extra)
    except (KeyError, ValueError) as exc:
        print(json.dumps({**base, "schema": ref, "verdict": "UNCHECKABLE", "reason": str(exc)}))
        print(f"UNCHECKABLE: {exc}", file=sys.stderr)
        return EXIT_UNCHECKABLE
    # Belt and braces for the same class: a non-empty body that produced zero checked
    # fields means the contract said nothing about it. That is UNCHECKABLE, not a pass.
    if rep.checked == 0 and body not in (None, {}, []):
        reason = "the contract constrained no field of this response (0 fields checked)"
        print(json.dumps({**base, "schema": ref, "verdict": "UNCHECKABLE", "reason": reason}))
        print(f"UNCHECKABLE: {reason}", file=sys.stderr)
        return EXIT_UNCHECKABLE

    mismatch = bool(rep.missing or rep.extra or rep.type_mismatch or rep.format_mismatch)
    out = {**base, "schema": ref, "verdict": "MISMATCH" if mismatch else "CONFORMS",
           "missing": rep.missing, "extra": rep.extra, "type_mismatch": rep.type_mismatch,
           "format_mismatch": rep.format_mismatch, "checked_fields": rep.checked}
    print(json.dumps(out, indent=2))
    print(f"{out['verdict']}: {rep.checked} field(s) checked · missing {len(rep.missing)} · extra {len(rep.extra)} · "
          f"type {len(rep.type_mismatch)} · format {len(rep.format_mismatch)}", file=sys.stderr)
    return EXIT_MISMATCH if mismatch else EXIT_CONFORMS


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - any crash must not exit 1 (see the header)
        print(json.dumps({"verdict": "ERROR", "reason": f"internal error: {type(exc).__name__}: {exc}"}))
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL)
