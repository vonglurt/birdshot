# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# Homebrew formula (docs/PACKAGING.md, 4.5) for the Mac darkroom: sessions,
# assemble, EXIF and doctor work today; capture waits on the AVFoundation
# backend. Lives in a tap:  brew tap vonglurt/birdshot && brew install birdshot
#
# TODO at release time: point url at the new tag and fill sha256 from
#   shasum -a 256 <tarball>   (CI prints it next to the uploaded asset).

class Birdshot < Formula
  include Language::Python::Virtualenv

  desc "Bird and sky capture for the Raspberry Pi HQ Camera"
  homepage "https://birdshot.org"
  url "https://github.com/vonglurt/birdshot/archive/refs/tags/v1.1.0-rc1.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000" # TODO
  license "MIT"

  depends_on "ffmpeg"
  depends_on "python@3.12"

  resource "numpy" do
    url "https://files.pythonhosted.org/packages/source/n/numpy/numpy-2.0.2.tar.gz"
    sha256 "883c987dee1880e2a864ab0dc9892292582510604156762362d9326444636e78"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "birdshot doctor", shell_output("#{bin}/birdshot-cli doctor")
  end
end
