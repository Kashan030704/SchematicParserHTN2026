from orchestrator.loop import Orchestrator
from orchestrator.simulation import DEMO_BOM, FixtureModel


class IngestionFixture:
    def extract_bom(self, images, schema):
        return DEMO_BOM


def test_orchestrator_can_use_live_ingestion_model_with_simulated_planner():
    class Host:
        def publish_context(self, value):
            pass

    orchestrator = Orchestrator(Host(), FixtureModel(), {"parts": []}, ingestion_model=IngestionFixture())
    assert orchestrator.ingestion_model is not orchestrator.model
