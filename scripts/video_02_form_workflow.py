"""
Video 2: Completing a Form Workflow (~90 seconds)

Goal: Walk through a multi-step form from start to finish.
Audience: End users who need to fill out forms in the application.

This is a template for any multi-page form workflow.  Replace the
selectors and subtitle text with your actual form fields.

Prerequisites:
    - App running at DEMO_BASE_URL
    - Valid login credentials
    - Any prerequisite data (e.g., a project to attach the form to)
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
    id="02-form-workflow",
    title="Completing a Form",
    target_audience="End users -- task walkthrough",
    duration_estimate="~90s",
    scenes=[
        Scene("Login & navigate", "Get to the form", "~10s"),
        Scene("Fill page 1", "Basic information fields", "~25s"),
        Scene("Fill page 2", "Detail fields and selections", "~25s"),
        Scene("Review & submit", "Check the summary and submit", "~20s"),
        Scene("Confirmation", "Show the success state", "~10s"),
    ],
)


def run(headless: bool = True):
    with DemoRunner(script.id, headless=headless) as demo:
        page = demo.page

        # -- Scene 1: Login & navigate --------------------------------------
        demo.login()
        demo.subtitle("Let's walk through filling out a form from start to finish.")

        demo.navigate("/forms/new")
        demo.subtitle("Click 'New Form' to begin.  The wizard will guide you through each step.")

        # -- Scene 2: Fill page 1 -------------------------------------------
        demo.subtitle("Start with the basics -- name, description, and category.")

        # Replace these selectors with your actual form fields:
        # demo.slow_type("#name", "Q4 Budget Review")
        # demo.slow_type("#description", "Annual budget review for Q4 2026")
        # demo.click_and_wait("select#category")
        # page.select_option("select#category", "finance")
        # beat()
        # demo.click_and_wait('button:has-text("Next")')

        demo.subtitle("Fill in each field, then click Next to continue.")

        # -- Scene 3: Fill page 2 -------------------------------------------
        demo.subtitle("The second page asks for more detail.  Take your time here.")

        # demo.slow_type("#amount", "150000")
        # demo.slow_type("#justification", "Required for infrastructure upgrades")
        # demo.click_and_wait('button:has-text("Next")')

        # -- Scene 4: Review & submit ---------------------------------------
        demo.subtitle(
            "The review page shows everything you've entered.  "
            "Check it carefully before submitting."
        )
        # demo.screenshot("review-page")
        # demo.highlight_area(".review-summary")
        # long_pause()
        # demo.click_and_wait('button:has-text("Submit")')

        # -- Scene 5: Confirmation ------------------------------------------
        demo.subtitle(
            "You'll see a confirmation with a reference number.  "
            "Save this for your records."
        )
        # demo.dismiss_flash()
        demo.screenshot("confirmation")

    demo.merge_subtitles()
    demo.narrate()


if __name__ == "__main__":
    headless = "--headed" not in sys.argv
    run(headless=headless)
