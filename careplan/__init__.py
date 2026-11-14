"""Care Plan Check: planner, tools and evaluation harness."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BARRIERS = ("transport", "food_access", "med_cost", "other_cost", "utilities", "housing",
            "caregiving", "language_literacy", "digital_access", "work_schedule")
NEEDS = ("transport", "food", "utilities", "med_cost", "housing", "caregiving", "language", "digital")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
