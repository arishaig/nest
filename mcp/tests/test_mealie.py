from nest_mcp.tools import mealie
from helpers import load_tools, patch_http


async def test_recipes_list(monkeypatch):
    patch_http(monkeypatch, mealie, {"/api/recipes": {"items": [
        {"name": "Soup", "slug": "soup", "description": "warm",
         "tags": [{"name": "winter"}], "recipeCategory": [{"name": "main"}]},
    ]}})
    out = await load_tools(mealie)["mealie_recipes"](search="soup")
    assert out[0]["name"] == "Soup" and out[0]["tags"] == ["winter"]
    assert out[0]["categories"] == ["main"]


async def test_recipe_detail(monkeypatch):
    patch_http(monkeypatch, mealie, {"/api/recipes/*": {
        "name": "Soup", "recipeIngredient": [
            {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "water"}, "note": ""}],
        "recipeInstructions": [{"text": "boil"}], "notes": [{"text": "hot"}],
        "tags": [{"name": "winter"}],
    }})
    out = await load_tools(mealie)["mealie_recipe"](slug="soup")
    assert out["ingredients"][0]["food"] == "water" and out["ingredients"][0]["unit"] == "cup"
    assert out["instructions"] == ["boil"] and out["notes"] == ["hot"]


async def test_meal_plan_sorted(monkeypatch):
    patch_http(monkeypatch, mealie, {"/api/households/mealplans": {"items": [
        {"date": "2026-06-17", "entryType": "dinner", "recipe": {"name": "B", "slug": "b"}},
        {"date": "2026-06-16", "entryType": "lunch", "title": "Leftovers"},
    ]}})
    out = await load_tools(mealie)["mealie_meal_plan"](days=3)
    assert [p["date"] for p in out] == ["2026-06-16", "2026-06-17"]
    assert out[0]["recipe"] == "Leftovers"  # falls back to title when no recipe
    assert out[1]["recipe"] == "B"


async def test_shopping_lists_unchecked_only(monkeypatch):
    patch_http(monkeypatch, mealie, {
        "/api/households/shopping/lists": {"items": [{"id": "1", "name": "Groceries"}]},
        "/api/households/shopping/lists/*": {"listItems": [
            {"note": "milk", "checked": False, "quantity": 1, "food": {"name": "milk"}},
            {"note": "done", "checked": True},
        ]},
    })
    out = await load_tools(mealie)["mealie_shopping_lists"]()
    assert out[0]["name"] == "Groceries"
    assert len(out[0]["items"]) == 1 and out[0]["items"][0]["note"] == "milk"
