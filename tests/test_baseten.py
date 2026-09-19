import json
from types import SimpleNamespace

import pytest
import pymupdf
from ingestion.parse import BOM_SCHEMA, parse_schematic, validate_bom, strict_json
from orchestrator.baseten_client import BasetenClient


class CompletionStub:
    def __init__(self, response):
        self.response, self.requests = response, []
        self.chat = SimpleNamespace(completions=self)
    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=content, refusal=None))])


def test_real_ingestion_path_single_structured_call(tmp_path):
    path = tmp_path / "schematic.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 40), "R3 10k; C1 0.1uF")
    doc.save(path)
    doc.close()
    bom = {"R3": 2, "C1": 1}
    stub = CompletionStub(response(json.dumps(bom)))
    client = BasetenClient(stub, "event-vision")
    assert parse_schematic(path, client, part_types=bom) == bom
    assert len(stub.requests) == 1
    call = stub.requests[0]
    assert call["response_format"]["json_schema"]["schema"] == BOM_SCHEMA
    assert call["response_format"]["json_schema"]["strict"] is True
    assert "R3" in call["messages"][0]["content"]
    assert call["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("bom", [{}, {"R3": 0}, {"R3": True}, {"R3": 1.0}, {"R3": -1}, {"": 2}, {" ": 1}, {"R3": "2"}, []])
def test_bad_boms_rejected(bom):
    with pytest.raises(Exception):
        validate_bom(bom)


@pytest.mark.parametrize("text", ['{"R3":1,"R3":2}', '{"R3":NaN}', '{"R3":Infinity}'])
def test_duplicate_and_nonfinite_json_rejected(text):
    with pytest.raises(ValueError):
        strict_json(text)


def test_429_not_automatically_retried():
    err = RuntimeError("429 rate limited")
    stub = CompletionStub(err)
    with pytest.raises(RuntimeError):
        BasetenClient(stub, "event-vision").extract_bom([], BOM_SCHEMA)
    assert len(stub.requests) == 1


def test_truncated_model_result_rejected():
    with pytest.raises(ValueError, match="incomplete"):
        BasetenClient(CompletionStub(response('{"R3":', "length")), "event-vision").extract_bom([], BOM_SCHEMA)


def test_model_required(monkeypatch):
    monkeypatch.delenv("BASETEN_VISION_MODEL", raising=False)
    with pytest.raises(ValueError, match="BASETEN_VISION_MODEL"):
        BasetenClient(CompletionStub(None))
