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
  - Everything else works as-is
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
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

# TTS narration -- openedai-speech or any OpenAI-compatible TTS endpoint
TTS_ENDPOINT = os.environ.get("TTS_ENDPOINT", "http://localhost:8100/v1")
TTS_VOICE = os.environ.get("TTS_VOICE", "shimmer")
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.0"))
TTS_ENABLED = os.environ.get("TTS_ENABLED", "true").lower() in ("true", "1", "yes")


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


# ---------------------------------------------------------------------------
# Subtitle tracking
# ---------------------------------------------------------------------------

@dataclass
class SubtitleCue:
    """A single subtitle entry with timing and text."""
    start: float   # seconds from recording start
    end: float     # seconds from recording start
    text: str


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

    # Try host ffmpeg first (Flatpak sandboxes often lack libass)
    if shutil.which("flatpak-spawn"):
        if _run_ffmpeg(["flatpak-spawn", "--host"]):
            return output_path

    # Try direct ffmpeg
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


def _tts_preprocess(text: str) -> str:
    """Prepare subtitle text for TTS synthesis.

    Spaces out acronyms so they're read as individual letters.
    'Import from CSV source' -> 'Import from C S V source'
    """
    return _ACRONYM_RE.sub(lambda m: " ".join(m.group(1)), text)


def parse_srt(srt_path: Path) -> list[SubtitleCue]:
    """Parse an SRT file back into SubtitleCue objects."""
    text = srt_path.read_text(encoding="utf-8")
    cues = []
    # SRT blocks: index, timestamp line, text, blank line
    blocks = re.split(r"\n\n+", text.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        # lines[0] = index, lines[1] = timestamps, lines[2:] = text
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
                  output_path: Optional[Path] = None) -> Optional[Path]:
    """
    Synthesize TTS audio from subtitle cues and mix into the video.

    Calls an OpenAI-compatible TTS endpoint for each cue, positions the
    audio at the cue's start timestamp via ffmpeg adelay, mixes all clips
    together, then muxes the result into the video.  Produces a
    *_voiced.mp4 alongside the existing *_subtitled.mp4.

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

    from openai import OpenAI

    client = OpenAI(base_url=TTS_ENDPOINT, api_key="unused")
    cues = parse_srt(srt_path)

    if not cues:
        print("  No subtitle cues found -- skipping narration.")
        return None

    tmpdir = tempfile.mkdtemp(prefix="openivac_tts_")
    clip_paths = []

    try:
        # Step 1: Synthesize each cue
        for i, cue in enumerate(cues):
            clip_path = Path(tmpdir) / f"cue_{i:03d}.mp3"
            tts_text = _tts_preprocess(cue.text)
            print(f"  TTS [{i+1}/{len(cues)}]: {tts_text[:60]}...")
            with client.audio.speech.with_streaming_response.create(
                model=TTS_MODEL,
                voice=TTS_VOICE,
                input=tts_text,
                speed=TTS_SPEED,
                response_format="mp3",
            ) as response:
                response.stream_to_file(str(clip_path))
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
                 auto_capture: bool = True):
        self.video_id = video_id
        self.headless = headless
        self.auto_capture = auto_capture
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.output_path = OUTPUT_DIR / video_id
        self.output_path.mkdir(parents=True, exist_ok=True)
        # Subtitle tracking
        self._recording_start: float = 0.0
        self._subtitles: list[SubtitleCue] = []
        # Auto-capture step counter
        self._step_count: int = 0

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

        # Rename the auto-generated video file to something predictable
        for f in self.output_path.glob("*.webm"):
            final = self.output_path / f"{self.video_id}.webm"
            if f != final:
                f.rename(final)
            break

        # Write SRT subtitle file if any cues were recorded
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
        """Capture a WebP screenshot if auto_capture is enabled.

        Called automatically after each action method.  Screenshots are
        numbered sequentially: step_001_login.webp, step_002_navigate.webp,
        etc.
        """
        if not self.auto_capture or not self.page:
            return None
        self._step_count += 1
        suffix = f"_{label}" if label else ""
        name = f"step_{self._step_count:03d}{suffix}"
        return self._save_screenshot(name, fmt="webp")

    def _save_screenshot(self, name: str, fmt: str = "webp") -> Path:
        """Capture a screenshot and convert to the requested format.

        Playwright captures PNG natively.  For WebP, we capture PNG then
        convert via Pillow -- same pixel data, ~60-80% smaller files.
        """
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
        """Take a named screenshot.  Defaults to WebP.

        Use fmt="png" if you need lossless output for specific comparisons.
        """
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
        """
        Briefly outline an element to draw viewer attention.
        Adds a red border, pauses, then removes it.
        """
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

    def subtitle(self, text: str, duration: float = 0.0) -> None:
        """
        Record a subtitle cue at the current timestamp.

        If duration is 0, it's estimated from word count (~150 wpm).
        Also pauses execution so the subtitle stays on screen during
        recording.
        """
        now = time.time() - self._recording_start

        # Close the previous open-ended cue
        if self._subtitles and self._subtitles[-1].end == 0.0:
            self._subtitles[-1].end = now

        # Estimate duration from text length if not specified:
        # ~150 words per minute = ~2.5 words per second
        if duration == 0.0:
            word_count = len(text.split())
            duration = max(2.5, word_count / 2.5)  # at least 2.5s

        cue = SubtitleCue(start=now, end=now + duration, text=text)
        self._subtitles.append(cue)

        # Pause so the subtitle is visible during recording
        pause(duration)

    def merge_subtitles(self) -> Optional[Path]:
        """
        Burn the SRT file into the video.  Call AFTER the context manager
        exits (i.e., after the `with DemoRunner(...) as demo:` block).

        Returns the path to the subtitled MP4, or None if ffmpeg fails.
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

        Returns the path to the voiced MP4, or None if skipped/failed.
        """
        subtitled = self.output_path / f"{self.video_id}_subtitled.mp4"
        srt_path = self.output_path / f"{self.video_id}.srt"
        return add_narration(subtitled, srt_path)

    # -- Flash / toast dismissal -------------------------------------------

    def dismiss_flash(self, selector: str = ".flash-message") -> None:
        """Pause to let viewer read a flash/toast message, then continue."""
        flash = self.page.locator(selector)
        if flash.count() > 0:
            pause(1.5)
