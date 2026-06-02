from mcp.server.fastmcp import FastMCP
from nest_mcp import config
from nest_mcp.http_client import make_client


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.mealie.key}"}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def mealie_recipes(search: str = "", limit: int = 20) -> list[dict]:
        """List recipes from Mealie. Optionally filter by search term."""
        params = {"perPage": limit, "orderBy": "name", "orderDirection": "asc"}
        if search:
            params["search"] = search
        async with make_client(config.mealie.url, headers=_headers()) as client:
            resp = await client.get("/api/recipes", params=params)
            resp.raise_for_status()
            items = resp.json().get("items", [])
            return [
                {
                    "name": r.get("name", ""),
                    "slug": r.get("slug", ""),
                    "description": r.get("description", ""),
                    "tags": [t["name"] for t in r.get("tags", [])],
                    "categories": [c["name"] for c in r.get("recipeCategory", [])],
                    "last_made": r.get("lastMade", ""),
                }
                for r in items
            ]

    @mcp.tool()
    async def mealie_recipe(slug: str) -> dict:
        """Get full details of a recipe including ingredients and instructions."""
        async with make_client(config.mealie.url, headers=_headers()) as client:
            resp = await client.get(f"/api/recipes/{slug}")
            resp.raise_for_status()
            r = resp.json()
            return {
                "name": r.get("name", ""),
                "description": r.get("description", ""),
                "tags": [t["name"] for t in r.get("tags", [])],
                "total_time": r.get("totalTime", ""),
                "prep_time": r.get("prepTime", ""),
                "cook_time": r.get("cookTime", ""),
                "servings": r.get("recipeYield", ""),
                "ingredients": [
                    {
                        "quantity": i.get("quantity", ""),
                        "unit": i.get("unit", {}).get("name", "") if i.get("unit") else "",
                        "food": i.get("food", {}).get("name", "") if i.get("food") else "",
                        "note": i.get("note", ""),
                    }
                    for i in r.get("recipeIngredient", [])
                ],
                "instructions": [
                    s.get("text", "") for s in r.get("recipeInstructions", [])
                ],
                "notes": [n.get("text", "") for n in r.get("notes", [])],
            }

    @mcp.tool()
    async def mealie_meal_plan(days: int = 7) -> list[dict]:
        """Get the meal plan for the next N days (default 7)."""
        from datetime import date, timedelta
        today = date.today()
        end = today + timedelta(days=days)
        async with make_client(config.mealie.url, headers=_headers()) as client:
            resp = await client.get(
                "/api/households/mealplans",
                params={"start_date": today.isoformat(), "end_date": end.isoformat()},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            return [
                {
                    "date": p.get("date", ""),
                    "meal_type": p.get("entryType", ""),
                    "recipe": p.get("recipe", {}).get("name", "") if p.get("recipe") else p.get("title", ""),
                    "slug": p.get("recipe", {}).get("slug", "") if p.get("recipe") else "",
                }
                for p in sorted(items, key=lambda x: x.get("date", ""))
            ]

    @mcp.tool()
    async def mealie_shopping_lists() -> list[dict]:
        """Get all Mealie shopping lists and their items."""
        async with make_client(config.mealie.url, headers=_headers()) as client:
            resp = await client.get("/api/households/shopping/lists")
            resp.raise_for_status()
            lists = resp.json().get("items", [])
            result = []
            for lst in lists:
                list_id = lst["id"]
                items_resp = await client.get(f"/api/households/shopping/lists/{list_id}")
                items_resp.raise_for_status()
                detail = items_resp.json()
                result.append({
                    "name": lst.get("name", ""),
                    "items": [
                        {
                            "note": i.get("note", ""),
                            "food": i.get("food", {}).get("name", "") if i.get("food") else "",
                            "quantity": i.get("quantity", ""),
                            "unit": i.get("unit", {}).get("name", "") if i.get("unit") else "",
                            "checked": i.get("checked", False),
                        }
                        for i in detail.get("listItems", [])
                        if not i.get("checked", False)
                    ],
                })
            return result
