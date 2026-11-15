"""Format recorded POS catalog and recipe trial data for a trial database."""

import hashlib


def prepare_trial_data(settings):
    seed = settings["dataset_seed"]
    stock = settings["stock_per_ingredient"]
    item_ids = list(range(1, 101))
    ranked = sorted(item_ids, key=lambda item: hashlib.sha256(f"{seed}:{item}".encode()).digest())
    hot = sorted(ranked[:20])
    hot_set = set(hot)

    ingredients = [(i, f"Shared ingredient {i:03d}", stock) for i in range(1, 51)]
    items = [(i, f"Menu item {i:03d}", 700 + (i % 20) * 25) for i in item_ids]
    recipes = []
    for item in item_ids:
        group = (item - 1) // 4
        unique_ingredient = 50 + item
        ingredients.append((unique_ingredient, f"Item ingredient {item:03d}", stock))
        recipes.extend([(item, 2 * group + 1, 1), (item, 2 * group + 2, 2),
                        (item, unique_ingredient, 1)])

    def insert(table, columns, rows):
        # Format recorded catalog and recipe rows as SQL literals.
        def literal(value):
            return f"'{value}'" if isinstance(value, str) else str(value)

        values = ",\n".join("(" + ", ".join(map(literal, row)) + ")" for row in rows)
        return f"INSERT INTO {table} ({columns}) VALUES\n{values};"

    sql = "\n".join([
        "BEGIN;",
        insert("ingredients", "id, name, on_hand", ingredients),
        insert("menu_items", "id, name, price_cents", items),
        insert("recipe_ingredients", "menu_item_id, ingredient_id, units_per_item", recipes),
        "COMMIT;",
        "",
    ])
    manifest = {
        "actual_trial": True,
        "dataset_seed": seed,
        "stock_per_ingredient": stock,
        "counts": {
            "menu_items": len(items), "ingredients": len(ingredients),
            "recipe_ingredients": len(recipes), "orders": 0,
        },
        "hot_item_ids": hot,
        "other_item_ids": sorted(set(item_ids) - hot_set),
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
    }
    return sql, manifest
