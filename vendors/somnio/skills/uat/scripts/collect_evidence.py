#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Store one evidence artifact in a /uat run record: stable name, redaction, manifest entry.

Standard library only. Every artifact enters docs/uat/<run>/evidence/ through
this script so naming is sequential and collision-free, secrets and PII are
redacted from text/JSON captures before they touch the working tree, and
evidence/manifest.json carries the sha256, size, lane, case and redaction
counts of each file. The patterns are the table in references/evidence-spec.md.

Binary media (png, jpg, gif, webp, mp4, webm, mov) is copied byte-for-byte
under a size cap; its PII is the capturer's responsibility (crop before
capture). Containers and rendered documents (har, har.gz, zip, tar, gz, pdf,
sqlite) are REFUSED because they cannot be redacted here — extract the text
first. A text file larger than --max-text-bytes is refused rather than
partially redacted. The run directory must live under <repo>/docs/uat/
(override with --allow-outside-uat-tree for scratch runs), evidence/ and
manifest.json must not be symlinks, artifacts are written through private
temp files, and the manifest sequence is taken under an O_EXCL lock so
parallel lanes never collide.

Usage:
    uv run collect_evidence.py --run-dir docs/uat/UAT-MH-2185-2026-09-03 --lane api --case C3 \
        --slug pipeline-response --file /tmp/resp.json [--env develop] [--build b6fb841] \
        [--keep-email staff-test@example.com] [--stdin-name capture.txt]

    cat capture.txt | uv run collect_evidence.py --run-dir … --lane web --case C1 --slug console --stdin-name console.txt

stdout: JSON manifest entry for the stored artifact
stderr: redaction counts and the final path
exit:   0 stored · 1 refused (name exists, bad lane, oversize, unreadable) · 2 usage
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

LANES = ("api", "web", "mobile", "db", "infra", "manifest", "validator")
# Binary media is copied byte-for-byte: it cannot be text-redacted, and the capturer
# owns cropping PII out of a screenshot before it is handed over.
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov"}
# Containers and PDFs are REFUSED: a .har/.har.gz carries cookies and auth headers, a
# .zip hides anything, a PDF is rendered text — none can be redacted here. Extract the
# text (a .har is JSON; hand it over as .har.json) or excerpt it before collecting.
REFUSED_EXT = {".har", ".har.gz", ".tar.gz", ".gz", ".zip", ".tar", ".tgz", ".pdf", ".sqlite", ".db"}
# 20 MB: larger text captures are almost always a full log dump, which should
# be excerpted by hand rather than redacted blindly.
DEFAULT_MAX_TEXT_BYTES = 20 * 1024 * 1024
# 200 MB: a screen recording of a long flow; anything larger is a mistake (a raw
# simulator capture), and the report cannot embed it anyway.
DEFAULT_MAX_BINARY_BYTES = 200 * 1024 * 1024
# Manifest lock: parallel lanes collecting at once must not interleave the
# read-modify-write of manifest.json. Ten seconds covers a slow disk; beyond
# that a stale lock from a killed run is more likely than a live writer.
LOCK_WAIT_S = 10.0

SENSITIVE_HEADERS = ("authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
                     "x-amz-security-token", "x-goog-api-key")
SENSITIVE_KEY_RE = re.compile(
    r"^(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret|refresh[_-]?token|"
    r"access[_-]?token|id[_-]?token|session(id)?|auth(orization)?|cookie|credentials?)$", re.I)

PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # NOT anchored to start-of-line: a header inside a timestamped log line, a
    # `curl -v` trace ("< set-cookie: ..."), or a wrapped console capture is the
    # common case, and a `^` anchor let every one of those through.
    ("header", re.compile(r"(?i)(\b(" + "|".join(re.escape(h) for h in SENSITIVE_HEADERS) + r")\"?\s*[:=]\s*\"?)([^\"\r\n]+)"),
     r"\1<redacted>"),
    ("bearer", re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 <redacted>"),
    ("jwt", re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), "<redacted-jwt>"),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<redacted-key>"),
    ("cloud_key", re.compile(r"\b(AIza[0-9A-Za-z\-_]{30,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "<redacted-key>"),
    ("connection_string", re.compile(r"\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s\"']+"), r"\1://<redacted>"),
    # Credential-bearing URL query parameters: signed-URL families (GCS V4, S3 presigned,
    # CloudFront, Azure SAS) plus the OAuth/API-key names that show up in captured calls.
    ("url_credential_param", re.compile(
        r"([?&](?:X-Goog-Signature|X-Goog-Credential|X-Goog-Algorithm|X-Amz-Signature|X-Amz-Credential|"
        r"X-Amz-Security-Token|X-Amz-Algorithm|Key-Pair-Id|Policy|sig|se|sp|sr|sv|skoid|sktid|skt|ske|sks|skv|"
        r"signature|token|access_token|refresh_token|id_token|api[_-]?key|apikey|client_secret|client_id|"
        r"code|auth|authorization|session(?:_?id)?|password|passwd|secret|key)=)[^&\s\"']+", re.I),
     r"\1<redacted>"),
    # Plaintext key=value / key: value assignments, the shape env dumps, .env captures and
    # log lines use (PASSWORD=hunter2, export API_KEY=…, db_password: …). The JSON walker
    # only covers parsed documents, so without this a text capture publishes the secret.
    # The lookahead keeps an already-redacted value from being rewritten.
    ("plaintext_assignment", re.compile(
        r"(?i)\b((?:[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|"
        r"private[_-]?key|client[_-]?secret|access[_-]?key|credentials?|auth|"
        r"cookie|session(?:_?id)?|sid|jsessionid)[A-Za-z0-9_.-]*)"
        r"\s*[:=]\s*)(?!<redacted)(\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"),
     r"\1<redacted>"),
    ("phone", re.compile(r"(?<![\w/.-])(\+?\d{1,3}[\s.-]?)?(\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"), "<phone-redacted>"),
]
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redact_text(text: str, keep_emails: set[str]) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pattern, repl in PATTERNS:
        text, n = pattern.subn(repl, text)
        if n:
            counts[name] = n

    def email_sub(m: re.Match) -> str:
        return m.group(0) if m.group(0).lower() in keep_emails else "<email-redacted>"

    text, n = EMAIL_RE.subn(email_sub, text)
    emails = sum(1 for m in EMAIL_RE.finditer(text) if m.group(0).lower() in keep_emails)
    if n - emails:
        counts["email"] = n - emails
    return text, counts


# A {"name": "Cookie", "value": "..."} pair carries the secret under a neutral key, so
# key-matching alone never fires. This is the shape HAR/OpenAPI/Playwright traces use for
# headers and cookies — and the very shape this module tells callers to convert a HAR into.
PAIR_NAME_FIELDS = ("name", "key", "header", "field", "param")
PAIR_VALUE_FIELDS = ("value", "val", "content")


def _is_sensitive_name(value: object) -> bool:
    return isinstance(value, str) and bool(
        SENSITIVE_KEY_RE.match(value.strip()) or value.strip().lower() in SENSITIVE_HEADERS
    )


def redact_json(node: object, keep_emails: set[str], counts: dict[str, int]) -> object:
    if isinstance(node, dict):
        # Pair form first: if a name-ish field names a secret, its sibling value is one.
        name_field = next((f for f in PAIR_NAME_FIELDS if _is_sensitive_name(node.get(f))), None)
        if name_field is not None:
            out = {}
            for k, v in node.items():
                if k in PAIR_VALUE_FIELDS:
                    out[k] = "<redacted>"
                    counts["json_pair"] = counts.get("json_pair", 0) + 1
                else:
                    out[k] = redact_json(v, keep_emails, counts)
            return out
        out = {}
        for k, v in node.items():
            if isinstance(k, str) and (SENSITIVE_KEY_RE.match(k) or k.lower() in SENSITIVE_HEADERS):
                out[k] = "<redacted>"
                counts["json_key"] = counts.get("json_key", 0) + 1
            else:
                out[k] = redact_json(v, keep_emails, counts)
        return out
    if isinstance(node, list):
        return [redact_json(v, keep_emails, counts) for v in node]
    if isinstance(node, str):
        text, c = redact_text(node, keep_emails)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return text
    return node


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:40] or "artifact"


def read_capped(stream, cap: int) -> bytes | None:
    """Read at most `cap` bytes; return None when the source exceeds it (never buffer it all)."""
    data = stream.read(cap + 1)
    return None if len(data) > cap else data


def inside_uat_tree(run_dir: Path) -> bool:
    """A run record lives under <repo>/docs/uat/; anything else is a wrong --run-dir."""
    parts = run_dir.resolve().parts
    return any(parts[i] == "docs" and parts[i + 1] == "uat" for i in range(len(parts) - 1))


class ManifestLock:
    """O_EXCL lock file next to the manifest so parallel lanes serialize the sequence number."""

    def __init__(self, evidence: Path) -> None:
        self.path = evidence / ".manifest.lock"
        self.fd: int | None = None

    def __enter__(self) -> "ManifestLock":
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{self.path} held for more than {LOCK_WAIT_S:.0f}s — "
                                       "another collector is stuck, or a killed run left the lock; remove it by hand")
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--case", required=True, help="criterion id (C3, R1, F2, M2) or 'run'")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--file", help="source file; omit to read stdin")
    parser.add_argument("--stdin-name", default="capture.txt", help="name (for the extension) when reading stdin")
    parser.add_argument("--env", default="")
    parser.add_argument("--build", default="")
    parser.add_argument("--keep-email", action="append", default=[], help="allow-listed QA identity email")
    parser.add_argument("--max-text-bytes", type=int, default=DEFAULT_MAX_TEXT_BYTES)
    parser.add_argument("--max-binary-bytes", type=int, default=DEFAULT_MAX_BINARY_BYTES)
    parser.add_argument("--allow-outside-uat-tree", action="store_true",
                        help="accept a --run-dir that is not under <repo>/docs/uat/ (scratch runs only)")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not args.allow_outside_uat_tree and not inside_uat_tree(run_dir):
        print(f"error: {run_dir} is not under a docs/uat/ tree — run records live in the target repo; "
              "pass --allow-outside-uat-tree for a scratch run", file=sys.stderr)
        return 1
    evidence = run_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence / "manifest.json"
    if manifest_path.is_symlink() or evidence.is_symlink():
        print("error: evidence/ or manifest.json is a symlink — refusing to write through it", file=sys.stderr)
        return 1

    # Read the source with a hard cap BEFORE anything else: never buffer an unbounded
    # stream, and refuse container formats that cannot be redacted.
    name_for_ext = Path(args.file).name if args.file else args.stdin_name
    suffixes = [s.lower() for s in Path(name_for_ext).suffixes]
    last1 = suffixes[-1] if suffixes else ".txt"
    last2 = "".join(suffixes[-2:]) if len(suffixes) >= 2 else ""
    # Check BOTH forms against the refusal set. Deriving one extension first and then
    # testing it let `.tar.gz` slip through: the joined form was not in the set and the
    # single form was never consulted, so a gzip archive was utf-8 "redacted" and stored
    # as text. Refusing on either form closes that for every compound suffix.
    if last1 in REFUSED_EXT or last2 in REFUSED_EXT:
        ext = last2 if last2 in REFUSED_EXT else last1
        print(f"error: {ext} artifacts are refused — they cannot be redacted (HAR/zip carry cookies and auth headers). "
              "Extract the text (a HAR is JSON: save it as .har.json) or excerpt it, then collect that", file=sys.stderr)
        return 1
    # Accepted: keep the compound form only where it is meaningful to the naming
    # convention (`.http.json`), otherwise the single suffix.
    ext = last2 if last2 == ".http.json" else last1
    is_binary = ext in BINARY_EXT
    cap = args.max_binary_bytes if is_binary else args.max_text_bytes
    if args.file:
        src = Path(args.file)
        if not src.is_file():
            print(f"error: {src} is not a file", file=sys.stderr)
            return 1
        with src.open("rb") as fh:
            data = read_capped(fh, cap)
        source = str(src)
    else:
        data = read_capped(sys.stdin.buffer, cap)
        source = "<stdin>"
    if data is None:
        print(f"error: artifact exceeds {cap} bytes ({'binary' if is_binary else 'text'} cap); "
              "excerpt or downscale it first", file=sys.stderr)
        return 1

    counts: dict[str, int] = {}
    keep = {e.lower() for e in args.keep_email}
    if is_binary:
        payload, kind = data, "binary"
    else:
        text = data.decode("utf-8", errors="replace")
        if ext.endswith(".json"):
            try:
                parsed = json.loads(text)
                redacted = json.dumps(redact_json(parsed, keep, counts), indent=2, ensure_ascii=False) + "\n"
                kind = "json"
            except json.JSONDecodeError:
                redacted, counts = redact_text(text, keep)
                kind = "text"
        else:
            redacted, counts = redact_text(text, keep)
            kind = "text"
        payload = redacted.encode("utf-8")

    # Sequence + manifest update under a lock so parallel lanes never collide, and the
    # artifact is written to a private temp file then renamed (no predictable temp names).
    try:
        with ManifestLock(evidence):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else []
            except (OSError, json.JSONDecodeError) as exc:
                print(f"error: manifest unreadable ({exc}); fix or remove it before storing more evidence", file=sys.stderr)
                return 1
            if not isinstance(manifest, list):
                print("error: manifest.json must be a JSON array", file=sys.stderr)
                return 1
            seq = max((int(e.get("seq", -1)) for e in manifest), default=-1) + 1
            case = re.sub(r"[^A-Za-z0-9]+", "", args.case) or "run"
            target = evidence / f"{seq:02d}-{args.lane}-{case}-{slugify(args.slug)}{ext}"
            if target.exists() or target.is_symlink():
                print(f"error: {target.name} already exists — the sequence is corrupt; inspect manifest.json", file=sys.stderr)
                return 1
            fd, tmp_name = tempfile.mkstemp(dir=evidence, prefix=".artifact-", suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_name, target)

            entry = {
                "seq": seq, "file": target.name, "lane": args.lane, "case": args.case, "slug": slugify(args.slug),
                "kind": kind, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                "redactions": counts, "source": source,
                "captured_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "env": args.env, "build": args.build,
            }
            manifest.append(entry)
            fd, tmp_name = tempfile.mkstemp(dir=evidence, prefix=".manifest-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(manifest, indent=2) + "\n")
            os.replace(tmp_name, manifest_path)
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(entry))
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(f"stored {target} ({kind}, {len(payload)} bytes) — redactions: {summary}", file=sys.stderr)
    if args.lane == "api" and kind != "binary" and "header" not in counts and "bearer" not in counts and "json_key" not in counts:
        print("note: an API capture with zero auth redactions — confirm the call was authenticated and headers were captured",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
