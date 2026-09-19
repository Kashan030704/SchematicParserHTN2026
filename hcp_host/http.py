"""Shared HTTP node conventions. Legacy NDJSON transport is not the live runtime."""
import hmac
from flask import jsonify, request


def configure_node_app(app, *, token=None, hardware=False):
    if hardware and (not token or len(token) < 24):
        raise ValueError("Hardware HTTP nodes require HCP_NODE_TOKEN (24+ characters)")
    app.config["MAX_CONTENT_LENGTH"] = 16384

    @app.before_request
    def authorize():
        if token and not hmac.compare_digest(request.headers.get("Authorization", ""),
                                              "Bearer " + token):
            return jsonify(error="Unauthorized"), 401

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response


def object_body(required):
    from werkzeug.exceptions import BadRequest
    data = request.get_json()
    if not isinstance(data, dict) or set(data) != set(required):
        raise BadRequest("Expected JSON keys: " + ", ".join(sorted(required)))
    return data
