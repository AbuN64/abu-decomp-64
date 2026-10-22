import json

from abu_decomp import recipe_needs


def test_the_route_is_the_type_items_command_only_for_a_function_that_waits(tmp_path):
    (tmp_path / ".unbake/recipe").mkdir(parents=True)
    (tmp_path / ".unbake/recipe/needs.json").write_text(
        json.dumps({"f": {"item": "f.arg0", "next": "recipe work f.arg0 --kind type"}})
    )
    assert recipe_needs.route(tmp_path, "f", "recipe work f") == "recipe work f.arg0 --kind type"
    assert recipe_needs.route(tmp_path, "g", "recipe work g") == "recipe work g"
    assert recipe_needs.route(None, "f", "recipe work f") == "recipe work f"


def test_a_missing_or_damaged_file_routes_nowhere(tmp_path):
    assert recipe_needs.read(tmp_path) == {}
    (tmp_path / ".unbake/recipe").mkdir(parents=True)
    (tmp_path / ".unbake/recipe/needs.json").write_text("[1")
    assert recipe_needs.read(tmp_path) == {}
