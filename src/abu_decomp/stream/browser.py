"""Install the browser controls at the configured Moonlight Web boundary."""

# Embedded browser code has its own string/layout conventions.
# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Moonlight Web 2.10's initially empty media source can remain HAVE_NOTHING
# after addTrack. Bind a populated stream and signal the pending user gesture.
AUDIO_TRACK_OLD = "        this.stream.addTrack(track);"
AUDIO_TRACK_NEW = """        this.stream = new MediaStream([track]);
        this.audioElement.srcObject = this.stream;
        this.audioElement.dispatchEvent(new Event('stream-track-ready'));"""

SCRIPT = r"""
const facts = window.streamFacts;
// These are transport facts, not remembered per-browser quality preferences.
let saved;
try { saved = JSON.parse(localStorage.getItem('mlSettings') || '{}') || {}; }
catch (_) { saved = {}; }
if (typeof saved !== 'object' || Array.isArray(saved)) saved = {};
Object.assign(saved, {videoSize:'custom', videoSizeCustom:facts.size,
  bitrate:facts.bitrate, fps:facts.fps, videoCodec:'h264', dataTransport:'webrtc',
  playAudioLocal:true, forceVideoElementRenderer:true, mouseMode:'follow'});
localStorage.setItem('mlSettings', JSON.stringify(saved));
// No chrome: the page is the stream, always fitted to the window, with sound as soon as the browser allows it.
const style = document.createElement('style');
style.textContent = 'html,body{margin:0!important;padding:0!important;overflow:hidden!important;background:#000!important}'
  + '#root{position:fixed!important;inset:0!important;overflow:hidden!important;background:#000}'
  + '.sidebar-overlay{opacity:.15;transition:opacity .2s}.sidebar-overlay:hover,.sidebar-overlay.sidebar-show{opacity:1}';
document.head.appendChild(style);
let audio;
let blocked = false;
async function startAudio() {
  if (!audio?.srcObject?.getAudioTracks().length) return;
  try { audio.muted = false; await audio.play(); blocked = false; }
  catch (_) {
    // Unmuted autoplay needs a gesture: play muted now, unmute on the first click or key anywhere.
    blocked = true; audio.muted = true;
    try { await audio.play(); } catch (_) {}
  }
}
function unlock() { if (blocked) startAudio(); }
for (const type of ['pointerdown', 'mousedown', 'keydown', 'touchstart'])
  document.addEventListener(type, unlock, {capture: true, passive: true});
function sizeVideo() {
  const video = document.querySelector('video');
  if (!video) return;
  const ratio = facts.size.width / facts.size.height;
  const width = Math.min(innerWidth, innerHeight * ratio);
  const height = width / ratio;
  video.style.cssText = `position:fixed!important;left:${(innerWidth - width) / 2}px!important;top:${(innerHeight - height) / 2}px!important;width:${width}px!important;height:${height}px!important;max-width:none!important;max-height:none!important;min-width:0!important;min-height:0!important;object-fit:fill!important;transform:none!important`;
}
function attach() {
  const next = document.querySelector('audio');
  if (next && next !== audio) {
    audio = next;
    audio.addEventListener('stream-track-ready', startAudio);
    audio.addEventListener('canplay', startAudio);
    audio.srcObject?.addEventListener('addtrack', startAudio);
    startAudio();
  }
  sizeVideo();
}
new MutationObserver(attach).observe(document.getElementById('root'), {childList: true, subtree: true});
window.addEventListener('resize', sizeVideo);
document.addEventListener('loadedmetadata', attach, true);
attach();
"""


def install(config: dict[str, Any], output: dict[str, Any]) -> None:
    root = Path(config["web_root"])
    page = root / "stream.html"
    html = page.read_text()
    player = root / "stream/audio/audio_element.js"
    player_source = player.read_text()
    if AUDIO_TRACK_OLD in player_source:
        player_source = player_source.replace(AUDIO_TRACK_OLD, AUDIO_TRACK_NEW)
    elif AUDIO_TRACK_NEW not in player_source:
        raise ValueError("unsupported Moonlight audio player; expected the pinned track adapter")
    start, end = "<!-- taste-controls -->", "<!-- /taste-controls -->"
    if start in html:
        before, rest = html.split(start, 1)
        _, after = rest.split(end, 1)
        html = before + after.lstrip("\n")
    # Retire the old one-shot button; it falsely hid on volumechange before play succeeded.
    if '<button id="stream-audio"' in html:
        before, rest = html.split('<button id="stream-audio"', 1)
        _, after = rest.split("</script>", 1)
        html = before + after
    facts = {
        "size": {"width": int(output["width"]), "height": int(output["height"])},
        "bitrate": int(config["bitrate_kbps"]),
        "fps": int(config["fps"]),
    }
    if min(facts["size"].values()) <= 0 or facts["bitrate"] <= 0 or facts["fps"] <= 0:
        raise ValueError("browser output size, bitrate and fps must be positive")
    controls = start + "<script>window.streamFacts=" + json.dumps(facts) + ";" + SCRIPT + "</script>" + end
    if "</body>" not in html:
        raise ValueError("Moonlight stream page has no body")
    player.write_text(player_source)
    page.write_text(html.replace("</body>", controls + "\n</body>"))
