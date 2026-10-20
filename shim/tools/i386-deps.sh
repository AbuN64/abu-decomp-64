#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Host packages for the 32-bit Fast3D (libultraship) build on Debian 13 (trixie).
# The plan is simulated first and refused if it removes a package or upgrades an amd64
# Mesa, libstdc++, SDL or MIPS package. libdbus-1-dev: SDL's portal file dialog (the first-run
# ROM chooser) is compiled in only with the D-Bus headers. As root (CI containers) no sudo is used.
set -eu
PACKAGES="g++-14-multilib libspdlog-dev:i386 libfmt-dev:i386 libtinyxml2-dev:i386 libzip-dev:i386
nlohmann-json3-dev libgl-dev:i386 libegl-dev:i386 libgl1-mesa-dri:i386 libegl-mesa0:i386 libsdl3-dev:i386
libdbus-1-dev:i386"
if [ "$(id -u)" = 0 ]; then SUDO=${SUDO-}; else SUDO=${SUDO-sudo}; fi
dpkg --print-foreign-architectures | grep -qx i386 || $SUDO dpkg --add-architecture i386
$SUDO apt-get update -qq
plan=$(apt-get -s install $PACKAGES)
if printf "%s\n" "$plan" | grep -q "^Remv"; then echo "refused: plan removes packages" >&2; exit 1; fi
if printf "%s\n" "$plan" | grep -E "^Inst [^ ]+ \[" | grep -v ":i386" | grep -qiE "mesa|libstdc|sdl|mips"; then
    echo "refused: plan upgrades amd64 Mesa/libstdc++/SDL/MIPS packages" >&2; exit 1
fi
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q $PACKAGES
