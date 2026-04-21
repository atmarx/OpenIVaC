"""
OpenIVaC -- Instructional Videos as Code

Core framework for recording automated web application walkthroughs using
Playwright.  Each video script imports from here to get consistent browser
setup, login handling, narration timing, subtitle generation, and video
encoding.

Adapt to your project:
  - Set BASE_URL / credentials via environment variables
  - Override DemoRunner.login() if your auth flow differs
  - Adjust VIEWPORT for your app's layout
  - Pick a TTS backend (openai-compatible or Fish Speech) and point at your endpoint
  - Everything else works as-is
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# ---------------------------------------------------------------------------
# Environment / defaults
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://localhost:8000")
USERNAME = os.environ.get("DEMO_USERNAME", "admin")
PASSWORD = os.environ.get("DEMO_PASSWORD", "admin")

# 1280x720 is a good balance: readable text, standard 16:9 for video
VIEWPORT = {"width": 1280, "height": 720}

# Output directory for recordings and screenshots
OUTPUT_DIR = Path(__file__).parent / "output"

# Pace control -- multiplier applied to all pauses.
# 1.0 = normal narration pace, 0.5 = fast preview, 2.0 = slow/dramatic
PACE = float(os.environ.get("DEMO_PACE", "1.0"))

# TTS narration.  Two backends supported:
#   "openai" -- any OpenAI-compatible TTS server (openedai-speech, etc.).
#               Fast, consistent, no voice cloning.  Default.
#   "fish"   -- Fish Speech 1.5 via its Gradio interface.  Voice cloning
#               from preloaded references, deterministic seeding for voice
#               continuity across cues.  Requires a Fish Speech endpoint.
#
# Point TTS_ENDPOINT at your chosen backend; OpenIVaC ships no hosted service.
TTS_BACKEND = os.environ.get("TTS_BACKEND", "openai").lower()  # "openai" or "fish"
TTS_ENDPOINT = os.environ.get("TTS_ENDPOINT", "http://localhost:8100/v1")
TTS_VOICE = os.environ.get("TTS_VOICE", "shimmer")
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.0"))
TTS_ENABLED = os.environ.get("TTS_ENABLED", "true").lower() in ("true", "1", "yes")

# Emotion tags on SubtitleCue are reserved metadata for a future backend that
# interprets them.  The current Fish Speech endpoint reads parenthetical
# prefixes like "(confident)" aloud as literal text, so we do not route them
# to the synth call.  Setting TTS_EMOTIONS=true logs a notice and otherwise
# has no effect -- leave emotion= on cues for future use.
if os.environ.get("TTS_EMOTIONS", "").lower() in ("true", "1", "yes"):
    print("  [config] TTS_EMOTIONS is set but emotion prefixing is disabled "
          "pending a backend that interprets it.")


# ---------------------------------------------------------------------------
# Voice cast -- named slots lock (reference_id, seed) for deterministic playback
# ---------------------------------------------------------------------------
# Each cue routes through a slot ("narrator" by default).  Fish Speech uses
# the slot's seed to keep timbre/tempo consistent across cues instead of
# drifting per-call.  Seeds are arbitrary but fixed -- any non-zero int works,
# the point is to lock them.
#
# Single-narrator scripts do nothing extra: all cues default to narrator.
# Two-voice call-and-answer scripts mark the "asker" cues with voice="asker".
#
# Scripts can override the cast in two ways:
#     from config import VOICE_CAST
#     VOICE_CAST["narrator"] = ("female3", 42)
# or declaratively on the VideoScript:
#     script = VideoScript(id=..., title=..., cast={"narrator": ("female3", 42)})

_FISH_VOICES = {"female1", "female2", "female3", "male1", "male2", "male3"}


def _default_narrator_voice() -> str:
    return TTS_VOICE if TTS_VOICE in _FISH_VOICES else "female2"


VOICE_CAST: dict[str, tuple[str, int]] = {
    "narrator": (_default_narrator_voice(), 42),
    "asker": ("female1", 137),
}


def resolve_voice_slot(slot: str,
                       overrides: Optional[dict[str, tuple[str, int]]] = None) -> tuple[str, int]:
    """Resolve a slot name to (reference_id, seed).

    Checks ``overrides`` first (per-runner/per-script), then VOICE_CAST.
    Falls back to narrator if the slot is unknown so scripts never break on
    typos.
    """
    if overrides and slot in overrides:
        return overrides[slot]
    if slot in VOICE_CAST:
        return VOICE_CAST[slot]
    return VOICE_CAST["narrator"]


# ---------------------------------------------------------------------------
# Narration / timing helpers
# ---------------------------------------------------------------------------

def pause(seconds: float) -> None:
    """Sleep for *seconds* scaled by the global PACE multiplier."""
    time.sleep(seconds * PACE)


def narration_pause() -> None:
    """Standard pause while the narrator is speaking (~2.5s)."""
    pause(2.5)


def beat() -> None:
    """Short pause to let the viewer register a UI change (~1s)."""
    pause(1.0)


def long_pause() -> None:
    """Longer pause for complex visuals or transitions (~4s)."""
    pause(4.0)


# ---------------------------------------------------------------------------
# Scene metadata
# ---------------------------------------------------------------------------

@dataclass
class Scene:
    """Metadata for a single scene within a video."""
    title: str
    description: str
    duration_estimate: str  # e.g. "~45s"


@dataclass
class VideoScript:
    """Top-level metadata for a demo video."""
    id: str                          # e.g. "01-getting-started"
    title: str
    target_audience: str
    duration_estimate: str           # e.g. "~5min"
    scenes: list[Scene] = field(default_factory=list)
    # Optional voice-cast override, merged over VOICE_CAST.  Lets a script
    # pin a specific voice/seed without mutating the module-level cast.
    cast: dict[str, tuple[str, int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subtitle tracking
# ---------------------------------------------------------------------------

@dataclass
class SubtitleCue:
    """A single subtitle entry with timing and text."""
    start: float   # seconds from recording start
    end: float     # seconds from recording start
    text: str
    emotion: str = ""  # Reserved metadata; not currently routed to TTS.
    voice: str = "narrator"  # voice-cast slot for Fish Speech backend


def _format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp format: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[SubtitleCue], path: Path) -> Path:
    """Write a list of SubtitleCues to an SRT file."""
    lines = []
    for i, cue in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}")
        lines.append(cue.text)
        lines.append("")  # blank line between cues
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Optional[Path] = None) -> Path:
    """
    Use ffmpeg to burn SRT subtitles into a video file.

    Returns the path to the output file.  Requires ffmpeg with libass.
    Tries flatpak-spawn --host first (for sandboxed environments like
    VSCode Flatpak), then falls back to direct ffmpeg.
    """
    if output_path is None:
        output_path = video_path.with_name(video_path.stem + "_subtitled.mp4")

    # Subtitle style: semi-transparent background, readable font
    style = (
        "FontName=Arial,"
        "FontSize=16,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BackColour=&H80000000,"
        "BorderStyle=4,"
        "Outline=1,"
        "Shadow=0,"
        "MarginV=40"
    )

    # Escape path for ffmpeg subtitle filter (colons and backslashes)
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")

    filter_str = f"subtitles='{srt_escaped}':force_style='{style}'"

    def _run_ffmpeg(prefix: list[str]) -> bool:
        cmd = prefix + [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", filter_str,
            "-c:a", "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    if shutil.which("flatpak-spawn"):
        if _run_ffmpeg(["flatpak-spawn", "--host"]):
            return output_path

    if _run_ffmpeg([]):
        return output_path

    raise RuntimeError(
        f"ffmpeg subtitle burn failed.  Merge manually:\n"
        f"  ffmpeg -i {video_path} -vf \"subtitles={srt_path}\" {output_path}"
    )


# ---------------------------------------------------------------------------
# TTS narration
# ---------------------------------------------------------------------------

# Matches sequences of 2+ uppercase letters that stand alone as words --
# acronyms like CSV, PDF, API, URL.  We space them so TTS reads each letter
# individually ("C S V") rather than guessing at pronunciation.
_ACRONYM_RE = re.compile(r'\b([A-Z]{2,})\b')

# Sonorous acronyms that read naturally as a word.  Keys are the source form
# in scripts; values are the spoken form.  Grow reactively -- these are
# starter entries; add your domain-specific terms here.
TTS_PRONUNCIATIONS: dict[str, str] = {
    "SQL": "sequel",
    "SaaS": "sass",
    "JSON": "jay-sawn",
    "YAML": "yammel",
    "OAuth": "oh-auth",
}

# Storage / bandwidth / time units that should be spoken in full when preceded
# by a number.  "10TB" is otherwise pronounced "ten tee bee" by most TTS.
TTS_UNIT_EXPANSIONS: dict[str, str] = {
    "TB": "terabytes", "GB": "gigabytes", "MB": "megabytes", "KB": "kilobytes",
    "PB": "petabytes", "TiB": "tebibytes", "GiB": "gibibytes", "MiB": "mebibytes",
    "Mbps": "megabits per second", "Gbps": "gigabits per second",
    "Kbps": "kilobits per second",
    "ms": "milliseconds", "hrs": "hours", "mins": "minutes", "sec": "seconds",
}

_UNIT_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(' +
    "|".join(sorted(TTS_UNIT_EXPANSIONS.keys(), key=len, reverse=True)) +
    r')\b'
)

# Safety net: scripts may still carry inline "(confident)" emotion prefixes
# from earlier experiments or copy-paste.  Strip leading parens before
# synthesis so no TTS backend reads them aloud as literal text.
_LEADING_PAREN_RE = re.compile(r'^\s*\([^)]+\)\s*')

_NUMBER_WORDS_SMALL = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
_NUMBER_WORDS_TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
]


def _int_to_words(n: int) -> str:
    """Convert a non-negative integer (0-9999) to spoken-English words."""
    if n < 0 or n > 9999:
        return str(n)
    if n < 20:
        return _NUMBER_WORDS_SMALL[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _NUMBER_WORDS_TENS[tens] + (f"-{_NUMBER_WORDS_SMALL[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        base = f"{_NUMBER_WORDS_SMALL[hundreds]} hundred"
        return f"{base} {_int_to_words(rest)}" if rest else base
    thousands, rest = divmod(n, 1000)
    base = f"{_NUMBER_WORDS_SMALL[thousands]} thousand"
    return f"{base} {_int_to_words(rest)}" if rest else base


def _number_to_words(token: str) -> str:
    """Convert a numeric token (possibly with decimal) into spoken words."""
    if "." in token:
        whole, frac = token.split(".", 1)
        whole_words = _int_to_words(int(whole)) if whole else "zero"
        frac_words = " ".join(_NUMBER_WORDS_SMALL[int(d)] for d in frac if d.isdigit())
        return f"{whole_words} point {frac_words}".strip()
    try:
        return _int_to_words(int(token))
    except ValueError:
        return token


def _expand_units(text: str) -> str:
    def repl(m: re.Match) -> str:
        number, unit = m.group(1), m.group(2)
        return f"{_number_to_words(number)} {TTS_UNIT_EXPANSIONS[unit]}"
    return _UNIT_RE.sub(repl, text)


def _apply_pronunciations(text: str) -> str:
    for src, spoken in TTS_PRONUNCIATIONS.items():
        text = re.sub(r'\b' + re.escape(src) + r'\b', spoken, text)
    return text


def _tts_preprocess(text: str, emotion: str = "") -> str:
    """Prepare subtitle text for TTS synthesis.

    Passes, in order:
      1. Unit expansion -- '10TB' -> 'ten terabytes'.
      2. Pronunciation dict -- 'SQL' -> 'sequel'.
      3. Acronym spacing -- remaining ALL-CAPS runs get 'C S V' treatment.
      4. Emotion-prefix safety net -- strip any leading '(emotion) ' that
         would otherwise be read aloud.

    ``emotion`` is accepted for signature compatibility with the cue pipeline
    but is no longer routed into the synth output.
    """
    processed = _expand_units(text)
    processed = _apply_pronunciations(processed)
    processed = _ACRONYM_RE.sub(lambda m: " ".join(m.group(1)), processed)
    processed = _LEADING_PAREN_RE.sub("", processed)
    return processed


def _fish_speech_synthesize(text: str, output_path: Path,
                            voice_ref: Optional[str] = None,
                            voice_seed: Optional[int] = None) -> bool:
    """Synthesize a single TTS clip via Fish Speech's Gradio interface.

    Fish Speech 1.5 exposes a Gradio web UI (not a REST /v1/tts endpoint).
    Uses gradio_client to call the inference function directly.

    When ``voice_ref`` / ``voice_seed`` are omitted, the narrator slot from
    VOICE_CAST is used.  A non-zero seed keeps rendered timbre consistent
    across cues; seed=0 lets Fish re-randomize per call and causes drift.

    Returns True on success, False on failure.
    """
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        print("  gradio_client not installed -- run: pip install gradio_client")
        return False

    endpoint = TTS_ENDPOINT.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]

    # reference_audio is a required FileData param even when reference_id
    # resolves server-side to a preloaded voice.  Ship a 1-sec silent WAV.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _tmp:
        _dummy = _tmp.name
    with wave.open(_dummy, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))

    if voice_ref is None or voice_seed is None:
        narrator_ref, narrator_seed = VOICE_CAST["narrator"]
        if voice_ref is None:
            voice_ref = narrator_ref
        if voice_seed is None:
            voice_seed = narrator_seed
    if voice_ref not in _FISH_VOICES:
        voice_ref = "female2"

    try:
        gc = Client(endpoint, verbose=False)
        result = gc.predict(
            text,
            voice_ref,
            handle_file(_dummy),
            "",
            0,
            200,
            0.7,
            1.2,
            0.7,
            voice_seed,
            "on",
            api_name="/partial",
        )
    except Exception as e:
        print(f"  Fish Speech Gradio call failed: {e}")
        return False

    audio_path, err = result if isinstance(result, tuple) else (result, None)
    if err:
        print(f"  Fish Speech error: {err}")
        return False
    if not audio_path:
        print("  Fish Speech: no audio path returned")
        return False

    src = Path(audio_path)
    if not src.exists():
        print(f"  Fish Speech: audio file not found at {src}")
        return False

    if src.suffix.lower() == ".mp3":
        shutil.copy(src, output_path)
        return True

    prefix = ["flatpak-spawn", "--host"] if shutil.which("flatpak-spawn") else []
    proc = subprocess.run(
        prefix + ["ffmpeg", "-y", "-i", str(src), str(output_path)],
        capture_output=True,
    )
    return proc.returncode == 0


NARRATION_CACHE_DIR = OUTPUT_DIR / ".narration_cache"

# Bump when preprocessor behavior changes so contaminated cached clips get
# regenerated instead of re-used.
_CACHE_VERSION = "v1"


def _synthesize_to_path(text: str, emotion: str, output_path: Path,
                        voice_slot: str = "narrator",
                        cast_overrides: Optional[dict[str, tuple[str, int]]] = None) -> bool:
    """Synthesize TTS audio for (text, emotion) into output_path.

    Dispatches based on TTS_BACKEND.  Returns True on success.
    """
    tts_text = _tts_preprocess(text, emotion)
    if TTS_BACKEND == "fish":
        voice_ref, voice_seed = resolve_voice_slot(voice_slot, cast_overrides)
        return _fish_speech_synthesize(tts_text, output_path, voice_ref, voice_seed)
    try:
        from openai import OpenAI
        client = OpenAI(base_url=TTS_ENDPOINT, api_key="unused")
        with client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=tts_text,
            speed=TTS_SPEED,
            response_format="mp3",
        ) as response:
            response.stream_to_file(str(output_path))
        return True
    except Exception as e:
        print(f"  openai-compatible TTS synthesis failed: {e}")
        return False


def _ffprobe_duration(path: Path) -> Optional[float]:
    prefix = ["flatpak-spawn", "--host"] if shutil.which("flatpak-spawn") else []
    try:
        proc = subprocess.run(
            prefix + [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _narration_cache_key(text: str, emotion: str = "",
                         voice_slot: str = "narrator",
                         cast_overrides: Optional[dict[str, tuple[str, int]]] = None) -> str:
    # Include the resolved (ref_id, seed) so a cast change invalidates the
    # cache cleanly.  Emotion is not part of the key: it's metadata-only.
    if TTS_BACKEND == "fish":
        voice_ref, voice_seed = resolve_voice_slot(voice_slot, cast_overrides)
        voice_key = f"{voice_slot}:{voice_ref}:{voice_seed}"
    else:
        voice_key = TTS_VOICE
    key_str = f"{_CACHE_VERSION}||{text}||{voice_key}||{TTS_BACKEND}"
    return hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]


def narration_cache_paths(text: str, emotion: str = "",
                          voice_slot: str = "narrator",
                          cast_overrides: Optional[dict[str, tuple[str, int]]] = None) -> tuple[Path, Path]:
    """Return (audio, meta) cache paths for a given cue."""
    key = _narration_cache_key(text, emotion, voice_slot, cast_overrides)
    return (
        NARRATION_CACHE_DIR / f"{key}.mp3",
        NARRATION_CACHE_DIR / f"{key}.json",
    )


def get_tts_duration(text: str, emotion: str = "",
                     voice_slot: str = "narrator",
                     cast_overrides: Optional[dict[str, tuple[str, int]]] = None) -> Optional[float]:
    """Return the duration (seconds) of TTS audio for (text, emotion).

    Cache hits skip synthesis.  Misses synthesize, measure, and write the
    clip + metadata into output/.narration_cache/.  Returns None on failure;
    callers fall back to a word-count estimate.
    """
    if not TTS_ENABLED:
        return None
    audio_path, meta_path = narration_cache_paths(text, emotion, voice_slot, cast_overrides)
    if meta_path.exists() and audio_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return float(data["duration"])
        except (ValueError, KeyError, OSError):
            pass
    NARRATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not _synthesize_to_path(text, emotion, audio_path, voice_slot, cast_overrides):
        return None
    duration = _ffprobe_duration(audio_path)
    if duration is None:
        return None
    voice_ref, voice_seed = resolve_voice_slot(voice_slot, cast_overrides)
    meta_path.write_text(json.dumps({
        "duration": duration,
        "text": text,
        "emotion": emotion,
        "voice_slot": voice_slot,
        "voice_ref": voice_ref,
        "voice_seed": voice_seed,
        "backend": TTS_BACKEND,
    }), encoding="utf-8")
    return duration


def parse_srt(srt_path: Path) -> list[SubtitleCue]:
    """Parse an SRT file back into SubtitleCue objects."""
    text = srt_path.read_text(encoding="utf-8")
    cues = []
    blocks = re.split(r"\n\n+", text.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        match = re.match(
            r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})",
            lines[1],
        )
        if not match:
            continue
        g = [int(x) for x in match.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        cue_text = " ".join(lines[2:])
        cues.append(SubtitleCue(start=start, end=end, text=cue_text))
    return cues


def add_narration(video_path: Path, srt_path: Path,
                  output_path: Optional[Path] = None,
                  cues: Optional[list[SubtitleCue]] = None,
                  cast_overrides: Optional[dict[str, tuple[str, int]]] = None) -> Optional[Path]:
    """
    Synthesize TTS audio from subtitle cues and mix into the video.

    Dispatches to the active backend (openai-compatible or Fish Speech),
    positions each clip at the cue's start timestamp via ffmpeg adelay, mixes
    all clips, then muxes the result into the video.  Produces a *_voiced.mp4
    alongside the existing *_subtitled.mp4.

    Pass ``cues`` to preserve in-memory emotion + voice slot metadata that
    would otherwise be lost in an SRT round-trip.  DemoRunner.narrate() does
    this; the ``--narrate-only`` CLI path falls back to parse_srt which drops
    emotion and voice (SRT has no field for either).

    Returns the output path, or None if TTS is disabled or fails.
    """
    if not TTS_ENABLED:
        return None

    if not video_path.exists() or not srt_path.exists():
        return None

    if output_path is None:
        output_path = video_path.with_name(
            video_path.stem.replace("_subtitled", "") + "_voiced.mp4"
        )

    if TTS_BACKEND == "fish":
        print(f"  Using Fish Speech backend at {TTS_ENDPOINT}")

    if cues is None:
        cues = parse_srt(srt_path)

    if not cues:
        print("  No subtitle cues found -- skipping narration.")
        return None

    tmpdir = tempfile.mkdtemp(prefix="openivac_tts_")
    clip_paths = []

    try:
        # Step 1: Pull each cue from the narration cache, synthesizing on miss.
        for i, cue in enumerate(cues):
            clip_path = Path(tmpdir) / f"cue_{i:03d}.mp3"
            slot = cue.voice
            cached_audio, cached_meta = narration_cache_paths(
                cue.text, cue.emotion, slot, cast_overrides,
            )
            if cached_audio.exists():
                print(f"  TTS [{i+1}/{len(cues)}]: [cached] {cue.text[:60]}...")
                shutil.copy(cached_audio, clip_path)
            else:
                tts_text = _tts_preprocess(cue.text, cue.emotion)
                print(f"  TTS [{i+1}/{len(cues)}]: {tts_text[:60]}...")
                if not _synthesize_to_path(cue.text, cue.emotion, clip_path,
                                           slot, cast_overrides):
                    print(f"  Skipping cue {i+1} -- TTS synthesis failed")
                    continue
                NARRATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy(clip_path, cached_audio)
                dur = _ffprobe_duration(clip_path)
                if dur is not None:
                    voice_ref, voice_seed = resolve_voice_slot(slot, cast_overrides)
                    cached_meta.write_text(json.dumps({
                        "duration": dur,
                        "text": cue.text,
                        "emotion": cue.emotion,
                        "voice_slot": slot,
                        "voice_ref": voice_ref,
                        "voice_seed": voice_seed,
                        "backend": TTS_BACKEND,
                    }), encoding="utf-8")
            clip_paths.append((cue.start, clip_path))

        # Step 2: Build a mixed audio track with clips at correct offsets
        filter_inputs = []
        filter_parts = []
        for idx, (start_sec, clip) in enumerate(clip_paths):
            delay_ms = int(start_sec * 1000)
            filter_inputs.extend(["-i", str(clip)])
            filter_parts.append(
                f"[{idx}:a]adelay={delay_ms}|{delay_ms}[a{idx}]"
            )

        mix_labels = "".join(f"[a{i}]" for i in range(len(clip_paths)))
        filter_parts.append(
            f"{mix_labels}amix=inputs={len(clip_paths)}:duration=longest:normalize=0[narration]"
        )
        filter_str = ";".join(filter_parts)

        mixed_audio = Path(tmpdir) / "narration.mp3"

        def _run_cmd(prefix: list[str], cmd: list[str]) -> bool:
            result = subprocess.run(
                prefix + cmd, capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"  ffmpeg error: {result.stderr[-500:]}")
            return result.returncode == 0

        mix_cmd = [
            "ffmpeg", "-y",
            *filter_inputs,
            "-filter_complex", filter_str,
            "-map", "[narration]",
            "-c:a", "libmp3lame",
            str(mixed_audio),
        ]

        prefix = []
        if shutil.which("flatpak-spawn"):
            prefix = ["flatpak-spawn", "--host"]

        print("  Mixing narration track...")
        if not _run_cmd(prefix, mix_cmd):
            if prefix:
                if not _run_cmd([], mix_cmd):
                    print("  Warning: narration mix failed.")
                    return None
            else:
                print("  Warning: narration mix failed.")
                return None

        # Step 3: Mux narration audio into the video
        print("  Muxing narration into video...")
        mux_cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(mixed_audio),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(output_path),
        ]

        if not _run_cmd(prefix, mux_cmd):
            if prefix:
                if not _run_cmd([], mux_cmd):
                    print("  Warning: narration mux failed.")
                    return None
            else:
                print("  Warning: narration mux failed.")
                return None

        print(f"  Voiced video: {output_path}")
        return output_path

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Browser / context management
# ---------------------------------------------------------------------------

class DemoRunner:
    """
    Manages Playwright browser lifecycle, video recording, and login.

    Usage:
        with DemoRunner("01-getting-started") as demo:
            demo.login()
            demo.subtitle("Welcome to the app.")
            demo.screenshot("landing-page")
            # ... interact with the page ...

        # After the with-block, merge subtitles into the video
        demo.merge_subtitles()

    The recording is saved to output/<video_id>/<video_id>.webm
    Screenshots land in the same directory as <name>.png files.
    """

    def __init__(self, video_id: str, headless: bool = True,
                 auto_capture: bool = True,
                 cast: Optional[dict[str, tuple[str, int]]] = None):
        self.video_id = video_id
        self.headless = headless
        self.auto_capture = auto_capture
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.output_path = OUTPUT_DIR / video_id
        self.output_path.mkdir(parents=True, exist_ok=True)
        self._recording_start: float = 0.0
        self._subtitles: list[SubtitleCue] = []
        self._step_count: int = 0
        # Per-runner voice-cast overrides -- merged over VOICE_CAST per lookup.
        self.cast: dict[str, tuple[str, int]] = dict(cast) if cast else {}

    def __enter__(self) -> "DemoRunner":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(self.output_path),
            record_video_size=VIEWPORT,
        )
        self.page = self._context.new_page()
        self._recording_start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._context:
            self._context.close()  # finalizes video recording
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

        for f in self.output_path.glob("*.webm"):
            final = self.output_path / f"{self.video_id}.webm"
            if f != final:
                f.rename(final)
            break

        if self._subtitles:
            srt_path = self.output_path / f"{self.video_id}.srt"
            write_srt(self._subtitles, srt_path)

    # -- Authentication ----------------------------------------------------

    def login(self, username: str = USERNAME, password: str = PASSWORD) -> None:
        """
        Navigate to the login page and authenticate.

        *** CUSTOMIZE THIS for your app's auth flow. ***

        The default assumes:
          - Login page at {BASE_URL}/login
          - Username field with id="username"
          - Password field with id="password"
          - Submit button with type="submit"
          - Successful login redirects to {BASE_URL}/
        """
        self.page.goto(f"{BASE_URL}/login")
        beat()
        self.page.fill("#username", username)
        beat()
        self.page.fill("#password", password)
        beat()
        self.page.click('button[type="submit"]')
        self.page.wait_for_url(f"{BASE_URL}/")
        beat()
        self._auto_capture("login")

    # -- Navigation --------------------------------------------------------

    def navigate(self, path: str) -> None:
        """Navigate to a path relative to BASE_URL."""
        self.page.goto(f"{BASE_URL}{path}")
        self.page.wait_for_load_state("networkidle")
        beat()
        self._auto_capture("navigate")

    def click_and_wait(self, selector: str, wait_for: str = "networkidle") -> None:
        """Click an element and wait for navigation/network to settle."""
        self.page.click(selector)
        self.page.wait_for_load_state(wait_for)
        beat()
        self._auto_capture("click")

    # -- Visual helpers ----------------------------------------------------

    def _auto_capture(self, label: str = "") -> Optional[Path]:
        """Capture a WebP screenshot if auto_capture is enabled."""
        if not self.auto_capture or not self.page:
            return None
        self._step_count += 1
        suffix = f"_{label}" if label else ""
        name = f"step_{self._step_count:03d}{suffix}"
        return self._save_screenshot(name, fmt="webp")

    def _save_screenshot(self, name: str, fmt: str = "webp") -> Path:
        png_path = self.output_path / f"{name}.png"
        self.page.screenshot(path=str(png_path))
        if fmt == "webp":
            webp_path = self.output_path / f"{name}.webp"
            with Image.open(png_path) as img:
                img.save(webp_path, "WEBP", quality=85)
            png_path.unlink()
            return webp_path
        return png_path

    def screenshot(self, name: str, fmt: str = "webp") -> Path:
        """Take a named screenshot.  Defaults to WebP."""
        return self._save_screenshot(name, fmt=fmt)

    def hover_over(self, selector: str) -> None:
        """Hover over an element to highlight it for the viewer."""
        self.page.hover(selector)
        beat()

    def slow_type(self, selector: str, text: str, delay: int = 80) -> None:
        """Type text character-by-character so the viewer can follow."""
        self.page.click(selector)
        self.page.type(selector, text, delay=delay)
        beat()
        self._auto_capture("type")

    def scroll_to(self, selector: str) -> None:
        """Scroll an element into view."""
        self.page.locator(selector).scroll_into_view_if_needed()
        beat()
        self._auto_capture("scroll")

    def highlight_area(self, selector: str) -> None:
        """Briefly outline an element to draw viewer attention."""
        self.page.eval_on_selector(
            selector,
            """el => {
                el.style.outline = '3px solid #e74c3c';
                el.style.outlineOffset = '2px';
            }""",
        )
        pause(2.0)
        self.page.eval_on_selector(
            selector,
            """el => {
                el.style.outline = '';
                el.style.outlineOffset = '';
            }""",
        )

    # -- Subtitles ---------------------------------------------------------

    def subtitle(self, text: str, duration: float = 0.0, emotion: str = "",
                 voice: str = "narrator") -> None:
        """
        Record a subtitle cue at the current timestamp.

        If duration is 0, it's derived from actual TTS audio length (falls
        back to a word-count estimate when TTS is off or synth fails).

        ``voice`` picks a voice-cast slot (default ``"narrator"``).  For
        call-and-answer scripts, mark question cues with ``voice="asker"``
        and the answers default to narrator.

        ``emotion`` is reserved metadata -- stored on the cue but not routed
        to the current TTS backend.
        """
        now = time.time() - self._recording_start

        if self._subtitles and self._subtitles[-1].end == 0.0:
            self._subtitles[-1].end = now

        if duration == 0.0:
            tts_duration = get_tts_duration(text, emotion, voice, self.cast)
            if tts_duration is not None:
                duration = tts_duration + 0.3  # 300ms visual tail after voice
            else:
                word_count = len(text.split())
                duration = max(2.5, word_count / 2.5)

        cue = SubtitleCue(start=now, end=now + duration, text=text,
                          emotion=emotion, voice=voice)
        self._subtitles.append(cue)

        pause(duration)

    def merge_subtitles(self) -> Optional[Path]:
        """
        Burn the SRT file into the video.  Call AFTER the context manager
        exits.  Returns the path to the subtitled MP4, or None if ffmpeg fails.
        """
        video_path = self.output_path / f"{self.video_id}.webm"
        srt_path = self.output_path / f"{self.video_id}.srt"

        if not video_path.exists() or not srt_path.exists():
            return None

        try:
            output = burn_subtitles(video_path, srt_path)
            return output
        except (subprocess.CalledProcessError, FileNotFoundError, RuntimeError) as e:
            print(f"  Warning: Could not burn subtitles -- {e}")
            print(f"  SRT file saved at: {srt_path}")
            print(f"  Merge manually: ffmpeg -i {video_path} -vf subtitles={srt_path} output.mp4")
            return None

    def narrate(self) -> Optional[Path]:
        """
        Add TTS voice narration to the subtitled video.  Call AFTER
        merge_subtitles().  Only runs when TTS_ENABLED is true.

        Passes in-memory cues so emotion + voice slot metadata is preserved.
        """
        subtitled = self.output_path / f"{self.video_id}_subtitled.mp4"
        srt_path = self.output_path / f"{self.video_id}.srt"
        return add_narration(subtitled, srt_path,
                             cues=self._subtitles if self._subtitles else None,
                             cast_overrides=self.cast or None)

    # -- Flash / toast dismissal -------------------------------------------

    def dismiss_flash(self, selector: str = ".flash-message") -> None:
        """Pause to let viewer read a flash/toast message, then continue."""
        flash = self.page.locator(selector)
        if flash.count() > 0:
            pause(1.5)
