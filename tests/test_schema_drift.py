"""Tests for schema-drift detection."""
import json

import pytest

from integration_rot.schema_drift import (
    SchemaDiff,
    check_drift,
    diff_specs,
    load_snapshot,
    load_spec,
    normalize_spec,
)

OLD = [
    {"path": "/v1/charges", "method": "POST", "summary": "c",
     "parameters": [{"name": "limit", "in": "query", "type": "integer"}],
     "request_fields": {"amount": "integer", "currency": "string", "capture": "boolean"},
     "response_fields": {"id": "string", "amount": "integer"}},
    {"path": "/v1/old", "method": "GET", "summary": "gone",
     "parameters": [], "request_fields": {}, "response_fields": {}},
]

NEW = [
    {"path": "/v1/charges", "method": "POST", "summary": "c",
     "parameters": [{"name": "limit", "in": "query", "type": "string"},
                    {"name": "starting_after", "in": "query", "type": "string"}],
     "request_fields": {"amount": "integer", "currency": "string",
                        "payment_method": "string"},
     "response_fields": {"id": "string", "amount": "string"}},
    {"path": "/v1/new", "method": "POST", "summary": "fresh",
     "parameters": [], "request_fields": {}, "response_fields": {}},
]


def test_diff_detects_added_removed_changed():
    diff = diff_specs(OLD, NEW, vendor="Test")
    assert ("POST", "/v1/new") in diff.added
    assert ("GET", "/v1/old") in diff.removed
    assert len(diff.changed) == 1
    change = diff.changed[0]
    assert (change.method, change.path) == ("POST", "/v1/charges")
    details = "\n".join(change.details)
    assert "parameter `limit` (query) type changed: integer -> string" in details
    assert "parameter `starting_after` (query) added" in details
    assert "request field `capture` removed" in details
    assert "request field `payment_method` added" in details
    assert "response field `amount` type changed: integer -> string" in details
    assert not diff.empty()


def test_diff_identical_is_empty():
    diff = diff_specs(OLD, OLD, vendor="Test")
    assert diff.empty()
    assert diff.added == [] and diff.removed == [] and diff.changed == []


def test_diff_summary():
    diff = diff_specs(OLD, NEW, vendor="Test")
    s = diff.summary()
    assert "1 endpoint(s) added" in s and "1 removed" in s and "1 changed" in s


def test_normalize_spec_extracts_endpoints():
    spec = {
        "paths": {
            "/v1/things": {
                "post": {
                    "summary": "Create a thing",
                    "parameters": [
                        {"name": "verbose", "in": "query",
                         "schema": {"type": "boolean"}}
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"id": {"type": "string"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }
    eps = normalize_spec(spec)
    assert len(eps) == 1
    ep = eps[0]
    assert (ep["method"], ep["path"]) == ("POST", "/v1/things")
    assert ep["parameters"] == [{"name": "verbose", "in": "query", "type": "boolean"}]
    assert ep["request_fields"] == {"name": "string"}
    assert ep["response_fields"] == {"id": "string"}


def test_normalize_spec_resolves_refs():
    spec = {
        "paths": {
            "/v1/a": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/A"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "A": {"type": "object", "properties": {"x": {"type": "integer"}}}
            }
        },
    }
    eps = normalize_spec(spec)
    assert eps[0]["response_fields"] == {"x": "integer"}


def test_normalize_spec_only_filter():
    spec = {"paths": {
        "/v1/a": {"get": {"responses": {}}},
        "/v1/b": {"get": {"responses": {}}},
    }}
    eps = normalize_spec(spec, only={("GET", "/v1/a")})
    assert [(e["method"], e["path"]) for e in eps] == [("GET", "/v1/a")]


def test_pinned_stripe_snapshot_loads_and_self_diffs_empty(tmp_path):
    import copy

    snap = load_snapshot("stripe")
    assert snap["_meta"]["vendor"] == "Stripe"
    assert len(snap["endpoints"]) >= 4
    # diff the snapshot against itself (normalized shape) -> must be empty
    f = tmp_path / "self.json"
    f.write_text(json.dumps({"endpoints": copy.deepcopy(snap["endpoints"])}))
    diff = check_drift("stripe", f)
    assert diff.empty(), diff.summary()


def test_check_drift_detects_real_change(tmp_path):
    snap = load_snapshot("stripe")
    import copy
    doc = copy.deepcopy(snap)
    # simulate drift: drop a request field, change a type, add an endpoint
    charges = next(e for e in doc["endpoints"]
                   if e["path"] == "/v1/charges" and e["method"] == "POST")
    dropped = charges["request_fields"].pop("capture")
    assert dropped  # sanity: the field existed in the real snapshot
    first_field = next(iter(charges["response_fields"]))
    charges["response_fields"][first_field] = "CHANGED_TYPE"
    doc["endpoints"].append({"path": "/v1/brand_new", "method": "POST",
                             "summary": "", "parameters": [],
                             "request_fields": {}, "response_fields": {}})
    f = tmp_path / "fresh.json"
    f.write_text(json.dumps(doc))
    diff = check_drift("stripe", f)
    assert not diff.empty()
    details = "\n".join(d for c in diff.changed for d in c.details)
    assert "request field `capture` removed" in details
    assert f"response field `{first_field}` type changed" in details
    assert ("POST", "/v1/brand_new") in diff.added


def test_load_spec_from_file(tmp_path):
    f = tmp_path / "spec.json"
    f.write_text(json.dumps({"openapi": "3.0.0"}))
    assert load_spec(f)["openapi"] == "3.0.0"


def test_load_snapshot_missing_vendor():
    with pytest.raises(FileNotFoundError):
        load_snapshot("no-such-vendor-xyz")
