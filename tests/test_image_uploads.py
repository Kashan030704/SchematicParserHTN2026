import base64
import copy
import io

import cv2
import numpy as np
import pymupdf
import pytest

from ingestion.parse import parse_schematic
from orchestrator.fixtures import DEMO_BOM
from tests.test_palette_backend import finish
from ui.app import create_palette_app


@pytest.mark.parametrize('suffix', ['.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.JPEG'])
def test_image_upload_reaches_vision_and_completes(tmp_path, suffix):
    class Model:
        images = None

        def extract_bom(self, images, schema):
            self.images = images
            return copy.deepcopy(DEMO_BOM)

    model = Model()
    app = create_palette_app(instance_path=tmp_path, live_ingestion=True, ingestion_model=model)
    ok, encoded = cv2.imencode(suffix.lower(), np.full((80, 120, 3), 255, dtype=np.uint8))
    assert ok
    client = app.test_client()
    result = finish(client, client.post('/api/runs', data={
        'pdf': (io.BytesIO(encoded.tobytes()), 'schematic' + suffix),
    }))
    assert result['state'] == 'complete', result
    assert result['bom'] == DEMO_BOM
    assert len(model.images) == 1
    assert model.images[0].startswith('data:image/png;base64,')
    pixmap = pymupdf.Pixmap(base64.b64decode(model.images[0].split(',')[1]))
    assert max(pixmap.width, pixmap.height) <= 2400


def test_unsupported_upload_is_rejected(tmp_path):
    client = create_palette_app(instance_path=tmp_path).test_client()
    response = client.post('/api/runs', data={'pdf': (io.BytesIO(b'text'), 'schematic.txt')})
    assert response.status_code == 400
    assert 'JPEG' in response.json['error']


def test_corrupt_image_never_calls_model(tmp_path):
    class Model:
        def extract_bom(self, *args):
            pytest.fail('Invalid image must not reach vision ingestion')

    path = tmp_path / 'invalid.jpg'
    path.write_bytes(b'not an image')
    with pytest.raises(Exception):
        parse_schematic(path, Model())
