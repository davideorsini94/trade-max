"""Weekly evaluation package (blueprint section 7).

Contains the scoring/attribution engine (``evaluator``) and the LLM-backed
feedback loop (``feedback``). Submodules are imported explicitly by callers
(e.g. ``from app.evaluation.evaluator import run_weekly_evaluation``); nothing
is re-exported here to keep import order simple and cycle-free.
"""
