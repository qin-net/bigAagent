from insightagent.business_contracts import Report
from insightagent.contracts import ResourceSpec, ResourceType
from insightagent.fundamental_agent import EmptyArgs, SearchArgs
from insightagent.runtime import SubmitFinalArgs, _submit_final_tool
from insightagent.schema import assert_strict_compatible, to_strict_json_schema


def test_strict_schema_requires_all_properties_and_closes_objects():
    schema = to_strict_json_schema(SearchArgs.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "minLength" not in schema["properties"]["query"]
    assert_strict_compatible(schema)


def test_empty_args_are_strict_compatible():
    schema = to_strict_json_schema(EmptyArgs.model_json_schema())
    assert schema["additionalProperties"] is False
    assert "dummy" in schema["properties"]
    assert set(schema["required"]) == set(schema["properties"])
    assert_strict_compatible(schema)


def test_defs_are_rewritten_and_datetime_format_stripped():
    schema = to_strict_json_schema(Report.model_json_schema())
    assert "$defs" not in schema
    dumped = str(schema)
    assert "#/$defs/" not in dumped
    assert "date-time" not in dumped
    assert_strict_compatible(schema)


def test_submit_final_anyof_branches_have_type():
    schema = to_strict_json_schema(SubmitFinalArgs.model_json_schema())
    assert_strict_compatible(schema)
    report = schema["$def"]["SubmitFinalOutput"]["properties"]["report"]
    assert "type" not in report or report.get("anyOf")
    types = {item.get("type") for item in report["anyOf"]}
    assert "object" in types
    assert "null" in types
    assert all("type" in item for item in report["anyOf"])
    assert "$ref" not in report["anyOf"][0]


def test_submit_final_tool_is_strict_and_has_no_runtime_counters():
    tool = _submit_final_tool(strict=True)
    function = tool["function"]
    assert function["strict"] is True
    assert function["name"] == "submit_final"
    parameters = function["parameters"]
    assert_strict_compatible(parameters)
    dumped = str(parameters)
    assert "base_version" not in dumped
    assert "loop_round" not in dumped
    spec = ResourceSpec(
        name="search",
        type=ResourceType.FUNCTION,
        description="search",
        input_schema=SearchArgs.model_json_schema(),
        output_schema={},
    )
    tool = spec.to_deepseek_tool(strict=True)
    assert tool["function"]["strict"] is True
    assert_strict_compatible(tool["function"]["parameters"])


def test_submit_final_args_accept_report_without_counters():
    payload = {
        "status": "abstained",
        "output": {
            "report": {
                "role": "fundamental",
                "score": 3,
                "stance": "abstain",
                "summary": "missing data",
                "citations": [],
                "risks": ["incomplete"],
                "abstain": True,
            }
        },
        "reflection": {},
        "state_patch": {"set": [], "append": [], "remove": []},
    }
    parsed = SubmitFinalArgs.model_validate(payload)
    assert parsed.output.report is not None
    assert parsed.output.report.abstain is True


def test_strict_tool_schemas_have_no_empty_objects():
    from insightagent.data_contracts import EmptyInput
    from insightagent.fundamental_agent import ArtifactArgs

    for model in (EmptyArgs, SearchArgs, ArtifactArgs, EmptyInput, SubmitFinalArgs):
        schema = to_strict_json_schema(model.model_json_schema())
        assert_strict_compatible(schema)

        def walk(node):
            if isinstance(node, dict):
                if (
                    node.get("type") == "object"
                    and node.get("properties") == {}
                    and "anyOf" not in node
                ):
                    raise AssertionError("empty object schema")
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(schema)
