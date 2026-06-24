#!/usr/bin/env python3
"""
Dashboard server with API proxy.
Browser talks only to port 8080 — no CORS issues ever.
All /api/* requests are forwarded to Flask API on port 5000.
"""

from flask import Flask, send_from_directory, request, Response
import requests as req
import os

app      = Flask(__name__)
DASH_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL  = "http://localhost:5000"


# ── Proxy all /api/* → localhost:5000 ───────────────────
@app.route("/api/<path:path>", methods=["GET","POST","PUT","DELETE"])
def proxy(path):
    url     = f"{API_URL}/{path}"
    headers = {
        k: v for k, v in request.headers
        if k.lower() not in ("host", "content-length", "transfer-encoding", "content-type")
    }

    try:
        # File upload — forward multipart form data as-is
        if request.files:
            files = {
                key: (f.filename, f.stream, f.content_type)
                for key, f in request.files.items()
            }
            resp = req.request(
                method  = request.method,
                url     = url,
                headers = headers,
                files   = files,
                data    = request.form,
                params  = request.args,
                timeout = 30,
            )
        # JSON body
        elif request.get_json(silent=True) is not None:
            headers["Content-Type"] = "application/json"
            resp = req.request(
                method  = request.method,
                url     = url,
                headers = headers,
                json    = request.get_json(silent=True),
                params  = request.args,
                timeout = 30,
            )
        # No body (GET/DELETE)
        else:
            resp = req.request(
                method  = request.method,
                url     = url,
                headers = headers,
                params  = request.args,
                timeout = 30,
            )

        return Response(
            resp.content,
            status       = resp.status_code,
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
        )
    except req.exceptions.ConnectionError:
        return Response(
            '{"error": "API server not reachable"}',
            status       = 503,
            content_type = "application/json"
        )

# ── Serve dashboard files ────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(DASH_DIR, "dashboard.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(DASH_DIR, filename)


if __name__ == "__main__":
    print("\n  MyCloud Dashboard")
    print("  Open : http://localhost:8080")
    print("  API  : proxied from http://localhost:5000\n")
    app.run(host="0.0.0.0", port=8080, debug=False)
