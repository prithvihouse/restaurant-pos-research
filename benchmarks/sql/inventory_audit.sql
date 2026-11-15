COPY (
    WITH deductions AS (
        SELECT r.ingredient_id,
               SUM(o.quantity::bigint * r.units_per_item) AS deducted,
               COUNT(*) AS revisions
        FROM orders o
        JOIN recipe_ingredients r ON r.menu_item_id = o.menu_item_id
        GROUP BY r.ingredient_id
    )
    SELECT i.id AS ingredient_id, i.on_hand, i.revision,
           COALESCE(d.deducted, 0) AS units_deducted,
           COALESCE(d.revisions, 0) AS expected_revision
    FROM ingredients i
    LEFT JOIN deductions d ON d.ingredient_id = i.id
    ORDER BY i.id
) TO STDOUT WITH (FORMAT csv, HEADER true);
