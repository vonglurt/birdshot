# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# RPM adapter over the canonical pyproject build (docs/PACKAGING.md, 4.3).
# Distribution point is COPR; Fedora's pyproject macros do all the work.

Name:           birdshot
Version:        1.1.0~rc1
Release:        1%{?dist}
Summary:        Bird and sky capture for the Raspberry Pi HQ Camera
License:        MIT
URL:            https://birdshot.org
Source0:        https://github.com/vonglurt/birdshot/archive/refs/tags/v1.1.0-rc1.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
Requires:       python3-numpy
Requires:       ffmpeg
Recommends:     python3-qt5
Recommends:     rsync

%description
Metered auto-exposure that holds through a changing sky, quality gates,
a tiered storage cascade, EXIF tagging and timelapse assembly, for the
IMX477 HQ Camera. Ships birdshot-cli, birdshot-gui and birdshot-wallpaper.

%prep
%autosetup -n birdshot-1.1.0-rc1

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files birdshot
install -Dm644 packaging/share/birdshot.desktop \
    %{buildroot}%{_datadir}/applications/birdshot.desktop

%files -f %{pyproject_files}
%{_bindir}/birdshot-cli
%{_bindir}/birdshot-gui
%{_bindir}/birdshot-wallpaper
%{_datadir}/applications/birdshot.desktop
%license LICENSE
%doc README.md docs/GUIDE.md

%changelog
* Sun Aug 30 2026 Paul Richeson - 1.1.0~rc1-1
- First spec: pyproject macros over the upstream build.
