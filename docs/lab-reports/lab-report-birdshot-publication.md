<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
<!-- birdshot-lint: allow-legacy-name — a report that cannot name what it renamed is not a record -->

# Publishing a Working Instrument: Rebranding, Licence Audit and History Sanitisation of a Raspberry Pi Camera Application

**Author:** Paul Richeson ([vonglurt](https://github.com/vonglurt)) — contact: paulr@sdf.org
**Date:** 23 August 2026
**Licence:** MIT (see `LICENSE`)
**Project:** `birdshot` — IMX477 bird and sky capture for the Raspberry Pi CM4
**Version at time of writing:** `1.0.0-rc1`
**Predecessor:** `picam` — the same software under a name that could not be published

---

## Abstract

This report documents the preparation of a working, privately developed camera
application for public release: the audit that established what could be
published, the rewrite that made it publishable, and what remains outstanding.
The software itself was not modified in any behavioural way and is not the
subject. The subject is everything *around* the software that made it
unpublishable while the software worked perfectly — a fact the project's own
history illustrates, since all five commits were authored, complete and correct,
and every one of them contained a private network address.

Three findings drove the work. First, a repository can be functionally clean and
still be unpublishable: no secrets, no credentials and no logs were ever
committed here, yet the history carried a private LAN address, a personal home
directory and a specific USB stick's volume label across every commit — an
inventory of one machine on one network, published permanently. Second, the
licence hazard in a permissively licensed project is rarely in the project's own
files; it is in a dependency, and here it is **PyQt5, which is GPL-3.0 or
commercial**, imported by an MIT application. Third, and more usefully, that
hazard was already architecturally contained before anyone looked for it: the
GPL surface is confined to six GUI files, so the entire headless capture path is
copyleft-free — a property discovered rather than designed, and now worth
defending deliberately.

The remedy was a full history rewrite rather than a corrective commit, on the
argument developed in §4 that a fix-going-forward is not a fix when the artefact
being published is the history itself. All five commits were rebuilt, re-signed,
re-attributed and rebranded, and a pre-commit gate now enforces the sanitised
state against the same machine that produced the original leak.

**Index Terms** — software publication, licence compatibility, GPL, history
rewriting, `git-filter-repo`, commit signing, secret scanning, rebranding,
Raspberry Pi, release engineering.

---

## 1. Introduction

The software worked. That is the premise, and it is worth stating first because
it is the reason none of what follows was noticed earlier.

`picam` is an IMX477 capture application for a Raspberry Pi Compute Module 4:
metered auto-exposure in EV space with lux feed-forward, quality gates that
discard frames on measured focus and clipping, a tiered storage cascade that
migrates sealed groups from tmpfs to eMMC to USB with per-file verification
before deletion, EXIF tagging by direct APP1 injection, and a PyQt5 GUI on the
Pi's own display. It was deployed, it ran, and it produced photographs. By the
only test that matters to its operator it was finished.

By the tests that matter to a *published* project it was not started. The name
collided with dozens of repositories and a PyPI package. The copyright line
named an email address rather than a person. Every commit was unsigned. The
deployment scripts hardcoded a specific machine on a specific network. And no
document anywhere in the tree stated what the software depended on or under what
terms — including the dependency that is not permissively licensed.

This report is the record of closing that gap. It is deliberately a report about
*publication* rather than about capture engineering; the camera work is
documented in `README.md` and `docs/GUIDE.md` and is unchanged by any of this.

### 1.1 Method

Every claim below was established by reading the repository — the working tree
*and* every blob in every commit — rather than by recollection. Where a check is
quoted, it is a command that was actually run and whose output is reproduced.
Two findings that were nearly acted upon incorrectly are retained in §7 with the
correction, because a report that records only the conclusions it kept is a
weaker document than one that shows where it turned.

---

## 2. The audit

### 2.1 What was actually in the history

The first question was not "is there a secret in here" but "what is in here at
all". The answer was reassuring and then not.

```sh
git log --all --pretty=format: --name-only | sort -u
```

Twenty-eight files, every one of them `.py`, `.sh` or `.md`, plus `LICENSE` and
`.gitignore`. **No logs, no captured frames, no binaries, no build output had
ever been committed** — the `.gitignore` had excluded `*.jpg`, `*.mp4`,
`index.jsonl` and the capture root from the first commit, and it had held. The
largest blob in the entire history was a 100 KB Python file.

This is the outcome one hopes for and it is worth naming why it happened: the
exclusions were written *before* the first capture session, not after the first
accident. A `.gitignore` added in response to a problem documents the problem;
one added in advance prevents it.

Content was another matter. A pattern sweep across every blob reachable from
every ref — not the working tree, which is the check that misses things —
found each of the following in **all five commits**:

| Finding | Occurrences | What it disclosed |
|---|---|---|
| A private `192.168.x.x` address | `sync.sh`, `mac/pull-photos.sh` | The Pi's address on the author's LAN, and by implication the subnet |
| `<user>@…`, `/home/<user>/…` | 14 lines across 6 files | The account name on both the Pi and, by inference, the Mac |
| `/media/<user>/<label>` | 5 lines incl. two config defaults | A specific removable volume's label |
| `Copyright (c) 2026 paulr@sdf.org` | `LICENSE` + 27 SPDX headers | An email address standing where a legal person should |

The values are given in redacted form above, which is not squeamishness: this
report is published from the repository it audits, and reproducing the literal
address would reintroduce it to exactly the artefact the rewrite removed it
from. `make sanitise` refuses this file if the real strings appear in it, which
is how the first draft was caught.

None of these is a *secret*. Nothing here is a credential, and no key material
was ever present. That is precisely what makes them easy to leave in: there is
no moment of alarm, because nothing feels dangerous. But a private address, an
account name and a volume label together constitute a description of one
machine on one network, and publishing them is publishing that description
permanently and irrevocably to anyone who clones the repository.

### 2.2 The licence position

The project's own files were unambiguous. Every source file already carried
`SPDX-License-Identifier: MIT`; the `LICENSE` was the standard MIT text; and a
search for attribution language — `adapted from`, `based on`, `derived`,
`courtesy`, `stackoverflow`, any other licence identifier — returned exactly one
hit, and it was the English word "derived" in a docstring about tier naming.

**No third-party code is present in the repository.** Every line under `src/`,
`bin/`, `mac/` and `docs/` is original work. That is an unusually clean starting
position and it is the reason the `vendor/` directory described in §5.2 could be
created empty rather than as a salvage operation.

The dependencies were where the problem was.

---

## 3. The finding that mattered: an MIT application on GPL bindings

`birdshot` imports five third-party Python packages. Four are permissive —
picamera2 (BSD-2-Clause), numpy (BSD-3-Clause), simplejpeg (MIT), piexif (MIT).
The fifth is **PyQt5, which Riverbank Computing offers under GPL v3 or a paid
commercial licence, with no permissive option.**

An MIT-licensed application whose GUI is built on GPL bindings is a real and
frequently misunderstood position, and it is worth being precise rather than
alarmed.

**What is not affected.** The MIT grant on birdshot's own files stands. It is
Paul Richeson's to make and nothing about PyQt5 withdraws it. Cloning this
repository, running it having installed PyQt5 yourself, and redistributing this
source are all unaffected, because no PyQt5 code travels with the source.

**What is affected.** *Conveying a combined work* — shipping birdshot and PyQt5
together as one artefact: an SD-card image, a `.deb`, a container, a PyInstaller
bundle. That aggregate must be GPL-3.0. birdshot's files remain MIT inside it,
but the combination cannot be offered under MIT alone, and selling such a bundle
requires a Riverbank commercial licence.

This is the ordinary "permissive application, copyleft toolkit" position. It is
recorded because it is invisible until the day someone publishes a ready-to-flash
image, which is exactly the kind of thing a camera project eventually does.

### 3.1 The mitigation was already in the architecture

The useful part of the finding is what the follow-up check returned:

```sh
grep -rl PyQt5 --include='*.py' src bin
```

Seven files. Five are `src/birdshot/gui/*`. The remaining two are false
positives on inspection: `src/birdshot/__init__.py` mentions PyQt5 only in a
module docstring, and `bin/birdshot-cli` imports it **inside the body of
`t_qt()`**, a function that only `birdshot-cli selftest` ever calls. Importing
the CLI does not import Qt.

Therefore **the entire headless path — capture, metering, auto-exposure, the
storage cascade, EXIF, timelapse — touches no GPL code at all.** A deployment
running `birdshot-cli` with PyQt5 absent is MIT plus permissive dependencies and
can be bundled and redistributed freely.

Nobody designed this. It fell out of keeping the GUI separate from the engine
for testing reasons — the engine runs in its own thread with event callbacks
precisely so it can be driven headless. The licence benefit was free, which is
the argument for now defending it deliberately: `CONTRIBUTING.md` refuses new
GPL dependencies outside `src/birdshot/gui/`, and `make deps` prints the PyQt5
import surface on demand so a regression is visible rather than discovered.

External programs — `ffmpeg`, `rsync`, `ssh` — are invoked as separate
processes. Process invocation is an arm's-length boundary and no licence
propagates across it, which is why `rsync` being GPL-3.0 imposes nothing here
while PyQt5 being GPL-3.0 imposes something real.

---

## 4. Why the history was rewritten rather than corrected

The obvious remedy for §2.1 is a commit that fixes the strings. It is the wrong
remedy, and the reasoning generalises.

A corrective commit fixes the *tip*. `git clone` fetches the *history*. The
private address would have remained in five reachable commits, retrievable by
anyone with the repository and one `git log -p`, and the corrective commit
itself would function as an index — a diff whose left-hand side is the address,
labelled with a message explaining its significance. Publishing a fix for a leak
you are simultaneously publishing is worse than publishing neither.

The counter-argument to rewriting is rewriting's real cost: it invalidates every
commit hash, breaking forks, clones, references and signatures. That cost is
paid by *other people*, and it is why rewriting published history is close to
indefensible.

**This history had never been published.** No remote was configured; the
repository existed on one machine. The cost of rewriting was therefore zero and
the benefit was total, and the decision inverts entirely at the moment of first
push — which is the point of doing it before that moment rather than after.

### 4.1 The rewrite

`git-filter-repo`, over all five commits in one pass: content substitutions,
path renames, and author normalisation via mailmap.

The substitutions were ordered, and one of them required care. Rebranding
`picam` → `birdshot` by plain substring replacement would have corrupted
`picamera2`, the camera dependency, in every file that imports it. The rule was
therefore anchored on a word boundary:

```
regex:\bpicam\b==>birdshot
```

`picam-cli`, `picam/config`, `picam-data` and `picam.desktop` all match, because
`-`, `/`, `.` are non-word characters. `picamera2` does not, because `e` is a
word character and no boundary exists there. `Picamera2` is additionally
protected by case. This was verified against real source lines before the rewrite
rather than after it.

A second case needed a judgement rather than a rule. `CameraEngine._picam` — 32
occurrences — is the attribute holding the `Picamera2` instance. It is an
abbreviation of the dependency, not a reference to the project, so the word
boundary correctly left it alone. But leaving it would have kept the old stem in
the tree, and renaming it to `_birdshot` would have actively misnamed the object.
It became `self._cam`, which removes the stem and reads truer than either
alternative. **The rename that a mechanical rule gets right is not always the
rename that is correct**; this one needed a human to look at what the variable
held.

The tree was also restructured to a `src/` layout, matching the other projects in
this family, with `picam/` → `src/birdshot/` and `GUIDE.md` → `docs/GUIDE.md`.

### 4.2 A defect the rewrite introduced, and caught

The `src/` move broke something, and the way it was caught is the transferable
part.

`bin/birdshot-*` locate the package by extending `sys.path` with the repository
root. With the package one level deeper that is wrong, and the fix was folded
into the rewrite as a literal substitution so that every commit — not just the
tip — carried the corrected line.

That much was anticipated. What was not anticipated was `sync.sh`, which runs
four inline `python3 -c` snippets **on the Pi over SSH**, each doing
`cd $REMOTE_DIR && python3 -c "from birdshot.config import Config …"`. These do
not go through `bin/`, so the `sys.path` fix did not reach them, and all four
would have failed at runtime on the deployed machine. They were found by
grepping the rewritten tree for `python3 -c` rather than by testing, because
testing them requires the Pi.

The rewrite was **discarded and re-run from the backup bundle** with an
additional rule (`PYTHONPATH=src python3 -c`), so that all five published commits
are internally consistent rather than four broken ones followed by a fix. Total
cost of redoing it: under a second, because the whole operation is
deterministic and scripted. This is the practical argument for driving a history
rewrite from a rules file rather than by hand — **a rewrite you can re-run is a
rewrite you can afford to get wrong once.**

### 4.3 Signing and attribution

`filter-repo` does not sign. Signatures were applied afterwards with
`git filter-branch --commit-filter 'git commit-tree -S "$@"'`, which preserves
both `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` from the environment
`filter-branch` exports — the property that matters, since a rebase-based
approach would have rewritten every committer date to the moment of the rewrite.

Result, verified with `git log --format='%h sig:%G? %an <%ae>'`:

| | Before | After |
|---|---|---|
| Author | `paulr <paulr@sdf.org>` | `Paul Richeson <paulr@sdf.org>` |
| Signature | `N` (unsigned) ×5 | `G` (good) ×5 |
| Author dates | 2026-07-31 | 2026-07-31, unchanged |

The author identity had been `paulr` because the repository was created before
the global `user.name` was set to the full name; the global config was already
correct and had simply never applied here. Small, and exactly the kind of thing
that is permanent once pushed.

---

## 5. What was built around the software

### 5.1 A gate, because the leak source still exists

The sanitisation in §4 is a one-time correction to a *recurring* condition. The
machine that develops birdshot is still the machine with the Pi on the LAN, and
the next convenient hardcoded address is one `sync.sh` edit away.

`.githooks/pre-commit` scans **staged content** — what is about to enter history,
not what is lying in the working tree — and refuses private IPv4 addresses, a
`/home/` path that is not `/home/pi`, a personal `/media/` mount, private key
material, the pre-rebrand name, and any capture or log file regardless of
`.gitignore`.

Building it surfaced a design question worth recording. The hook flagged
*itself*: it must spell `picam` in order to match it. The same applies to this
report and to `CHANGELOG.md` — **a history document that cannot name the thing
it renamed is not a record of anything.** The available answers were to exempt a
directory, which becomes a loophole nobody re-examines, or to require an
explicit marker in the file. The marker won:

```
birdshot-lint: allow-legacy-name
```

It appears at the top of this file. The exemption is greppable, every use is
visible in review, and it cannot be acquired accidentally by putting a file in
the wrong folder. The hook exempts itself by exact path, which is the one case
that cannot be solved by a marker.

The whole-tree equivalent runs as `make sanitise`, which catches anything
committed before the hook existed; `make audit-history` reads every blob in every
commit and is the check to run after any rewrite.

### 5.2 `vendor/`, deliberately empty

Since no foreign code exists in the tree, `vendor/` contains only a README. The
argument for creating it anyway is that the alternative is worse: when something
external eventually *is* copied in — a libcamera tuning file, a lens correction
table — the pressure is always to drop it beside the code that uses it, and that
is how a repository quietly stops being licensable. Eighteen months later nobody
can say which files are theirs to relicense, and the answer has to be
reconstructed by reading every file.

The rule is therefore stated while it is still free to state: **`src/`, `bin/`,
`mac/` and `docs/` are one author's work; anything else lives in `vendor/` or
does not enter the repository.** The procedure — `SOURCE.txt`, verbatim
`LICENSE`, `MANIFEST.sha256`, a row in `THIRD-PARTY.md` — is in
`vendor/README.md`, and `make vendor-check` enforces its shape.

Note that the criterion is *authorship*, not licence compatibility. MIT-licensed
code from a third party still belongs in `vendor/`, because the question the
layout answers is "whose is it?" and not "may we use it?".

### 5.3 Documents

`THIRD-PARTY.md` inventories every dependency and external binary with its
licence and how it is reached, and carries the §3 analysis.
`CONTRIBUTING.md` states the three non-negotiables — signed commits, the
`vendor/` rule, nothing about your machine — and is honest that `make check`
proves only that the code parses and nothing leaked, not that it works;
that needs `make selftest` and a camera. `SECURITY.md` describes a program that
listens on nothing and whose real disclosure risk is not a code defect at all,
but the operator publishing thousands of timestamped photographs of a fixed view
of somewhere they live. `README.md` was repaired for the new layout and its
"change this to your name" boilerplate replaced with an actual licence position.

### 5.4 The landing page

`index.html` at the repository root, served by Pages at `birdshot.org`, with
`CNAME` and `.nojekyll`. No build step and therefore no Actions workflow — which
is fortunate, because the available token lacks the `workflow` scope and a push
containing `.github/workflows/` would have been rejected. Branch-deploy is both
simpler and the only option currently available (§8.4).

The page has no photographs, because the rig is powered down. Rather than mock
one up, the hero is built from the thing the project has instead of pictures:
its **exposure ladder** — the fourteen shutter presets in `PRESET_SHUTTERS_US`,
each with the folder it writes into, on a log-scaled bar tinted from night-blue
at 19.1 s to near-white at 1/8000 s. The rows are **generated at build time by
importing `birdshot.naming`**, so the page cannot drift from the code that
defines it. The screenshot section states plainly that the captures are pending
and why, which is both true and the honest form of an empty state.

---

## 6. Verification

All checks re-run against the final tree and the rewritten history.

| Check | Command | Result |
|---|---|---|
| No private address in any blob | `make audit-history` | 0 blobs |
| No personal home or media path | `make audit-history` | 0 blobs |
| No key material | `make audit-history` | 0 blobs |
| No pre-rebrand name | `make sanitise` | 0 files |
| Only source in history | `git log --all --name-only` | `.py`/`.sh`/`.md` + `LICENSE` + `.gitignore` |
| Every commit signed | `git log --format='%G?'` | `G` ×5 |
| Every commit attributed | `git log --format='%an <%ae>'` | `Paul Richeson <paulr@sdf.org>` ×5 |
| Python parses | `make lint` | 17 modules, 3 scripts |
| Shell parses | `make lint` | 5 scripts |
| Hook rejects a leak | staged probe with all four violations | refused, 4 rules fired |
| Hook accepts the real tree | staged whole tree | clean |

**What is not verified: that the software works at this tree.** It worked as
deployed, and no behavioural change was made — the rewrite touched names, paths
and headers. But `make selftest` needs the camera, and the camera is off. This
is the whole reason `1.0.0-rc1` is a candidate (§8.1).

---

## 7. Two corrections

Retained because both were nearly acted upon.

**The word-boundary rule looked like it needed an exception list.** The initial
plan enumerated compounds — `picam-cli`, `picam-gui`, `picam-data`,
`picam.desktop`, `PICAM_PROFILE` — as separate substitutions, on the assumption
that a single rule could not distinguish them from `picamera2`. It can; `\b`
does exactly that, and the enumeration would have been five rules to maintain and
one to forget. The check that settled it was running the regex against real
source lines and reading the output, which took less time than writing the list.
`PICAM_PROFILE` did need its own rule, for the opposite reason: `_` *is* a word
character, so `\bPICAM\b` does not match it.

**`shutter_dir(125)` returns `us120`, and this is not a bug.** Computing the
ladder for the landing page produced `us120` for a 125 µs preset, which looks
like an off-by-one. It is Python's banker's rounding — `round(12.5)` is `12`,
not `13` — inside a function that is documented as returning a *bucket* on a
10 µs grid, with the exact microsecond value recorded in `index.jsonl`
regardless. The asymmetry is real (125 rounds down, 135 rounds up) and cosmetic.
It was recorded as a next step rather than fixed, because changing a folder-naming
function that the operator's existing captures already sort under is not a change
to make without the hardware to test it. `describe_shutter(16000)` similarly
prints `1/62 s` where a photographer would write `1/60`.

---

## 8. Next steps

Ordered by what blocks the release.

### 8.1 Formal physical testing — blocks `1.0.0`

**Solution:** power the rig, run `make selftest` at this exact tree, and record
the output in `docs/`. Eighteen checks against real hardware: naming, gates,
exposure ladder, PID convergence, storage layout, YUV conversion, the live
camera, ffmpeg, Qt. The rename touched module paths and the `sys.path`
bootstrap in three scripts and four inline SSH snippets, so the checks that
matter most are the ones that exercise an import path: a successful `selftest`
plus one real `sync.sh push` and one capture session is sufficient evidence.
Until that output exists, `rc1` does not become `1.0.0`.

### 8.2 Screenshots — blocks the landing page and the guide

**Solution:** `assets/screenshots/README.md` holds the shot list, the `scrot`
commands, and the review checklist. The last is the substantive part: a
screenshot of a live camera is a photograph of wherever it points, and the GUI
puts the capture root on screen beside it. Commit them one at a time by name.
The hook blocks image files from a bulk `git add` deliberately, so each one
requires `BIRDSHOT_ALLOW_UNSANITISED=1` — the friction is the feature.

### 8.3 DNS cutover for `birdshot.org`

**Solution:** the diagnostic first, because it is the step that is skipped and
the one that determines everything:

```sh
dig +short birdshot.org NS
```

That names the party whose opinion about address records actually matters, which
is frequently not the registrar. Snapshot the existing zone before touching it
(`for t in A AAAA CNAME MX TXT NS CAA; do dig +short birdshot.org $t; done`) —
a freshly registered domain should be empty, but if it carries `MX` records, a
migration planned as "point it at the new thing" can silently destroy mail.

Then: apex `A` to `185.199.108–111.153`, apex `AAAA` to the four
`2606:50c0:800x::153`, `www` `CNAME` to `vonglurt.github.io`, and the
verification `TXT` at `_github-pages-challenge-vonglurt` with the value GitHub
issues under **Settings → Pages → Add a domain**. Domain verification should be
done *before* the `CNAME` is committed, since it is what prevents another
account claiming the name. Expect the Let's Encrypt certificate to take up to an
hour, and do not enable Enforce HTTPS until it is issued.

Watch for the **apex lock**: if the zone is published by a shared host with an
active hosting enrolment, the apex `A` record may render as uneditable in that
host's DNS panel. The record is not broken and the provider does not forbid the
operation — the hosting enrolment is holding it, and cancelling the enrolment
releases it.

### 8.4 CI

**Solution:** `make check` is host-runnable and should run on every push; the
hardware selftest cannot run in Actions and never will, so the workflow's honest
scope is lint plus sanitise plus vendor-check. This requires a token scope the
current one lacks:

```sh
gh auth refresh -s workflow
```

Adding `.github/workflows/` without it fails at push time. Worth doing, because
the sanitisation gate is currently client-side only — a contributor who has not
run `make hooks` is not gated at all, and CI is what closes that.

### 8.5 Packaging

**Solution:** a `pyproject.toml` with a `src/` layout and console entry points,
so deployment becomes `pip install .` rather than `sync.sh` plus a `PYTHONPATH`
convention. Note the licence consequence: **a wheel bundling PyQt5 is a combined
work and must be GPL-3.0** (§3). Declare PyQt5 an optional extra
(`birdshot[gui]`) so the default install is the copyleft-free headless path.
The architecture already supports this; only the metadata is missing.

### 8.6 The rounding asymmetry

**Solution:** replace the banker's rounding in `shutter_dir` with explicit
half-up (`math.floor(x + 0.5)`), and correct `describe_shutter` to snap to
conventional shutter denominators. Both are one-line changes and both are
gated on §8.1, since they alter folder names that existing captures already
sort under. Add the round-trip property — `parse_shutter_dir(shutter_dir(n))`
within one bucket of `n` — to the selftest at the same time.

---

## 9. Conclusion

The software was finished and unpublishable simultaneously, and the gap between
those two states was made entirely of things that are invisible while a project
has one user: a name that could not be searched for, a copyright line naming an
email address, an unsigned history, an undocumented dependency graph containing
one copyleft licence, and five commits describing a private network.

None of it was a defect. All of it was permanent the moment a remote was added,
which is the actual finding: **the cost of every item above was zero until first
push and unbounded afterwards.** The rewrite was affordable precisely because
nothing had been published yet, and the same operation a week later would have
broken every clone.

The one genuinely useful discovery was structural rather than procedural. The
GPL boundary that makes birdshot's headless path freely redistributable exists
because the capture engine was separated from the GUI for *testing* reasons,
years of design decisions before anyone asked a licence question. Good
architecture paid a debt that had not been incurred yet — and the response is
not to congratulate it but to write it down as an invariant, so that the next
convenient GUI import does not quietly spend it.

---

*MIT — Copyright (c) 2026 Paul Richeson. Contact: paulr@sdf.org*
