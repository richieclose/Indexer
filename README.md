# Indexer

This project stores image metadata in a SQLite database. The `DatabaseManager`
class starts with a very small schema and expands it as more images are
processed.

## Minimal Starting Schema

The database begins with only three columns:

- `path` – the file path of the image (primary key)
- `File_Location_Folder` – top‑level folder name
- `File_Location_Session` – session or sub‑folder name

New metadata columns are added as images are inserted. This keeps the initial
schema simple while allowing it to grow with the data.

## Dynamic Columns

`DatabaseManager.bulk_insert_images` accepts a list of dictionaries. Before the
records are inserted, it gathers all keys from the batch and calls
`add_columns_if_needed` to create any missing columns. Columns for GPS or tag
fields automatically receive indexes when added. As a result, every piece of
metadata extracted from images becomes a new column the first time it appears.

## Automatic Indexes

Indexes are created automatically for frequently used fields:

- `File_Location_Folder` and `File_Location_Session`
- `Capture_Time` (when that column exists)
- GPS latitude, longitude and altitude columns
- Any column beginning with `Tag_`

These indexes are created when the columns exist so queries remain fast even as
the schema grows.
