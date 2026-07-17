# PTWebSocket — PineTunnel WebSocket Client DLL
# CMakeLists.txt for development builds
#
# Production builds use Visual Studio project files (.vcxproj).
# This CMakeLists.txt is for local development and testing.
#
# Build instructions (Windows with MSVC):
#   mkdir build && cd build
#   cmake -G "Visual Studio 17 2022" -A x64 ..
#   cmake --build . --config Release
#
# For Win32 (MT4):
#   cmake -G "Visual Studio 17 2022" -A Win32 ..
#   cmake --build . --config Release

cmake_minimum_required(VERSION 3.20)

# WinHTTP WebSocket API requires Windows 8+ (0x0602)
# Must be set before project() to ensure all Windows headers pick it up
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

project(PTWebSocket VERSION 3.3.2 LANGUAGES CXX)

# Static linking of VC++ runtime (no redist dependency)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# WinHTTP WebSocket API requires Windows 8+ — must be set before any header inclusion
add_compile_definitions(_WIN32_WINNT=0x0602)

# Source files
set(SOURCES
    asyncwebsocketclient.cpp
    ptwebsocket.cpp
    ptwebsocket.rc
)

set(HEADERS
    asyncwebsocketclient.h
    resource.h
)

# Shared library (DLL)
add_library(${PROJECT_NAME} SHARED ${SOURCES} ${HEADERS})

# Link WinHTTP, Winsock, WMI, ICMP (for base64 decode)
target_link_libraries(${PROJECT_NAME} PRIVATE
    winhttp
    ws2_32
    wbemuuid
    iphlpapi
    crypt32
)

# Export definitions from .def file
target_link_options(${PROJECT_NAME} PRIVATE
    /DEF:${CMAKE_CURRENT_SOURCE_DIR}/ptwebsocket.def
)

# Include directories
target_include_directories(${PROJECT_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
)

# Compiler flags
if(MSVC)
    target_compile_options(${PROJECT_NAME} PRIVATE
        /W4                    # Warning level 4
        /DPTWEBSOCKET_EXPORTS   # Export macros
        /D_WINDOWS             # Windows target
        /D_USRDLL              # DLL build
    )
    # Static CRT: /MT for Release, /MTd for Debug
    set_property(TARGET ${PROJECT_NAME} PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )
endif()

# Copy DLL to output directory
set_target_properties(${PROJECT_NAME} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)

# Platform-specific output names
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    # x64 — MT5 uses PTWebSocket.dll
    set_target_properties(${PROJECT_NAME} PROPERTIES
        OUTPUT_NAME "PTWebSocket"
    )
else()
    # x86 — MT4 uses PTWebSocket32.dll
    set_target_properties(${PROJECT_NAME} PROPERTIES
        OUTPUT_NAME "PTWebSocket32"
    )
endif()
