# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# Homebrew formula for the native line. `brew install --build-from-source`
# style: one cmake build, the selftest as the test block.
class BirdshotNative < Formula
  desc "Bird and sky capture: metered AE, quality gates, solar planning"
  homepage "https://birdshot.org"
  url "https://github.com/vonglurt/birdshot/archive/refs/tags/v2.0.0-rc1.tar.gz"
  sha256 "" # filled by the release checklist from the tagged tarball
  license "MIT"
  head "https://github.com/vonglurt/birdshot.git", branch: "main"

  depends_on "cmake" => :build

  def install
    system "cmake", "-S", "native", "-B", "build", *std_cmake_args
    system "cmake", "--build", "build"
    system "cmake", "--install", "build"
  end

  test do
    system "#{bin}/birdshot", "selftest"
  end
end
