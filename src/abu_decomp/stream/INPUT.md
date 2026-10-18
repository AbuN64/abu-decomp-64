# Headless Moonlight input

Sunshine creates virtual HID devices through `/dev/uinput`. A compositor started
with only the wlroots headless backend never consumes those devices. The
`input_bridge.c` adapter forwards the configured virtual device family to the
Wayland display in its environment. It creates a virtual keyboard and pointer on
the display's first seat. Mouse clicks select the receiving window. Gamepads stay
native Linux input devices for SDL applications; the bridge grants the configured
input group access without translating their buttons into application keys.

Build with the checked-in Wayland protocol definitions, a C compiler, Wayland
client and xkbcommon development packages:

```sh
wayland-scanner client-header protocols/virtual-keyboard.xml virtual-keyboard.h
wayland-scanner private-code protocols/virtual-keyboard.xml virtual-keyboard.c
wayland-scanner client-header protocols/virtual-pointer.xml virtual-pointer.h
wayland-scanner private-code protocols/virtual-pointer.xml virtual-pointer.c
cc -Wall -Wextra -Werror -O2 -I. input_bridge.c virtual-keyboard.c virtual-pointer.c \
  $(pkg-config --cflags --libs wayland-client xkbcommon) -o input-bridge
```

Configure an owned service to run `input-bridge DEVICE_NAME_MARKER INPUT_GID`,
with the compositor's `XDG_RUNTIME_DIR` and `WAYLAND_DISPLAY`. It needs permission
to open, change permissions on, and exclusively grab the named virtual devices.
Physical devices whose names do not contain the explicit marker are never opened.
Existing devices are scanned once; hotplug uses inotify, and events use blocking
poll. Missing Wayland protocols or failed device access terminate the process.
The bridge exclusively grabs keyboard/mouse devices to prevent input leaking
into a different desktop. For named devices with the Linux gamepad button capability, the bridge relays
add/remove events through the container's own libudev netlink multicast group.
This is necessary when devtmpfs is visible but the host's hotplug notifications
stay in its network namespace. It never writes sysfs or sends events to the host.
Running SDL applications can discover the controller without a restart.

Start Sunshine before the bridge; stop the bridge before Sunshine. Include both
units in `stream.moonlight.units`, along with the browser and HTTPS services.
`stream down` stops this transport only. It must not stop tile apps or sway.

Set `stream.moonlight.browser` to `web_root`, `bitrate_kbps` and `fps`; the native
width and height come from `stream.up.output`. `stream up` installs persistent
playback controls into the pinned Moonlight Web page. Audio only reports playback
once the media element is playing and unmuted. Clicking while connecting records
the request and retries when the track is ready; a rejected play retains the
visible retry control. The video defaults to its source resolution with scrolling;
Fit view is an explicit user choice. These transport facts override stale browser
quality preferences. Use `http_scheme: "https"` and the HTTPS port in `stream.up`.
The Gamepad API needs a secure context and the browser must trust the certificate.

The relay header follows systemd 257 libudev monitor framing (40-byte header,
MurmurHash2 subsystem filter). Its integration proof must include a running SDL
application receiving a controller created after that application started.
