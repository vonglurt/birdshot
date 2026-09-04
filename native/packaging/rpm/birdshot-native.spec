# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
Name:           birdshot-native
Version:        2.0.0~rc1
Release:        1%{?dist}
Summary:        Bird and sky capture: metered AE, quality gates, solar planning
License:        MIT
URL:            https://birdshot.org
Source0:        https://github.com/vonglurt/birdshot/archive/refs/tags/v2.0.0-rc1.tar.gz
BuildRequires:  cmake >= 3.16, gcc-c++ >= 8

%description
The native (C++17) line of birdshot: the capture engine, EV-space PID
auto-exposure, quality gates, Bird Flight detection, and the Horizons
tools -- solar ephemeris, sunset shoot planning, multi-day alignment.
No runtime dependencies.

%prep
%autosetup -n birdshot-2.0.0-rc1

%build
%cmake -S native
%cmake_build

%check
%{_vpath_builddir}/birdshot selftest

%install
%cmake_install

%files
%license LICENSE
%{_bindir}/birdshot

%changelog
* Sun Aug 30 2026 Paul Richeson <ports@birdshot.org> - 2.0.0~rc1-1
- First native release candidate
