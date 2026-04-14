"""
Video 1: Getting Started (~60 seconds)

Goal: Orient the viewer -- what is this app, what does it show, where do things live.
Audience: New users, stakeholders, anyone seeing the app for the first time.

Narrator voice: Calm, professional, unhurried.  Think "institutional training video",
not "product launch hype".  Assume the viewer has never seen the app before.

Prerequisites:
    - App running at DEMO_BASE_URL (default: http://localhost:8000)
    - Valid login credentials (DEMO_USERNAME / DEMO_PASSWORD)
    - Some data loaded so the dashboard isn't empty
"""

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from config import (
    DemoRunner,
    Scene,
    VideoScript,
    beat,
    long_pause,
    narration_pause,
    pause,
)

script = VideoScript(
    id="01-example",
    title="Getting Started",
    target_audience="All -- orientation video",
    duration_estimate="~60s",
    scenes=[
        Scene("Login", "Sign in and arrive at the dashboard", "~5s"),
        Scene("Dashboard overview", "Walk through the main interface", "~20s"),
        Scene("Navigation", "Tour the sidebar or menu structure", "~15s"),
        Scene("Closing", "Wrap up and tease next video", "~5s"),
    ],
)


def run(headless: bool = True):
    with DemoRunner(script.id, headless=headless) as demo:
        page = demo.page

        # -- Scene 1: Login -------------------------------------------------
        demo.login()
        demo.subtitle(
            "Welcome to the application.  Let's take a quick tour "
            "of what you'll see when you first log in."
        )

        # -- Scene 2: Dashboard overview ------------------------------------
        demo.screenshot("dashboard-overview")
        demo.subtitle(
            "The dashboard gives you everything at a glance. "
            "You can see recent activity, key metrics, and quick actions."
        )

        # Example: hover over key elements to draw attention
        # cards = page.locator(".stat-card")
        # for i in range(cards.count()):
        #     cards.nth(i).hover()
        #     beat()

        # Example: highlight an important element
        # demo.highlight_area(".alert-banner")

        # -- Scene 3: Navigation --------------------------------------------
        demo.subtitle(
            "The sidebar organizes everything into logical sections. "
            "Click any item to navigate directly."
        )

        # Example: hover down sidebar links
        # sidebar_links = page.locator(".sidebar a")
        # for i in range(sidebar_links.count()):
        #     link = sidebar_links.nth(i)
        #     if link.is_visible():
        #         link.hover()
        #         pause(0.5)

        # -- Scene 4: Closing -----------------------------------------------
        demo.subtitle(
            "That's the overview.  In the next video, we'll walk through "
            "the main workflow step by step."
        )
        demo.screenshot("dashboard-final")

    # After context manager exits, merge subtitles into video
    demo.merge_subtitles()
    demo.narrate()


if __name__ == "__main__":
    headless = "--headed" not in sys.argv
    run(headless=headless)
