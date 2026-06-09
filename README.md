# OpenIVaC — Instructional Videos as Code

**One script, two outputs.** A Python module drives a real Chromium browser
through a user flow while recording everything. The same script that proves
your documentation is correct produces the training video.

The video is the byproduct. The real output is **verified documentation** — if
the script can't click "Submit" because the button is actually labeled "Save &
Continue," your docs are wrong, and you find out before your users do.

## What you get

Every run produces, per video:

| Artifact | What it is | Who it's for |
|---|---|---|
| `*.webm` | Raw screen recording of the walkthrough | Archival |
| `*.srt` | Subtitles timed to each narration beat | Accessibility, voice-over |
| `*_subtitled.mp4` | Recording with burned-in subtitles | Humans |
| `*_voiced.mp4` | Subtitled video + synthesized narration | Humans (final cut) |
| `step_*.webp` | Auto-captured screenshot at every interaction | Agents, doc frames |

If the script exits 0, the flow works *and* the docs are current. Same action,
both guarantees.

## Quickstart

### Docker (recommended)

```bash
docker compose up -d tts          # start the TTS sidecar
docker compose run openivac       # record every video_*.py script
docker compose run openivac python run.py 01      # just one
docker compose run openivac python run.py --headed # watch it drive
```

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

export DEMO_BASE_URL=http://localhost:8000
export DEMO_USERNAME=admin
export DEMO_PASSWORD=admin
export TTS_ENABLED=true

# Narration backend (default is bark-local; point it at your Bark daemon).
# export TTS_BACKEND=bark-local && export BARK_ENDPOINT=http://localhost:8202
# Or swap to an OpenAI-compatible server with no GPU of your own:
# export TTS_BACKEND=openai && export TTS_ENDPOINT=http://localhost:8100/v1

python run.py
```

Requires Python 3.10+, ffmpeg (with libass for subtitle burning), and — if you
want narration — a running TTS endpoint.

## Writing a script

A script is a Python module in `scripts/` named `video_*.py`. It exports a
`script` (a `VideoScript` with metadata) and a `run(headless)` function that
drives the browser through `DemoRunner`:

```python
def run(headless: bool = True):
    with DemoRunner(script.id, headless=headless) as demo:
        demo.login()
        demo.subtitle("Welcome.  Let's take a look around.")
        demo.navigate("/dashboard")
        demo.subtitle("The dashboard shows your recent activity.")
        demo.screenshot("dashboard")
    demo.merge_subtitles()
    demo.narrate()
```

The full `DemoRunner` API, the doc-to-script generation loop, selector
strategy, and customization points (custom auth, viewport, pacing) live in
**[`agents.md`](agents.md)** — that's the deep guide, and it doubles as the
reference you feed an LLM when generating scripts.

## Narration (TTS)

Narration is optional (`TTS_ENABLED=false` skips it) and backend-pluggable.
Every cue routes through the same synth interface; you pick the engine with
`TTS_BACKEND`:

- **`bark-local`** *(default)* — Suno [Bark](https://github.com/suno-ai/bark)
  behind a small local HTTP daemon. Fully offline, MIT-licensed, and the
  backend we run in production. Natural, expressive narration with no hosted
  dependency. Each sentence is synthesized separately and stitched with a short
  silence gap (Bark degrades on long inputs), and a deterministic seed keeps the
  voice stable across a video. Picks a voice from Bark's built-in presets
  (`v2/en_speaker_0` … `v2/en_speaker_9`); supports the same per-script **voice
  cast** as `fish`. See [The Bark backend](#the-bark-backend) for the daemon
  contract.
- **`openai`** — any OpenAI-compatible TTS server, e.g.
  [openedai-speech](https://github.com/matatonic/openedai-speech). Fast and
  consistent; selects a voice by name (`shimmer`, `nova`, …). No seeding, no
  voice cloning.
- **`fish`** — [Fish Speech 1.5](https://github.com/fishaudio/fish-speech) via
  its Gradio interface. Preloaded reference voices and deterministic seeds, so
  a cue renders identically every time and timbre stays consistent across a
  video. Slower; self-hosted. Supports a per-script **voice cast** for
  call-and-answer narration (a "narrator" slot and an "asker" slot). Heads up:
  check Fish Speech's licence terms before shipping output — that friction is
  what pushed our production stack to Bark.

A pronunciation dictionary and unit-expansion table (`config.py`) keep TTS from
reading "10TB" as "ten tee bee" or "SQL" letter-by-letter. Subtitle text is
never altered — only the string sent to the synth. (On `bark-local` the
ALL-CAPS spacing pass is skipped: caps are Bark's own emphasis convention.)

### The Bark backend

`bark-local` talks to a tiny HTTP daemon that wraps Bark — OpenIVaC ships the
client, you run the daemon (any host with a GPU and the `bark` package). The
contract is one endpoint:

```
POST {BARK_ENDPOINT}/generate
{ "text": "One sentence of narration.",
  "voice_preset": "v2/en_speaker_9", "do_sample": true, "seed": 43,
  "semantic_temperature": 0.6, "coarse_temperature": 0.7,
  "fine_temperature": 0.35, "return_base64_wav": true }
→ 200 { "wav_base64": "<base64 WAV>", "sample_rate": 24000 }
```

Tuning knobs (all env vars, production defaults shown):

| Var | Default | What it does |
|---|---|---|
| `BARK_ENDPOINT` | `http://localhost:8202` | Daemon base URL |
| `BARK_SEMANTIC_TEMP` | `0.6` | Semantic-stage sampling temperature |
| `BARK_COARSE_TEMP` | `0.7` | Coarse-stage temperature |
| `BARK_FINE_TEMP` | `0.35` | Fine-stage temperature |
| `BARK_SILENCE_MS` | `250` | Silence inserted between stitched sentences |
| `BARK_SPEED` | `1.08` | Pitch-preserving atempo lift (Bark reads slow); folded into the cache key |

## Running

```bash
python run.py                 # all videos
python run.py 01              # by number
python run.py myapp           # by project prefix
python run.py --headed        # visible browser (debugging)
python run.py --narrate-only  # re-synth audio without re-recording
TTS_ENABLED=false python run.py   # quick silent pass
DEMO_PACE=1.5 python run.py       # slower, for longer narration gaps
```

## Project structure

```
OpenIVaC/
  config.py          # DemoRunner framework, timing, TTS, subtitles
  run.py             # CLI runner, auto-discovers video_*.py scripts
  agents.md          # the deep guide + LLM reference
  requirements.txt   # Python dependencies
  Dockerfile         # Playwright + ffmpeg container
  docker-compose.yml # app + TTS sidecar
  scripts/           # your video scripts
  docs/              # source docs to generate scripts from
  output/            # generated artifacts (gitignored)
```

## Origin

OpenIVaC is the standalone extraction of the IVaC framework that grew up inside
the [mydoulapage](https://mydoulapage.com) project and became fleet
infrastructure: any app with a frontend can adopt it, override `login()`, write
scripts, and get tested documentation as video.

## License

[Apache License 2.0](LICENSE) — © 2026 Andrew Marx.
