cmake_minimum_required(VERSION 3.25)
project(artificer LANGUAGES CXX)

include(FetchContent)

# The Anvil SDK: the AnvilPlugin CMake package (anvil_add_plugin) and anvil.exe,
# from the SDK archive of an Anvil release. URL_HASH is the archive's line in
# the release's SHA256SUMS.txt.
FetchContent_Declare(anvil_sdk
    URL https://github.com/happy-dwarf-team/anvil/releases/download/v1.27.0/anvil-sdk-windows-x64.zip
    URL_HASH SHA256=4b7206c00fe7a4f3551b71f65cc87d2f03a5a6a6dceb00cdc5091acfec375da7)
FetchContent_MakeAvailable(anvil_sdk)

# Reads plugin.toml, runs `anvil gen` on symbols.toml and settings.toml, builds
# the DLL and lays out build/plugin/<id>/. Adds artificer-check and, with
# -DEU4_DIR=<game folder>, artificer-deploy.
anvil_add_plugin(artificer SOURCES src/plugin.cpp src/popup_cleanup.cpp)

# For the IDE: copy the plugin into the game after every build, so a build
# (or F5) needs no separate artificer-deploy. Close the game first: Windows
# does not let a loaded DLL be replaced. CMakeUserPresets.json turns it on.
option(ARTIFICER_DEPLOY_ON_BUILD "Copy the plugin into EU4_DIR after every build" OFF)
if(ARTIFICER_DEPLOY_ON_BUILD AND EU4_DIR)
    add_custom_command(TARGET artificer POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory "${CMAKE_BINARY_DIR}/plugin/artificer"
                "${EU4_DIR}/anvil/plugins/artificer"
        COMMENT "deploy artificer to ${EU4_DIR}/anvil/plugins/artificer"
        VERBATIM)
endif()
