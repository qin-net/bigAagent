from insightagent.schema_model import model_from_json_schema
from insightagent.tracking_agent import DEFAULT_TRACK_OUTPUT_SCHEMA, parse_output_schema


def test_model_from_default_track_schema():
    model = model_from_json_schema(DEFAULT_TRACK_OUTPUT_SCHEMA)
    parsed = model.model_validate(
        {
            "answer": "问询增加披露压力，未打到现金流证伪",
            "thesis_impact": "weaken",
            "evidence_refs": ["flag_added:sentiment:has_inquiry"],
            "falsifier_hit": False,
            "abstain": False,
            "missing": [],
        }
    )
    assert parsed.thesis_impact == "weaken"


def test_parse_output_schema_rejects_non_object():
    try:
        parse_output_schema('"not-an-object"')
    except ValueError as error:
        assert "object schema" in str(error)
    else:
        raise AssertionError("expected ValueError")
