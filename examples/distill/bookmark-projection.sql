-- Example only: adapt these columns to the bookmark application's real schema.
-- updated_at must advance when tags, collections or source captures change.
-- captured_text must be a provenance-bearing source observation, not preview text.
SELECT json_object(
    'external_id', CAST(id AS TEXT),
    'revision', CAST(updated_at AS TEXT),
    'url', url,
    'title', title,
    'authors', json(COALESCE(authors_json, '[]')),
    'collections', json(COALESCE(collections_json, '[]')),
    'tags', json(COALESCE(tags_json, '[]')),
    'source_type', COALESCE(source_type, 'text'),
    'removed', CASE WHEN deleted_at IS NULL THEN json('false') ELSE json('true') END,
    'capture', CASE WHEN captured_text IS NULL THEN NULL ELSE json_object(
        'text', captured_text,
        'origin', capture_origin,
        'modality', COALESCE(source_type, 'text'),
        'locator', COALESCE(capture_locator, 'bounded saved source text')
    ) END
) AS item_json
FROM bookmarks
ORDER BY id;
