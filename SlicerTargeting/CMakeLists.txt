cmake_minimum_required(VERSION 3.16.6)

project(SlicerTargeting)

#-----------------------------------------------------------------------------
# Extension meta-information (required by Slicer's Extension Wizard)
set(EXTENSION_HOMEPAGE "https://example.com/himx-cam")
set(EXTENSION_CATEGORY "Examples")
set(EXTENSION_CONTRIBUTORS "himx-cam project")
set(EXTENSION_DESCRIPTION "Click a point in a slice view; get a 3D target.")
set(EXTENSION_ICONURL "https://example.com/himx-cam/icon.png")
set(EXTENSION_SCREENSHOTURLS "https://example.com/himx-cam/screenshot.png")
set(EXTENSION_DEPENDS "NA")

#-----------------------------------------------------------------------------
# Slicer
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Modules
add_subdirectory(Targeting)
