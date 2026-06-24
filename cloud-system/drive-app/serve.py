#!/usr/bin/env python3
"""
MyDrive — standalone end-user storage app server.
Serves on port 8081, proxies API calls to port 5000.
"""

from flask import Flask, send_from_directory, request, Response
import requests as req
import os

app      = Flask(__name__)
APP_DIR  = os.path.dirname(os.path.abspath(__file__))
API_URL  = "http://localhost:5000"


@app.route("/api/<path:path>", methods=["GET","POST","PUT","DELETE"])
def proxy(path):
    url     = f"{API_URL}/{path}"
    headers = {
        k: v for k, v in request.headers
        if k.lower() not in ("host", "content-length", "transfer-encoding", "content-type")
    }
    try:
        if request.files:
            files = {
                key: (f.filename, f.stream, f.content_type)
                for key, f in request.files.items()
            }
            resp = req.request(
                method=request.method, url=url, headers=headers,
                files=files, data=request.form, params=request.args, timeout=30,
            )
        elif request.get_json(silent=True) is not None:
            headers["Content-Type"] = "application/json"
            resp = req.request(
                method=request.method, url=url, headers=headers,
                json=request.get_json(silent=True), params=request.args, timeout=30,
            )
        else:
            resp = req.request(
                method=request.method, url=url, headers=headers,
                params=request.args, timeout=30,
            )
        return Response(
            resp.content, status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/octet-stream")
        )
    except req.exceptions.ConnectionError:
        return Response('{"error": "API server not reachable"}',
                         status=503, content_type="application/json")


@app.route("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(APP_DIR, filename)


if __name__ == "__main__":
    print("\n  MyDrive — user storage app")
    print("  Open: http://localhost:8081\n")
    app.run(host="0.0.0.0", port=8081, debug=False)
