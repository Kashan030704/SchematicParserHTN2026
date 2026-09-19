"""Preserve incoming SCH parsing without reviving palette execution or model calls."""
import io

import pytest

from ingestion.parse import parse_schematic
from ingestion.sch import parse_sch
from tests.test_controller import make_controller
from tests.conftest import eventually
from ui.app import create_app


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
    def extract_bom(self, *args, **kwargs):
        pytest.fail("SCH component extraction must not call vision")


@pytest.mark.parametrize("content", [kicad(("R1", "10k"), ("R2", "10k"), ("U1", "NE555")), EAGLE])
def test_sch_upload_preserves_records_and_requires_human_mapping(content):
    controller, events = make_controller()
    def never_construct():
        pytest.fail("SCH uploads must not construct Baseten or require an API key")
    app = create_app(controller, model_factory=never_construct, part_types=["R3", "C1"])
    client, token = app.test_client(), {"X-HCP-UI-Token": app.config["UI_TOKEN"]}
    response = client.post("/proposals", data={
        "schematic": (io.BytesIO(content.encode()), "circuit.SCH")}, headers=token)
    assert response.status_code == 201, response.json
    result = response.json
    assert result["state"] == "awaiting_confirmation" and result["source"] == "sch"
    assert result["bom"] == {"resistor:10k": 2, "ic:NE555": 1}
    assert result["component_details"] == [
        {"type": "resistor", "value": "10k", "quantity": 2, "refdes": ["R1", "R2"]},
        {"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U1"]}]
    assert any("decoupling" in note for note in result["llm_schematic_suggestions"])
    assert len(result["warnings"]) == 2
    assert [e[1] for e in events] == ["/detect"]
    url = f"/proposals/{result['id']}/approve"
    assert client.post(url, json={"approved": True, "bom": result["bom"]}, headers=token).status_code == 400
    assert "/begin" not in [e[1] for e in events]
    # Explicitly corrected cup labels, not an automatic mapping guess.
    assert client.post(url, json={"approved": True, "bom": {"R3": 2, "C1": 1}}, headers=token).status_code == 202
    done = eventually(lambda: (r if (r := client.get(f"/proposals/{result['id']}").json)["state"] != "running" else None))
    assert done["state"] == "complete" and done["cups_commanded"] == ["R3", "C1"]
    assert not done["warnings"]


def test_kicad_units_and_power_symbols(tmp_path):
    path = tmp_path / "units.sch"
    path.write_text(kicad(("U1", "NE555"), ("U1", "NE555"), ("#PWR01", "GND")))
    assert parse_sch(path)["components"] == [
        {"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U1"]}]
    assert parse_schematic(path, NoVision()) == {"ic:NE555": 1}
    assert parse_schematic(path, NoVision(), part_types=["U1"]) == {"U1": 1}


def test_exact_canonical_label_preferred_and_no_partial_group_skipping(tmp_path):
    path = tmp_path / "parts.sch"
    path.write_text(kicad(("R1", "10k"), ("R2", "10k")))
    assert parse_schematic(path, part_types=["resistor:10k", "R1", "R2"]) == {"resistor:10k": 2}
    assert parse_schematic(path, part_types=["R1", "R2"]) == {"R1": 1, "R2": 1}
    assert parse_schematic(path, part_types=["R1"]) == {"resistor:10k": 2}


@pytest.mark.parametrize("content, error", [
    ("unknown format", "Unsupported .sch"),
    (kicad(("R?", "10k")), "annotated reference"),
    (kicad(("R1", "")), "reference and value"),
    (kicad(("U1", "NE555"), ("U1", "OTHER")), "Conflicting"),
    (kicad(("R1", "10k")).replace("$EndComp", ""), "Incomplete"),
    (kicad(("R1", "10k")).replace("$EndSCHEMATC", "$Sheet\n$EndSheet\n$EndSCHEMATC"), "Hierarchical"),
    ("<eagle>", "Malformed"),
    ("<board/>", "Unsupported"),
    ('<!DOCTYPE eagle [<!ENTITY x "test">]><eagle/>', "entity"),
    (EAGLE.replace("<parts>", "<modules><module/></modules><parts>"), "modules"),
    (EAGLE.replace('name="R2"', 'name="R1"'), "duplicate"),
])
def test_invalid_sch_fails_before_ingestion(tmp_path, content, error):
    path = tmp_path / "bad.sch"
    path.write_text(content)
    with pytest.raises(ValueError, match=error):
        parse_schematic(path, NoVision())
