# UAT report Artifact — section contract and template

The published Artifact is the deliverable a QA lead, TL or PM reads. It is produced on every
run by default. Load `Skill: artifact-design` before authoring; this file fixes the **content
contract** and the mechanics that keep a re-run on the same URL. Design choices (palette, type)
follow the design skill and the target repo's identity, not this file.

## Contents

- Section contract (order is fixed)
- Title, description, favicon
- Same-URL redeploy
- Theme and layout rules
- Data hygiene
- HTML skeleton

## Section contract (order is fixed)

1. **Verdict header** — overall `PASS` / `FAIL` / `BLOCKED` / `UNSCOPED`; ticket(s) or PR with
   links; environment (name, project, region); build (revision, image sha, PR merge sha); run
   date/time (UTC); driver lanes used; run number (re-runs increment).
   Verdict rule: any `FAIL` → `FAIL`; else any `BLOCKED` → `BLOCKED`; else `PASS`;
   an unscoped run is `UNSCOPED` regardless, with the case tally shown.
2. **Context Frame** — the frame from Step 1, verbatim shape (use case, parent epic,
   predecessors, reproduction path, prior UAT re-executed / considered).
3. **Access manifest summary** — the manifest rows with values replaced by locators; never a
   secret; `needs person` rows show who supplied them (role, not name, if the person prefers).
4. **Criteria table** — one row per criterion: id · text · source (`spec §8` / `ticket AC` /
   `PR body` / `epic` / `prior UAT <record>`) · kind (`NEW` / `RERUN` / `REGRESSION`) · lane ·
   verdict pill · evidence link(s) into the gallery.
5. **Deviations table** — spec said · PR delivered · consequence for UAT · status (`accepted` /
   `finding F-n`).
6. **Findings** — severity-ranked (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`), each with:
   id, title, confidence 1–10, criterion(s) affected, reproduction steps (numbered), observed vs
   expected, evidence links, suggested owner (role). The validator's findings and the drift
   findings are merged here with their origin tagged.
7. **Evidence gallery** — grouped by lane then case: screenshots and device captures embedded
   (data URIs), each with the file name as caption; recordings linked by file name (a viewer
   cannot download, so the path under `docs/uat/` is the reference); request/response pairs and
   contract-check JSON in `<pre>` blocks inside `overflow:auto` containers.
8. **Terraform drift** — the drift table (resource · kind · declared · live · consequence) or
   the literal sentence "Not applicable: the repository contains no Terraform."
9. **Footer** — run record path (`docs/uat/UAT-…/`), `run.json` sha, the skill version
   (plugin version), and "Findings not yet posted" / "Posted to <ticket>, <PR> on <date>".

## Title, description, favicon

- `<title>`: `<KEY> <Short Feature Name> UAT` — for example `MH-2185 Renewal Deep Link UAT`;
  an epic run: `<EPIC-KEY> <Epic Name> UAT`; unscoped: `<Slug> UAT (unscoped)`. No colons, no
  explainer after a dash.
- `description` (publish parameter): one sentence — environment, build, verdict, case tally.
- `favicon`: `"🧪"` on the first publish only; omit on redeploys.

## Same-URL redeploy

- `run.json` stores `artifact_url` after the first publish.
- A re-run reads it and passes `url: <artifact_url>` to the `Artifact` tool; before publishing,
  `action: "read"` on that URL so the publish builds on the live version (the tool refuses a
  publish to an unread artifact).
- The file path is always `RUN_DIR/report.html`. Never write a second report file for the same
  scope — that claims a new URL.
- Label each publish with the run number (`label: "Run 2"`).

## Theme and layout rules

- Tokens on bare `:root` (full light palette), redefined under
  `@media (prefers-color-scheme: dark)` guarded as `:root:not([data-theme="light"])`, and again
  under `:root[data-theme="dark"]`. `body { background: var(--bg); color: var(--fg); }`.
- Verdict pills use semantic tokens (`--ok`, `--warn`, `--fail`, `--muted`) separate from the
  accent; both themes keep contrast legible.
- Tables and `<pre>` blocks live inside `.scroll { overflow-x: auto; }`; the body never scrolls
  sideways. `font-variant-numeric: tabular-nums` on numeric columns.
- Gallery images: `max-width: 100%`, captions under each, grouped with `gap`, no per-item
  margins. Downscale embedded PNGs to ≤ 1600 px wide; the page stays under 16 MB.
- `<pre>` content is HTML-escaped. No external scripts are needed; do not load any.

## Data hygiene

- **Only the embedded artifacts passed `collect_evidence.py`.** Every other section of this page
  is composed here by hand — the verdict header, Context Frame, manifest summary, criteria and
  deviations tables, findings, and the terraform drift rows — out of data the collector never
  saw. `check_api_contract`'s `actual` values quote a live response verbatim, the validator's
  output quotes deployed configuration, and the manifest summary carries the identity
  `check_infra_cli` reported. Run SKILL.md Step 8's pre-publish grep over the finished file and
  read every hit. Re-check the rendered
  page for `Bearer `, `@`-shaped emails outside the allow-list, and phone shapes before
  publishing; the collector's per-artifact redaction counts are shown in the gallery captions.
- Screenshots that contain a contact card or PII are cropped or excised before capture, and the
  caption says so (the Marketplace records set this precedent).
- Test identities appear by role (`staff admin (QA identity)`), not by email, unless the person
  allow-listed the identity.

## HTML skeleton

Author from this shape; design tokens and type are yours to choose per the design skill.

```html
<title>MH-2185 Renewal Deep Link UAT</title>
<style>
  :root { --bg:#…; --fg:#…; --muted:#…; --accent:#…; --ok:#…; --warn:#…; --fail:#…; --line:#…; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#…; --fg:#…; /* … */ } }
  :root[data-theme="dark"] { --bg:#…; --fg:#…; /* … */ }
  body { background: var(--bg); color: var(--fg); margin: 0; }
  .scroll { overflow-x: auto; }
  .pill { display:inline-block; padding:.15em .6em; border-radius:999px; font-weight:600; }
  .pill.pass { background: var(--ok); } .pill.fail { background: var(--fail); } .pill.blocked { background: var(--warn); }
  /* … */
</style>
<main>
  <header><!-- 1. Verdict header --></header>
  <section id="context"><!-- 2. Context Frame --></section>
  <section id="access"><!-- 3. Manifest summary --></section>
  <section id="criteria"><div class="scroll"><table><!-- 4 --></table></div></section>
  <section id="deviations"><div class="scroll"><table><!-- 5 --></table></div></section>
  <section id="findings"><!-- 6 --></section>
  <section id="evidence"><!-- 7: figures per lane/case; <pre> inside .scroll --></section>
  <section id="drift"><!-- 8 --></section>
  <footer><!-- 9 --></footer>
</main>
```
