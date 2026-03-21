cmake_minimum_required(VERSION 3.15)
project(aja-capture)

set(CMAKE_CXX_STANDARD 17)

# AJA NTV2 SDK location — adjust if your SDK is elsewhere
if (NOT DEFINED AJA_NTV2_ROOT)
    set(AJA_NTV2_ROOT "$ENV{HOME}/dev/libajantv2")
endif()

if (NOT DEFINED AJA_NTV2_BUILD)
    set(AJA_NTV2_BUILD "${AJA_NTV2_ROOT}/build")
endif()

set(TARGET_INCLUDE_DIRS
    ${AJA_NTV2_ROOT}/demos
    ${AJA_NTV2_ROOT}
    ${AJA_NTV2_ROOT}/ajantv2/includes
    ${AJA_NTV2_ROOT}/ajantv2/src/mac)

find_library(FOUNDATION_FRAMEWORK Foundation)

add_executable(aja-capture
    aja-capture.cpp
    ${AJA_NTV2_ROOT}/demos/ntv2democommon.cpp)

target_include_directories(aja-capture PRIVATE ${TARGET_INCLUDE_DIRS})
target_compile_definitions(aja-capture PRIVATE AJAMac)

target_link_libraries(aja-capture PRIVATE
    ${AJA_NTV2_BUILD}/ajantv2/libajantv2.dylib
    ${FOUNDATION_FRAMEWORK})

set_target_properties(aja-capture PROPERTIES
    BUILD_RPATH "${AJA_NTV2_BUILD}/ajantv2"
    INSTALL_RPATH "${AJA_NTV2_BUILD}/ajantv2")
