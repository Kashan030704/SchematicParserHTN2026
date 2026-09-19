import io

import pytest

from ingestion.parse import parse_schematic
from tests.test_palette_backend import finish
from ui.app import create_palette_app


def kicad(*parts):
    blocks = ''.join(f'$Comp\nL Device:R {ref}\nF 0 "{ref}" H 0 0 50 0000 C CNN\nF 1 "{value}" H 0 0 50 0000 C CNN\n$EndComp\n' for ref, value in parts)
    return 'EESchema Schematic File Version 4\n' + blocks + '$EndSCHEMATC\n'


EAGLE = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="9.6.2"><drawing><schematic><parts>
<part name="R1" value="10k"/><part name="R2" value="10k"/>
<part name="U1" deviceset="NE555"/>
</parts></schematic></drawing></eagle>'''


class NoVision:
    def extract_bom(self, *args):
        pytest.fail('SCH component extraction must not call vision')


@pytest.mark.parametrize('content', [kicad(('R1', '10k'), ('R2', '10k'), ('U1', 'NE555')), EAGLE])
def test_sch_upload_extracts_actual_parts_and_runs(tmp_path, content):
    app = create_palette_app(instance_path=tmp_path, ingestion_model=NoVision())
    client = app.test_client()
    result = finish(client, client.post('/api/runs', data={'pdf': (io.BytesIO(content.encode()), 'circuit.SCH')}))
    assert result['state'] == 'complete', result
    assert result['bom'] == {'components': [
        {'type': 'resistor', 'value': '10k', 'quantity': 2, 'refdes': ['R1', 'R2']},
        {'type': 'ic', 'value': 'NE555', 'quantity': 1, 'refdes': ['U1']},
    ]}
    assert len(result['delivered']) == 3


def test_kicad_units_and_power_symbols(tmp_path):
    path = tmp_path / 'units.sch'
    path.write_text(kicad(('U1', 'NE555'), ('U1', 'NE555'), ('#PWR01', 'GND')))
    assert parse_schematic(path, NoVision())['components'] == [
        {'type': 'ic', 'value': 'NE555', 'quantity': 1, 'refdes': ['U1']}]


@pytest.mark.parametrize('content, error', [
    ('unknown format', 'Unsupported .sch'),
    (kicad(('R?', '10k')), 'annotated reference'),
    (kicad(('R1', '')), 'reference and value'),
    (kicad(('U1', 'NE555'), ('U1', 'OTHER')), 'Conflicting'),
    (kicad(('R1', '10k')).replace('$EndComp', ''), 'Incomplete'),
    (kicad(('R1', '10k')).replace('$EndSCHEMATC', '$Sheet\n$EndSheet\n$EndSCHEMATC'), 'Hierarchical'),
    ('<eagle>', 'Malformed'),
    ('<board/>', 'Unsupported'),
    ('<!DOCTYPE eagle [<!ENTITY x "test">]><eagle/>', 'entity'),
    (EAGLE.replace('<parts>', '<modules><module/></modules><parts>'), 'modules'),
    (EAGLE.replace('name="R2"', 'name="R1"'), 'duplicate'),
])
def test_invalid_sch_fails_before_ingestion(tmp_path, content, error):
    path = tmp_path / 'bad.sch'
    path.write_text(content)
    with pytest.raises(ValueError, match=error):
        parse_schematic(path, NoVision())
