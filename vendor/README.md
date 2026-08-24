<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# vendor/ — external licensed items

**This directory is empty of code, and that is the interesting fact about it.**

birdshot carries no third-party source. Every file under `src/`, `bin/`, `mac/`
and `docs/` is original work, licensed MIT by Paul Richeson, and the audit that
established this is recorded in
[docs/lab-reports/lab-report-birdshot-publication.md](../docs/lab-reports/lab-report-birdshot-publication.md).
Everything birdshot depends on — picamera2, numpy, PyQt5, simplejpeg, piexif,
ffmpeg, rsync — is installed by the operator and reached at runtime, never
copied in. Those are inventoried in [THIRD-PARTY.md](../THIRD-PARTY.md).

The directory exists anyway, because the alternative is worse. When something
external *does* get copied in — a tuning file from the libcamera tree, a lens
correction table, a sensor tuning JSON from Raspberry Pi, a test image somebody
else made — the pressure is always to drop it next to the code that uses it.
That is how a repository quietly stops being licensable: eighteen months later
nobody can say which files are actually yours to relicense, and the answer has
to be reconstructed by reading every file.

So: **anything not written for this project lives here, or it does not enter the
repository at all.**

## The rule

`src/`, `bin/`, `mac/` and `docs/` are MIT and are Paul Richeson's work
exclusively. A commit that puts foreign code in any of them is wrong even if the
licence is compatible — MIT-licensed code from a third party still belongs here,
because the question this layout answers is *"whose is it?"*, not *"may we use
it?"*.

## Vendoring something

One directory per item, named for the upstream project, carrying three things
alongside the files:

```
vendor/<name>/
    SOURCE.txt        where it came from, which version, when, and why
    LICENSE           upstream's licence text, verbatim and unedited
    MANIFEST.sha256   a hash per vendored file
    <the files>
```

`SOURCE.txt` should be readable by someone who has never seen the project:

```
Project:   imx477 tuning files
Upstream:  https://github.com/raspberrypi/libcamera
Version:   v0.0.5  (commit abc1234)
Retrieved: 2026-08-23
Licence:   BSD-2-Clause  (see ./LICENSE)
Why:       the stock tuning clips highlights on bright sky; this is the
           unmodified upstream file, loaded by src/birdshot/tone.py.
Modified:  no
```

If the answer to `Modified:` is ever yes, say exactly what changed and keep the
diff beside it. A modified vendored file with no record of the modification is
indistinguishable from a file nobody can account for.

Then add a row to [THIRD-PARTY.md](../THIRD-PARTY.md) — a vendored item that is
not in the inventory is exactly the failure this directory exists to prevent —
and verify with `make vendor-check`, which recomputes every `MANIFEST.sha256`
and reports any file that has drifted from the hash recorded when it was
vendored.

## What does not belong here

Working material — reference photographs, scratch notes, downloaded datasets,
anything you are merely *consulting* rather than shipping. That is not vendored
code, it is your desk. Keep it outside the repository; `.gitignore` does not
make it safe, it only makes it invisible.
