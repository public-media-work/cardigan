
```json
{
  "phase_results": {
    "analyst": {
      "status": "pass",
      "flags": []
    },
    "formatter": {
      "status": "fail",
      "flags": [
        "Formatter contract violation: missing required metadata header block (Project, Program, Date Processed, Agent, Model), missing <!-- REVIEW NOTES: ... --> block immediately after header, and missing closing **Status:** footer. Formatted output begins directly with speaker dialogue instead of required structural elements."
      ]
    },
    "seo": {
      "status": "fail",
      "flags": [
        "Title character count exceeds 80-character limit. Recommended title 'Mega-donors reshape Wisconsin's 2026 governor race amid gas prices and corruption fights' is 88 characters (exceeds <80 requirement). Documented as '80 characters exactly' but manual count confirms 88.",
        "Short description character count exceeds 90-character limit. Final recommended 'Mega-donors fuel Republican spending in Wisconsin's 2026 governor race amid gas price and anti-corruption concerns.' is 113 characters (exceeds <90 requirement). Documented as '88 characters' but manual count confirms 113."
      ]
    }
  },
  "overall": "fail"
}
```
