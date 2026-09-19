"""Incoming multi-format coverage, ported to the explicit approval gate."""
import base64
import io

import cv2
import numpy as np
import pymupdf
import pytest

from ingestion.parse import parse_schematic
from tests.test_controller import make_controller
from tests.conftest import eventually
from ui.app import create_app


@pytest.mark.parametrize("suffix", [".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".JPEG"])
def test_image_upload_reaches_vision_and_waits_for_approval(suffix):
    class Model:
        images = None
        def extract_bom(self, images, schema, *, part_types):
            self.images = images
            return {"R3": 2, "C1": 1}
    model = Model()
    controller, events = make_controller()
    app = create_app(controller, model_factory=lambda: model, part_types=["R3", "C1"])
    token = {"X-HCP-UI-Token": app.config["UI_TOKEN"]}
    ok, encoded = cv2.imencode(suffix.lower(), np.full((80, 120, 3), 255, dtype=np.uint8))
    assert ok
    client = app.test_client()
    response = client.post("/proposals", data={
        "schematic": (io.BytesIO(encoded.tobytes()), "schematic" + suffix)}, headers=token)
    assert response.status_code == 201, response.json
    proposal = response.json
    assert proposal["state"] == "awaiting_confirmation"
    assert proposal["bom"] == {"R3": 2, "C1": 1}
    assert proposal["source"] == "baseten"
    assert proposal["llm_schematic_suggestions"]
    assert [e[1] for e in events] == ["/detect"]
    assert len(model.images) == 1
    pixmap = pymupdf.Pixmap(base64.b64decode(model.images[0].split(",")[1]))
    assert max(pixmap.width, pixmap.height) <= 2400
    response = client.post(f"/proposals/{proposal['id']}/approve",
        json={"approved": True, "bom": proposal["bom"]}, headers=token)
    assert response.status_code == 202
    done = eventually(lambda: (r if (r := client.get(f"/proposals/{proposal['id']}").json)["state"] != "running" else None))
    assert done["state"] == "complete"
    assert done["cups_commanded"] == ["R3", "C1"]  # One cup per TYPE, never quantity expansion.


def test_unsupported_upload_is_rejected():
    controller, _ = make_controller()
    app = create_app(controller, part_types=["R3"])
    response = app.test_client().post("/proposals",
        data={"schematic": (io.BytesIO(b"text"), "schematic.txt")},
        headers={"X-HCP-UI-Token": app.config["UI_TOKEN"]})
    assert response.status_code == 400
    assert "JPEG" in response.json["error"] and "SCH" in response.json["error"]


def test_corrupt_image_never_calls_model(tmp_path):
    class Model:
        def extract_bom(self, *args, **kwargs):
            pytest.fail("Invalid image must not reach vision ingestion")
    path = tmp_path / "invalid.jpg"
    path.write_bytes(b"not an image")
    with pytest.raises(Exception):
        parse_schematic(path, Model())


def test_extension_content_mismatch_never_calls_model(tmp_path):
    class Model:
        def extract_bom(self, *args, **kwargs):
            pytest.fail("Mislabeled PDF must not reach vision ingestion")
    path = tmp_path / "image.jpg"
    doc = pymupdf.open()
    doc.new_page()
    path.write_bytes(doc.tobytes())
    doc.close()
    with pytest.raises(ValueError, match="contents"):
        parse_schematic(path, Model())
