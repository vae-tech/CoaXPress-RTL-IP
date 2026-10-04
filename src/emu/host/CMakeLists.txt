# cxp-host — C++17 / Qt port of the emu/cxp Python host stack.
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build -j
#   ctest --test-dir build --output-on-failure
#
# Builds with Qt 6 when its Widgets/Test dev packages are
# installed, otherwise falls back to Qt 5.15.

cmake_minimum_required(VERSION 3.16)
project(cxp_host VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

option(CXP_BUILD_GUI   "Build the Qt Widgets device explorer" ON)
option(CXP_BUILD_TESTS "Build the QtTest suite"               ON)

set(_qt_components Core Gui)
if(CXP_BUILD_GUI)
  list(APPEND _qt_components Widgets)
endif()
if(CXP_BUILD_TESTS)
  list(APPEND _qt_components Test)
endif()

find_package(QT NAMES Qt6 Qt5 COMPONENTS ${_qt_components} QUIET)
if(NOT QT_VERSION_MAJOR)
  set(QT_VERSION_MAJOR 5)
endif()
find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS ${_qt_components})
set(QT Qt${QT_VERSION_MAJOR})
find_package(Threads REQUIRED)
find_package(ZLIB REQUIRED)  # zipped GenICam XML (GEN-001)
message(STATUS "cxp-host: building against Qt ${QT_VERSION_MAJOR}")

# Reference GenICam manifest served by the virtual camera; overridable at
# run time with $CXP_XML.
set(CXP_DEFAULT_XML "${CMAKE_CURRENT_SOURCE_DIR}/../../regmap/genicam/cxp_camera.xml"
    CACHE FILEPATH "Default camera XML for the virtual camera")

# Validation-plan test catalogue (generated from
# src/emu/host/validation/cxp_camera_validation_plan.md by
# validation/gen_validation_cases.py); overridable at run time with
# $CXP_VALIDATION_JSON.
set(CXP_VALIDATION_JSON "${CMAKE_CURRENT_SOURCE_DIR}/validation/cxp_validation_cases.json"
    CACHE FILEPATH "Validation test-case catalogue")

add_compile_options(-Wall -Wextra -Wno-unused-parameter)

# --------------------------------------------------------------------------
# core library
# --------------------------------------------------------------------------
add_library(cxpcore STATIC
  src/cxp/utils/log.cpp
  src/cxp/utils/hexdump.cpp
  src/cxp/utils/pcap.cpp
  src/cxp/protocol/chars.cpp
  src/cxp/protocol/constants.cpp
  src/cxp/protocol/crc.cpp
  src/cxp/protocol/packets.cpp
  src/cxp/transport/fifo.cpp
  src/cxp/image/pixel_formats.cpp
  src/cxp/image/reconstruct.cpp
  src/cxp/image/test_pattern.cpp
  src/cxp/parser/stream_parser.cpp
  src/cxp/genicam/sfnc.cpp
  src/cxp/compliance/checker.cpp
  src/cxp/camera/client.cpp
  src/cxp/camera/protocol_log.cpp
  src/cxp/camera/session.cpp
  src/cxp/sim/virtual_camera.cpp
  src/cxp/sim/virtual_camera_bench.cpp
)
# src/regmap: the generated register map (cxp_regmap.hpp, namespace cxp::reg).
target_include_directories(cxpcore PUBLIC src ${CMAKE_CURRENT_SOURCE_DIR}/../../regmap)
target_compile_definitions(cxpcore PUBLIC
  CXP_DEFAULT_XML="${CXP_DEFAULT_XML}"
  CXP_VERSION="${PROJECT_VERSION}")
target_link_libraries(cxpcore PUBLIC ${QT}::Core ${QT}::Gui Threads::Threads)

# --------------------------------------------------------------------------
# validation-plan runner
# --------------------------------------------------------------------------
# An object library, not a static one: every object file is linked into
# each consumer, so checks that register themselves from a static
# initialiser are never dropped by the linker.  Link it directly into
# every target that runs checks (object files do not propagate further).
# The checks must not depend on the virtual camera: they judge a device.
# One file per test case under cases/<area>/ (plus the area's _helpers.cpp);
# the glob picks up a new case file at the next build.
file(GLOB_RECURSE CXP_CASE_SOURCES CONFIGURE_DEPENDS
  ${CMAKE_CURRENT_SOURCE_DIR}/src/cxp/validation/cases/*.cpp)
add_library(cxpvalidation OBJECT
  src/cxp/validation/cases.cpp
  src/cxp/validation/context.cpp
  src/cxp/validation/runner.cpp
  src/cxp/validation/campaign.cpp
  ${CXP_CASE_SOURCES})
# Every case file includes the same heavy header; parse it once.
target_precompile_headers(cxpvalidation PRIVATE src/cxp/validation/cases/_common.h)
target_compile_definitions(cxpvalidation PUBLIC
  CXP_VALIDATION_JSON="${CXP_VALIDATION_JSON}")
target_link_libraries(cxpvalidation PUBLIC cxpcore ZLIB::ZLIB)

# --------------------------------------------------------------------------
# applications
# --------------------------------------------------------------------------
add_executable(cxp apps/cli/main.cpp)
target_link_libraries(cxp PRIVATE cxpvalidation cxpcore)

if(CXP_BUILD_GUI)
  add_executable(cxp-gui
    apps/gui/main.cpp
    apps/gui/io_bridge.h
    apps/gui/io_bridge.cpp
    apps/gui/device_model.h
    apps/gui/device_model.cpp
    apps/gui/link_stats.h
    apps/gui/link_stats.cpp
    apps/gui/explorer_tab.h
    apps/gui/explorer_tab.cpp
    apps/gui/stats_tab.h
    apps/gui/stats_tab.cpp
    apps/gui/image_tab.h
    apps/gui/image_tab.cpp
    apps/gui/validation_window.h
    apps/gui/validation_window.cpp
    apps/gui/main_window.h
    apps/gui/main_window.cpp
  )
  target_link_libraries(cxp-gui PRIVATE cxpvalidation cxpcore ${QT}::Widgets)
endif()

# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
if(CXP_BUILD_TESTS)
  enable_testing()
  foreach(t test_protocol test_transport test_genicam test_end_to_end
            test_golden_vectors test_validation)
    add_executable(${t} tests/${t}.cpp)
    target_link_libraries(${t} PRIVATE cxpcore ${QT}::Test)
    add_test(NAME ${t} COMMAND ${t})
    set_tests_properties(${t} PROPERTIES TIMEOUT 120)
  endforeach()
  target_link_libraries(test_validation PRIVATE cxpvalidation)
  # The GUI validation window against the CLI, on the offscreen platform.
  if(CXP_BUILD_GUI)
    add_executable(test_gui_validation
      tests/test_gui_validation.cpp
      apps/gui/validation_window.cpp
      apps/gui/device_model.cpp
      apps/gui/link_stats.cpp
    )
    target_include_directories(test_gui_validation PRIVATE apps/gui)
    target_compile_definitions(test_gui_validation PRIVATE CXP_CLI_BIN="$<TARGET_FILE:cxp>")
    target_link_libraries(test_gui_validation PRIVATE cxpvalidation cxpcore ${QT}::Widgets ${QT}::Test)
    add_dependencies(test_gui_validation cxp)
    add_test(NAME test_gui_validation COMMAND test_gui_validation)
    set_tests_properties(test_gui_validation PROPERTIES TIMEOUT 180 ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
  endif()
  # Golden vectors from the cxp_protocol package (src/model/cxp_protocol).
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  set(CXP_PROTOCOL_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../../model/cxp_protocol)
  set(CXP_GOLDEN_VECTORS ${CMAKE_CURRENT_BINARY_DIR}/golden_vectors.json)
  file(GLOB CXP_PROTOCOL_SRC ${CXP_PROTOCOL_DIR}/cxp_protocol/*.py)
  add_custom_command(
    OUTPUT ${CXP_GOLDEN_VECTORS}
    COMMAND ${Python3_EXECUTABLE} -m cxp_protocol.vectors > ${CXP_GOLDEN_VECTORS}
    WORKING_DIRECTORY ${CXP_PROTOCOL_DIR}
    DEPENDS ${CXP_PROTOCOL_SRC}
    COMMENT "cxp_protocol golden vectors")
  add_custom_target(golden_vectors ALL DEPENDS ${CXP_GOLDEN_VECTORS})
  add_dependencies(test_golden_vectors golden_vectors)
  # The validation catalogue must match the plan it is generated from.
  add_test(NAME validation_catalogue_fresh
           COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/validation/gen_validation_cases.py --check)
  # Each check reads exactly the parameters its catalogue entry declares.
  add_test(NAME validation_params_declared
           COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/validation/check_params.py)
  # One case per file, named by its ID, in its area's directory.
  add_test(NAME validation_case_files
           COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/validation/check_case_files.py)
  set_tests_properties(test_golden_vectors PROPERTIES
    ENVIRONMENT "CXP_GOLDEN_VECTORS=${CXP_GOLDEN_VECTORS}")
endif()
