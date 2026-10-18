"""Install the browser controls at the configured Moonlight Web boundary."""

# Embedded browser code has its own string/layout conventions.
# ruff: noqa: E501, RUF001

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
const bar = document.createElement('div');
bar.id = 'stream-controls';
bar.innerHTML = '<button id="stream-audio">Start audio</button> <button id="stream-size">Fit view</button> <span id="stream-state">Connecting…</span> <span>Click a tile to focus it, then use keyboard or controller.</span>';
bar.style.cssText='position:fixed;top:0;left:0;right:0;z-index:10000;background:#17191d;color:#fff;font:14px sans-serif;padding:8px;min-height:28px;display:flex;gap:12px;align-items:center';
for (const type of ['mousedown','mouseup','mousemove','keydown','keyup','wheel','touchstart','touchend'])
  bar.addEventListener(type, event => event.stopPropagation());
for (const b of bar.querySelectorAll('button')) b.style.cssText='padding:5px 10px;margin:0;font:14px sans-serif;min-height:0';
document.body.appendChild(bar);
const button = document.getElementById('stream-audio');
const status = document.getElementById('stream-state');
let native = true;
let audio;
let requestedAudio = false;
let playbackError = false;
async function startAudio() {
  if (!requestedAudio || !audio?.srcObject?.getAudioTracks().length) return;
  try { playbackError=false; audio.muted=false; await audio.play(); describe(); }
  catch (_) { playbackError=true; audio.muted=true; describe(); }
}
function describe() {
  const video = document.querySelector('video');
  const playing = audio && !audio.paused && !audio.muted && audio.readyState >= 2;
  button.textContent = playing ? 'Mute audio' : 'Start audio';
  if (playbackError) { status.textContent='Playback blocked. Click Start audio to retry.'; return; }
  status.textContent = `${video?.videoWidth || facts.size.width} × ${video?.videoHeight || facts.size.height} · ${native ? 'Native size' : 'Fit view'} · ${playing ? 'Audio on' : 'Audio off'}`;
}
function sizeVideo() {
  const video = document.querySelector('video');
  if (!video) return;
  const width = native ? facts.size.width : Math.min(innerWidth, (innerHeight-48)*facts.size.width/facts.size.height);
  const height = width*facts.size.height/facts.size.width;
  video.style.cssText=`position:absolute!important;top:48px!important;left:0!important;width:${width}px!important;height:${height}px!important;max-width:none!important;max-height:none!important;min-width:0!important;min-height:0!important;object-fit:fill!important;transform:none!important`;
  document.body.style.cssText='margin:0;overflow:auto!important';
  document.documentElement.style.overflow='auto';
  document.getElementById('root').style.cssText=`position:relative;width:${width}px;height:${height+48}px;overflow:visible`;
  describe();
}
function attach() {
  const next = document.querySelector('audio');
  if (next && next !== audio) {
    audio = next;
    for (const name of ['playing','pause','volumechange','loadeddata','emptied']) audio.addEventListener(name, describe);
    audio.addEventListener('stream-track-ready', startAudio);
    audio.addEventListener('canplay', startAudio);
    audio.srcObject?.addEventListener('addtrack', startAudio);
    startAudio();
    describe();
  }
  sizeVideo();
}
button.addEventListener('click', async () => {
  if (audio && !audio.paused && !audio.muted) {
    requestedAudio=false; audio.muted=true; describe(); return;
  }
  requestedAudio=true;
  status.textContent='Starting audio…';
  await startAudio();
  document.getElementById('input')?.focus();
});
document.getElementById('stream-size').onclick = function () {
  native=!native; this.textContent=native?'Fit view':'Native size'; sizeVideo();
};
new MutationObserver(attach).observe(document.getElementById('root'), {childList:true,subtree:true});
window.addEventListener('resize',sizeVideo);
document.addEventListener('loadedmetadata',attach,true);
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
