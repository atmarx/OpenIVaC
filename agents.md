# OpenIVaC -- Agents Guide

You are generating video scripts that automate browser walkthroughs of a web application.  Each script drives a real Playwright browser, records the session, generates subtitles, and optionally adds TTS narration.  The same script that proves the documentation is correct produces the training video.

## What this repo does

**One script, two outputs.**  A Python module drives Chromium through a user flow while recording everything.  You get:

1. A `.webm` screen recording of the entire walkthrough
2. An `.srt` subtitle file timed to each narration beat
3. A `_subtitled.mp4` with burned-in subtitles
4. A `_voiced.mp4` with synthesized narration audio (when TTS is enabled)
5. Auto-captured `.webp` screenshots at every interaction step

The video is the byproduct.  The real output is **verified documentation**.  If your script can't click "Submit" because the button is actually labeled "Save & Continue", the doc is wrong.  Fix the doc, fix the script, now you have both an accurate walkthrough AND proof that it works.

## Setup

### Docker (recommended)

```bash
docker compose up -d tts          # Start TTS sidecar
docker compose run openivac       # Record all videos

# Or run a specific video
docker compose run openivac python run.py 01

# Or with visible browser for debugging
docker compose run openivac python run.py --headed
```

### Local (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Set environment variables
export DEMO_BASE_URL=http://localhost:8000
export DEMO_USERNAME=admin
export DEMO_PASSWORD=admin

# TTS -- pick a backend and point at your endpoint.
export TTS_BACKEND=bark-local                  # "bark-local" (default), "openai", or "fish"
export BARK_ENDPOINT=http://localhost:8202     # local Bark daemon
# or:
# export TTS_BACKEND=openai
# export TTS_ENDPOINT=http://localhost:8100/v1 # OpenAI-compatible
# export TTS_BACKEND=fish
# export TTS_ENDPOINT=http://your-fish-endpoint:8200
export TTS_ENABLED=true

python run.py
```

Requires: Python 3.10+, ffmpeg (with libass for subtitle burning), a running TTS endpoint.

### Picking a TTS backend

- **`bark-local`** (default) -- Suno [Bark](https://github.com/suno-ai/bark) behind a small local HTTP daemon (`POST /generate` -> base64 WAV).  Fully offline, MIT-licensed, expressive -- the backend we run in production.  Synthesizes one sentence at a time and stitches the clips with a `BARK_SILENCE_MS` gap (Bark degrades on long inputs), and a deterministic `seed` keeps the voice stable across a video.  Picks a voice from Bark's built-in presets (`v2/en_speaker_0` .. `v2/en_speaker_9`) via the voice cast.  Knobs: `BARK_ENDPOINT`, `BARK_SEMANTIC_TEMP`, `BARK_COARSE_TEMP`, `BARK_FINE_TEMP`, `BARK_SILENCE_MS`, `BARK_SPEED` (pitch-preserving atempo lift, default 1.08, folded into the cache key).
- **`openai`** -- any OpenAI-compatible TTS server (e.g. [openedai-speech](https://github.com/matatonic/openedai-speech)).  Fast, consistent, no voice cloning, no per-clip seed control.  Uses `TTS_VOICE` (e.g. `shimmer`, `nova`) for voice selection.
- **`fish`** -- [Fish Speech 1.5](https://github.com/fishaudio/fish-speech) via its Gradio interface.  Supports preloaded reference voices and deterministic seeding, so the same cue renders identically every time and voices stay consistent across cues in a single video.  Slower than the openai backend; requires you to host Fish Speech yourself.  Check its licence before shipping output -- that friction is what moved our production stack to Bark.

## How to write a video script

Every script is a Python module in `scripts/` named `video_*.py`.  It must export two things:

1. **`script`** -- a `VideoScript` instance with metadata (id, title, audience, scenes)
2. **`run(headless: bool)`** -- a function that drives the browser

### Minimal template

```python
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from config import DemoRunner, Scene, VideoScript, beat, pause

script = VideoScript(
    id="myapp-01-getting-started",
    title="Getting Started with MyApp",
    target_audience="New users",
    duration_estimate="~60s",
    scenes=[
        Scene("Login", "Authenticate and land on dashboard", "~10s"),
        Scene("Overview", "Walk through the main interface", "~30s"),
        Scene("Closing", "Wrap up", "~5s"),
    ],
)

def run(headless: bool = True):
    with DemoRunner(script.id, headless=headless) as demo:
        page = demo.page

        demo.login()
        demo.subtitle("Welcome to MyApp.  Let's see what's here.")

        demo.navigate("/dashboard")
        demo.subtitle("The dashboard shows your recent activity and key metrics.")

        demo.screenshot("dashboard")
        demo.subtitle("That's the basics.  Next up: creating your first project.")

    demo.merge_subtitles()
    demo.narrate()
```

### The DemoRunner API

**Lifecycle:**
- `DemoRunner(video_id, headless=True, auto_capture=True)` -- context manager, starts Playwright + video recording
- `demo.page` -- the Playwright `Page` object for direct interaction
- `demo.merge_subtitles()` -- call AFTER the `with` block to burn subtitles
- `demo.narrate()` -- call AFTER merge to add TTS audio

**Authentication:**
- `demo.login(username, password)` -- navigates to `/login`, fills `#username` + `#password`, clicks submit.  **Override this** if your app uses a different auth flow (SSO, MFA, custom form).

**Navigation:**
- `demo.navigate("/path")` -- go to BASE_URL + path, wait for network idle
- `demo.click_and_wait("selector")` -- click element, wait for navigation to settle

**Visual:**
- `demo.screenshot("name")` -- named screenshot (auto WebP)
- `demo.hover_over("selector")` -- hover to highlight for the viewer
- `demo.slow_type("selector", "text")` -- visible character-by-character typing
- `demo.scroll_to("selector")` -- scroll element into view
- `demo.highlight_area("selector")` -- red border flash to draw attention
- `demo.dismiss_flash()` -- pause for toast/flash messages

**Narration:**
- `demo.subtitle("Text to show and speak.")` -- records a timed subtitle cue, pauses execution for the duration
- `demo.subtitle(text, voice="asker")` -- route this cue through the "asker" voice slot (see Voice cast below)
- `demo.subtitle(text, emotion="(confident)")` -- attach emotion metadata to the cue.  **Reserved for future use** -- the current Fish Speech endpoint doesn't interpret parenthetical prefixes as emotion directives, so emotion is stored on the cue but not routed to the synth call.  Leave `emotion=` on cues you've annotated; it'll take effect if/when a backend supports it.

**Timing:**
- `beat()` -- 1s pause (UI change registered)
- `pause(seconds)` -- custom pause (scaled by PACE)
- `narration_pause()` -- 2.5s (narrator speaking)
- `long_pause()` -- 4s (complex visuals)

All timing functions respect the `DEMO_PACE` multiplier.

## How to generate a script from documentation

This is the closed-loop workflow:

1. **Read the source doc** -- a step-by-step walkthrough, user guide, or tutorial
2. **Map each step to DemoRunner calls** -- "Click the Submit button" becomes `demo.click_and_wait('button[type="submit"]')`
3. **Write subtitle text** -- narrator-friendly rewording of each step
4. **Run the script** against a live instance of the app
5. **Fix what breaks** -- if a selector doesn't match, the doc might be wrong (or the selector needs updating)
6. **Repeat** until the script runs clean

### The back-and-forth is the feature

The script fails because the button text changed?  Good -- now you know the doc is stale.  Fix the doc, update the script, re-run.  When the video records cleanly from start to finish, you have proof that the documentation matches the live application.

### Selector strategy

- Prefer `data-testid` attributes when available
- Fall back to semantic selectors: `button:has-text("Submit")`, `h2:has-text("Dashboard")`
- Use `#id` selectors for form fields
- Avoid fragile CSS class selectors (`.btn-primary-v2`) -- they change with UI updates
- When a selector breaks, check: did the UI change, or was the doc wrong?

### Generating with an LLM

Feed the LLM:
1. The source documentation (markdown or HTML)
2. This file (`agents.md`) for API reference
3. One or two example scripts from `scripts/`

Ask it to produce a `video_*.py` module.  Review the output -- the LLM will get the structure right but may guess wrong on selectors.  Run it, fix the selectors, iterate.

## How to run

```bash
# All videos
python run.py

# By number, name, or project prefix
python run.py 01
python run.py getting-started
python run.py myapp

# Visible browser (debugging)
python run.py --headed

# Re-narrate without re-recording
python run.py --narrate-only
python run.py --narrate-only myapp
```

## Customization points

### Authentication

Override `DemoRunner.login()` for your auth flow.  The default assumes a simple username/password form at `/login`.  For SSO, MFA, or other flows:

```python
class MyDemoRunner(DemoRunner):
    def login(self, username=None, password=None):
        self.page.goto(f"{BASE_URL}/sso/login")
        # ... your auth flow here ...
        self.page.wait_for_url(f"{BASE_URL}/dashboard")
        beat()
```

### Headed mode for credential entry

Some environments (like Azure Virtual Desktop) require domain credentials that can't be automated.  Run `--headed` to show the browser, let the user type credentials manually, then the script continues from there.

### Viewport

Change `VIEWPORT` in `config.py` for different resolutions.  Default is 1280x720 (720p, good balance of readability and file size).

### TTS voice

Set `TTS_VOICE` environment variable.  Available voices depend on your TTS endpoint.  Default is "shimmer" (clear, professional on the openai backend).

### Voice cast (Bark and Fish Speech backends)

Both the `bark-local` and `fish` backends support deterministic seeding.  OpenIVaC routes every cue through a named **slot** -- by default, `"narrator"`.  Each slot locks a `(voice, seed)` tuple, so voice timbre and tempo stay consistent across cues in the same video instead of drifting per call.

Each backend resolves slots from its own cast table in `config.py` -- Bark uses preset names, Fish uses reference-voice names:

```python
# Fish Speech: reference-voice name + seed
VOICE_CAST = {
    "narrator": ("female2", 42),   # the primary walkthrough voice
    "asker":    ("female1", 137),  # the "how do I do this?" call voice
}

# Bark: built-in preset + seed (v2/en_speaker_0 .. v2/en_speaker_9)
BARK_VOICE_DEFAULTS = {
    "narrator": ("v2/en_speaker_9", 43),   # production narrator
    "asker":    ("v2/en_speaker_9", 137),
}
```

Seeds are arbitrary integers; just keep them **fixed and non-zero**.  A zero seed lets the backend re-randomize per call, which causes the drift the slot system exists to prevent.

**Single-narrator scripts** (most walkthroughs) do nothing extra -- all cues default to the narrator slot and a single voice walks the viewer through the entire video.

**Call-and-answer scripts** (e.g. "How do I do this?" / "I'm glad you asked") mark question cues with `voice="asker"`:

```python
demo.subtitle("How do I set a budget?", voice="asker")
demo.subtitle("Great question -- click the Budget tab on the left.", voice="narrator")
```

**Script-level cast override.**  Pin a specific voice per script without mutating the module-level cast:

```python
script = VideoScript(
    id="myapp-mdp-01",
    title="Tour",
    target_audience="New users",
    duration_estimate="~90s",
    cast={"narrator": ("female3", 42)},  # this script uses female3
)

def run(headless: bool = True):
    with DemoRunner(script.id, headless=headless, cast=script.cast) as demo:
        ...
```

The openai backend ignores voice slots -- it keys on `TTS_VOICE` alone.  Slots apply when `TTS_BACKEND=bark-local` or `TTS_BACKEND=fish`.  A script-level `cast` override works for both: pass `("v2/en_speaker_X", seed)` for Bark or `("female3", seed)` for Fish.

### Pronunciation dictionary

Some acronyms read naturally as words ("SMB" is "essembee", not "ess em bee").  Numbers + storage units need expansion ("10TB" is "ten terabytes", not "ten tee bee").  Two dicts in `config.py` handle both:

```python
TTS_PRONUNCIATIONS = {
    "SQL": "sequel",
    "SaaS": "sass",
    "JSON": "jay-sawn",
    # add your domain-specific terms here
}

TTS_UNIT_EXPANSIONS = {
    "TB": "terabytes", "GB": "gigabytes", "MB": "megabytes",
    "Mbps": "megabits per second",
    # etc.
}
```

Subtitle text is untouched -- only the text fed to the TTS model gets substituted.  Generic acronyms not in the dict still fall back to the letter-by-letter spacing behavior (CSV -> "C S V").  Grow the dict reactively when you hear a new mispronunciation.

## Known gotchas

- **Selector fragility** -- web UIs change.  When a video breaks after a UI update, it's telling you something.  Fix the selector, but also ask: does the user doc need updating too?
- **Timing sensitivity** -- some pages load slowly.  Use `page.wait_for_selector()` or `page.wait_for_load_state("networkidle")` before interacting.  The `DEMO_PACE` multiplier helps but doesn't fix race conditions.
- **TTS acronym handling** -- the framework auto-spaces uppercase acronyms (CSV -> C S V).  If TTS still mispronounces something, spell it out in the subtitle text.
- **Flatpak-spawn** -- if running inside a Flatpak sandbox (e.g., VSCode Flatpak), ffmpeg calls route through `flatpak-spawn --host` automatically.  If you're not in a Flatpak, this is a no-op.
- **WebM to MP4** -- Playwright records WebM natively.  The subtitle burn step converts to MP4.  If you need WebM output, use the raw recording directly.

## Project structure

```
OpenIVaC/
  config.py          # DemoRunner framework, timing helpers, TTS, subtitles
  run.py             # CLI runner, auto-discovers video_*.py scripts
  requirements.txt   # Python dependencies
  Dockerfile         # Playwright + ffmpeg container
  docker-compose.yml # App + TTS sidecar
  agents.md          # This file -- the handoff doc
  scripts/           # Your video scripts go here
    video_01_example.py
    video_02_form_workflow.py
  docs/              # Source documentation to generate scripts from
  output/            # Generated artifacts (gitignored)
```
