"""SEO-2: the SEO phase must not run on Haiku until limits are enforced in code.

The SEO prompt sets hard character limits (title 80, short 90, long 350) and
says the pipeline verifies them -- but the style-engine SEO post-stage is off,
so nothing does; the model's own count is the only check. Haiku 4.5 miscounts
by 10-70% (6POL0213: claimed 80/88/340, measured 88/115/596), and on job 24
(6POL0214) it shipped a 91-character short description. Sonnet counted
correctly on both its 6POL0213 runs.

``phase_models`` wins over the backend's model in ``LLMClient.chat``, so both
config entries are pinned. Lift this pin when ``routing.style_engine.phases.
seo.post`` enforces the limits deterministically.
"""

import json
from pathlib import Path

from api.routers.config import DEFAULT_PHASE_MODELS

_CONFIG = json.loads(Path("config/llm-config.json").read_text())


def test_seo_phase_model_is_not_haiku():
    assert "haiku" not in _CONFIG["phase_models"]["seo"]
    assert "haiku" not in DEFAULT_PHASE_MODELS["seo"]


def test_seo_phase_backend_is_not_cheapskate():
    assert _CONFIG["phase_backends"]["seo"] != "openrouter-cheapskate"
