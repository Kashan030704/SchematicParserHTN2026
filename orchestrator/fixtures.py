"""PDF/BOM fixtures shared by backends; no camera or actuator imports."""
import copy

DEMO_BOM = {"components": [
    {"type": "resistor", "value": "10k", "quantity": 2, "refdes": ["R1", "R2"]},
    {"type": "capacitor", "value": "0.1uF", "quantity": 2, "refdes": ["C1", "C2"]},
    {"type": "ic", "value": "NE555", "quantity": 1, "refdes": ["U1"]},
]}


class FixtureIngestionModel:
    def __init__(self, bom=None):
        self.bom = copy.deepcopy(DEMO_BOM if bom is None else bom)

    def extract_bom(self, images, schema):
        if not images or not all(image.startswith("data:image/png;base64,") for image in images):
            raise ValueError("Simulation still requires PDF rasterization")
        return copy.deepcopy(self.bom)


def create_demo_pdf(directory):
    import pymupdf

    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / "demo.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 40), "SIMULATION FIXTURE - BOM response is mocked, not inferred", fontsize=12)
        page.insert_text((40, 70), "R1,R2: resistor 10k; C1,C2: capacitor 0.1uF; U1: IC NE555", fontsize=11)
        document.save(pdf)
    return pdf
