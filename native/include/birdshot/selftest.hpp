// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#pragma once

namespace bs {

// Run the whole suite; returns the number of failures (0 = pass). Prints one
// line per check. Skips, never fails, on what this machine cannot run.
int run_selftest(bool verbose);

}  // namespace bs
