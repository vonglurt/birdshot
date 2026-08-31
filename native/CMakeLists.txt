# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# birdshot native -- the 2.0 line. One static core library, one CLI binary,
# no third-party code: the math, the ephemeris, the JSON and the JPEG encoder
# are all in-tree (vendor/ stays empty by the same policy as the 1.x line).
#
# Targets every platform the packaging/ channels ship to:
#   macOS (clang), Linux (gcc/clang), FreeBSD/OpenBSD/NetBSD (clang),
#   Windows (MSVC or MinGW). C++17 and the standard library only.

cmake_minimum_required(VERSION 3.16)
project(birdshot VERSION 2.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# Interprocedural optimisation where the toolchain supports it -- the analysis
# and encode paths are hot loops and benefit measurably.
include(CheckIPOSupported)
check_ipo_supported(RESULT ipo_ok OUTPUT ipo_msg)
if(ipo_ok AND NOT CMAKE_BUILD_TYPE STREQUAL "Debug")
  set(CMAKE_INTERPROCEDURAL_OPTIMIZATION ON)
endif()

if(MSVC)
  add_compile_options(/W4 /permissive-)
else()
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(Threads REQUIRED)

add_library(birdshot_core STATIC
  src/backend.cpp
  src/exif.cpp
  src/replay.cpp
  src/json.cpp
  src/mathkit.cpp
  src/geo.cpp
  src/solar.cpp
  src/naming.cpp
  src/image.cpp
  src/jpeg.cpp
  src/analysis.cpp
  src/exposure.cpp
  src/birdflight.cpp
  src/config.cpp
  src/storage.cpp
  src/synthetic.cpp
  src/engine.cpp
  src/gui.cpp
  src/plan.cpp
  src/align.cpp
  src/selftest.cpp
)
target_include_directories(birdshot_core PUBLIC include)
target_link_libraries(birdshot_core PUBLIC Threads::Threads)

# The macOS camera backend: platform frameworks, not third-party code --
# a system boundary like the standard library. Other platforms' backends
# (V4L2, Media Foundation, libcamera) join the same way.
if(APPLE)
  enable_language(OBJCXX)
  set(CMAKE_OBJCXX_STANDARD 17)
  set(CMAKE_OBJCXX_STANDARD_REQUIRED ON)
  target_sources(birdshot_core PRIVATE src/avfoundation.mm)
  set_source_files_properties(src/avfoundation.mm PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
  target_link_libraries(birdshot_core PUBLIC
    "-framework AVFoundation" "-framework CoreMedia" "-framework CoreVideo"
    "-framework Foundation")
endif()
if(WIN32)
  # The viewfinder's loopback HTTP server.
  target_link_libraries(birdshot_core PUBLIC ws2_32)
endif()

add_executable(birdshot cli/main.cpp)
target_link_libraries(birdshot PRIVATE birdshot_core)

install(TARGETS birdshot RUNTIME DESTINATION bin)

# ---------------------------------------------------------------- the GUI --
# The Qt Widgets front end. Optional and additive: the core library and the
# CLI stay dependency-free static builds; birdshot-gui links Qt 6 (LGPL-3.0)
# dynamically, which keeps the application MIT -- the 1.x licensing caveat
# was PyQt5's GPL, and it does not apply here. Absent Qt, nothing changes.
option(BIRDSHOT_QT "Build the Qt Widgets front end when Qt 6 is present" ON)
if(BIRDSHOT_QT)
  find_package(Qt6 QUIET COMPONENTS Widgets)
  if(Qt6_FOUND)
    set(CMAKE_AUTOMOC ON)
    file(GLOB qt_sources qt/*.cpp)
    add_executable(birdshot-gui ${qt_sources})
    target_link_libraries(birdshot-gui PRIVATE birdshot_core Qt6::Widgets)
    set_target_properties(birdshot-gui PROPERTIES WIN32_EXECUTABLE ON)
    install(TARGETS birdshot-gui RUNTIME DESTINATION bin)
    message(STATUS "Qt ${Qt6_VERSION} found -- building birdshot-gui")
  else()
    message(STATUS "Qt 6 not found -- skipping birdshot-gui (core and CLI unaffected)")
  endif()
endif()

enable_testing()
add_test(NAME selftest COMMAND birdshot selftest)
