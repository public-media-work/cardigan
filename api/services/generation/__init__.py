"""The V5 generation engine (#427): a context bundle and one generator per item.

Jobs run on it only on an instance configured with
``routing.generation.engine: "items"``; a V4 instance never runs it. See
``runner`` for the rules it enforces.
"""
