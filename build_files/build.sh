#!/bin/bash

set -ouex pipefail

# Copy the contents of system_files/ of the git repo to /
cp -avf "/ctx/system_files"/. /

### CN-PLAY console shell
# cage = single-app Wayland kiosk compositor, chromium = renders the CN-PLAY UI.
dnf5 install -y cage chromium

chmod +x /usr/bin/cn-play-session /usr/libexec/cn-play/bridge.py

# Brand the OS name shown in boot/about screens (keeps ID/VERSION_ID untouched).
sed -i 's/^PRETTY_NAME=.*/PRETTY_NAME="CN-PLAY (Bazzite)"/' /usr/lib/os-release

systemctl enable podman.socket
