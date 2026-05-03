"""
Trading Behavior Analyzer — Flask web app for CSV fill analysis.

Flow: POST / receives CSV → parser.parse_uploaded_csv builds a normalized DataFrame
→ insights compute metrics, narrative lines, and breakdown dict → render index.html.
"""

from __future__ import annotations

from flask import Flask, render_template, request

from insights import (
    compute_breakdown,
    compute_metrics,
    generate_insights,
    generate_summary_lines,
)
from parser import parse_uploaded_csv

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# Display denomination for fees, notionals, and trade-size stats (export is treated as USD).
DISPLAY_CURRENCY = "USD"


@app.context_processor
def inject_display_currency() -> dict[str, str]:
    return {"currency": DISPLAY_CURRENCY}


@app.route("/", methods=["GET", "POST"])
def index():
    """Serve the analyzer UI; POST parses uploaded CSV and renders results."""
    if request.method == "GET":
        return render_template("index.html", warnings_ui=[])

    try:
        f = request.files.get("file")
        # Tuple: dataframe, parser warnings (logged only), optional meta for future use.
        df, _warnings, _meta = parse_uploaded_csv(f)
        # Parser warnings are logged server-side only; keep key defined for templates.
        warnings_ui: list[str] = []
        metrics = compute_metrics(df)
        insight_lines = generate_insights(metrics)
        summary_lines = generate_summary_lines(metrics)
        breakdown = compute_breakdown(df)
        return render_template(
            "index.html",
            ok=True,
            metrics=metrics,
            insights=insight_lines,
            summary_lines=summary_lines,
            breakdown=breakdown,
            warnings_ui=warnings_ui,
        )
    except ValueError as e:
        return render_template("index.html", error=str(e), warnings_ui=[])
    except Exception as e:
        app.logger.exception("Upload failed")
        return render_template(
            "index.html",
            error=f"Something went wrong while processing the file: {e}",
            warnings_ui=[],
        )


if __name__ == "__main__":
    app.run(debug=True)
