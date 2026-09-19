import copy
import json
from types import SimpleNamespace

import pytest

from ingestion.parse import BOM_SCHEMA, parse_pdf
from orchestrator.baseten_client import BasetenClient
from orchestrator.simulation import DEMO_BOM, create_fixtures


class CompletionStub:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def response(content=None, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, refusal=None))])


def test_vision_pdf_request_uses_exact_schema_and_raster_images(tmp_path):
    pdf, _ = create_fixtures(tmp_path)
    stub = CompletionStub([response(json.dumps(DEMO_BOM))])
    client = BasetenClient(stub, vision_model="fixture-vision", tool_model="fixture-tool")
    assert parse_pdf(pdf, client) == DEMO_BOM
    request = stub.requests[0]
    assert request["response_format"]["json_schema"]["schema"] == BOM_SCHEMA
    assert request["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_429_retries_bounded_and_truncated_output_rejected():
    error = RuntimeError("rate limited")
    error.status_code = 429
    stub = CompletionStub([error] * 5)
    sleeps = []
    client = BasetenClient(stub, "vision", "tools", sleep=sleeps.append)
    with pytest.raises(RuntimeError, match="rate limited"):
        client.extract_bom(["fixture"], BOM_SCHEMA)
    assert len(stub.requests) == 5 and len(sleeps) == 4
    assert sleeps == sorted(sleeps)
    stub = CompletionStub([response('{"components":', finish="length")])
    with pytest.raises(ValueError, match="incomplete"):
        BasetenClient(stub, "vision", "tools").extract_bom(["fixture"], BOM_SCHEMA)


def test_invalid_schema_output_is_not_accepted(tmp_path):
    pdf, _ = create_fixtures(tmp_path)
    stub = CompletionStub([response('{"components":[{"type":"resistor"}]}')])
    with pytest.raises(Exception):
        parse_pdf(pdf, BasetenClient(stub, "vision", "tools"))


def test_event_model_slugs_are_required(monkeypatch):
    monkeypatch.delenv("BASETEN_VISION_MODEL", raising=False)
    with pytest.raises(ValueError, match="BASETEN_VISION_MODEL"):
        BasetenClient(CompletionStub([]), tool_model="tools")


def test_ingestion_client_does_not_require_tool_model():
    client = BasetenClient(CompletionStub([]), vision_model="fixture-vision")
    with pytest.raises(ValueError, match="BASETEN_TOOL_MODEL"):
        client.plan([], [])
