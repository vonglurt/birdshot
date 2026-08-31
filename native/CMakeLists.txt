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
  src/plan.cpp
  src/align.cpp
  src/selftest.cpp
)
target_include_directories(birdshot_core PUBLIC include)
target_link_libraries(birdshot_core PUBLIC Threads::Threads)

add_executable(birdshot cli/main.cpp)
target_link_libraries(birdshot PRIVATE birdshot_core)

install(TARGETS birdshot RUNTIME DESTINATION bin)

enable_testing()
add_test(NAME selftest COMMAND birdshot selftest)
