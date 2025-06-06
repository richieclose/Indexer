"""Database operations for the EXIF Extractor application."""

import logging
import sqlite3
from typing import Dict, List, Tuple, Optional, Any
from contextlib import contextmanager

from config import (
    TABLE_SCHEMA,
    SQL_CREATE_TABLE,
    SQL_INSERT_IMAGE,
    SQL_SELECT_IMAGES_IN_RADIUS,
    DEFAULT_TABLE_NAME,
    INDEXES
)

# Configure logging
logger = logging.getLogger(__name__)

class DatabaseManager:
    """Manages database operations for the EXIF Extractor."""
    
    def __init__(self, db_path: str):
        """Initialize database manager.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.table_name = DEFAULT_TABLE_NAME
        self.initialize_database()

    @contextmanager
    def get_connection(self):
        """Context manager for database connections.
        
        Yields:
            sqlite3.Connection: Database connection
        """
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def initialize_database(self):
        """Create database tables with minimal schema - columns added dynamically."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Create table with only essential columns
                # All metadata columns will be added dynamically as needed
                essential_columns = {
                    'path': 'TEXT PRIMARY KEY',
                    'File_Location_Folder': 'TEXT',
                    'File_Location_Session': 'TEXT'
                }
                
                columns = ', '.join(f'{name} {type}' for name, type in essential_columns.items())
                
                # Create table with minimal schema
                create_table_sql = SQL_CREATE_TABLE.format(
                    table_name=self.table_name,
                    columns=columns
                )
                cursor.execute(create_table_sql)
                
                # Create basic indexes (GPS indexes will be created when columns exist)
                basic_indexes = [
                    ('idx_location', ['File_Location_Folder', 'File_Location_Session'])
                ]
                
                for index_name, index_columns in basic_indexes:
                    try:
                        cursor.execute(f"""
                            CREATE INDEX IF NOT EXISTS {index_name}
                            ON {self.table_name} ({', '.join(index_columns)})
                        """)
                    except sqlite3.Error as e:
                        logger.error(f"Error creating index {index_name}: {str(e)}")
                
                # Add indexes for commonly queried text fields
                common_text_fields_to_index = [
                    "File_Location_Folder", 
                    "File_Location_Session", 
                    "Capture_Time"
                ]
                for field in common_text_fields_to_index:
                    if field in TABLE_SCHEMA: # Ensure the field is part of the schema
                        index_name = f"idx_{self.table_name}_{field.lower()}"
                        sql = f"CREATE INDEX IF NOT EXISTS \"{index_name}\" ON {self.table_name}(\"{field}\")"
                        cursor.execute(sql)
                        logger.info(f"Ensured index {index_name} exists on {field}.")

                # Add indexes for GPS related columns defined in the initial schema
                self.create_gps_indexes_if_needed(cursor, TABLE_SCHEMA)
                
                conn.commit()
                logger.info("Database initialized with minimal schema - metadata columns will be added dynamically")
                
        except sqlite3.Error as e:
            logger.error(f"Error initializing database: {str(e)}")
            raise

    def add_columns_if_needed(self, new_columns: Dict[str, str]):
        """Add columns to the database table if they don't already exist.
        
        Args:
            new_columns: Dictionary mapping column names to their SQL types
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Get existing columns
                cursor.execute(f"PRAGMA table_info({self.table_name})")
                existing_columns = {row[1] for row in cursor.fetchall()}
                
                columns_actually_added = False
                
                # Add each new column if it doesn't exist
                for col_name, col_type in new_columns.items():
                    if col_name not in existing_columns:
                        # Sanitize column name for SQL
                        safe_col_name = f'"{col_name}"'
                        actual_col_type = col_type if col_type else 'TEXT'
                        alter_sql = f"ALTER TABLE {self.table_name} ADD COLUMN {safe_col_name} {actual_col_type}"
                        
                        try:
                            cursor.execute(alter_sql)
                            logger.info(f"Added column: {safe_col_name} with type {actual_col_type} to table {self.table_name}")
                            columns_actually_added = True

                            # If the new column is a Tag column, create an index for it
                            if col_name.startswith("Tag_"):
                                sanitized_col_part = col_name.lower().replace('"', '')
                                index_name = f"idx_{self.table_name}_{sanitized_col_part}"  # Sanitize for index name
                                safe_index_name = f'"{index_name}"'
                                index_sql = f"CREATE INDEX IF NOT EXISTS {safe_index_name} ON {self.table_name}({safe_col_name})"
                                try:
                                    cursor.execute(index_sql)
                                    logger.info(f"Created index {safe_index_name} on new tag column {safe_col_name}")
                                except sqlite3.Error as e_idx:
                                    logger.error(f"Error creating index {safe_index_name} for tag column {safe_col_name}: {e_idx}")
                        except sqlite3.Error as e_alter:
                            if "duplicate column name" in str(e_alter):
                                # This is actually an expected case - the column already exists
                                logger.debug(f"Column {safe_col_name} already exists in table {self.table_name}")
                            else:
                                logger.error(f"Error adding column {safe_col_name} with type {actual_col_type}: {e_alter}")
                
                if columns_actually_added:
                    conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Error in add_columns_if_needed: {str(e)}")

    def create_gps_indexes_if_needed(self, cursor: sqlite3.Cursor, columns_to_check: Dict[str, str]):
        """Create indexes on GPS-related columns if they don't exist.
        
        Args:
            cursor: The database cursor to use.
            columns_to_check: A dictionary where keys are column names and values are their types.
        """
        if not columns_to_check:
            return
        
        # This operation doesn't modify schema outside of creating indexes,
        # so direct execution on the passed cursor is fine.
        # Commits should be handled by the calling context if schema changes (like ADD COLUMN) happen there.

        for col_name, col_type in columns_to_check.items():
            safe_col_name = f'"{col_name}"' # Quote column name for safety
            is_lat = "latitude" in col_name.lower() and "gps" in col_name.lower()
            is_lon = "longitude" in col_name.lower() and "gps" in col_name.lower()
            is_alt = "altitude" in col_name.lower() and "gps" in col_name.lower()

            if is_lat or is_lon or is_alt:
                # Ensure the column actually exists before trying to create an index
                # This is a safeguard, as columns_to_check should ideally be existing columns
                # or columns just added in the same transaction by the caller.
                # However, checking here makes this function more robust if used independently.
                cursor.execute(f"PRAGMA table_info({self.table_name})")
                current_table_columns = {row[1] for row in cursor.fetchall()}
                if col_name not in current_table_columns:
                    logger.warning(f"Skipping index creation for {col_name} as it does not exist in {self.table_name}.")
                    continue

                sanitized_col_part = col_name.lower().replace('"', '')
                index_name = f"idx_{self.table_name}_{sanitized_col_part}"
                safe_index_name = f'"{index_name}"'
                sql = f"CREATE INDEX IF NOT EXISTS {safe_index_name} ON {self.table_name}({safe_col_name})"
                try:
                    cursor.execute(sql)
                    logger.info(f"Ensured index {safe_index_name} exists on {safe_col_name}.")
                except sqlite3.Error as e:
                    logger.error(f"Error creating index {safe_index_name} for GPS column {safe_col_name}: {str(e)}")

    def bulk_insert_images(self, image_data: List[Dict[str, str]]):
        """Bulk insert image data into the database."""
        if not image_data:
            return

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # 1. Collect all unique column names from all dictionaries in the batch
                all_keys = set()
                for data_dict in image_data:
                    all_keys.update(data_dict.keys())
                
                if not all_keys:
                    logger.warning("No data keys found in the batch for bulk insert.")
                    return
                
                # Ensure 'path' is always a column, even if somehow missing from all_keys
                # (though it should always be present from BatchProcessWorker)
                if 'path' not in all_keys:
                    all_keys.add('path')
                    logger.warning("'path' key was missing from batch data, added it manually.")

                ordered_columns = sorted(list(all_keys)) # Consistent order for SQL

                # 2. Ensure all these columns exist in the database
                #    Specify REAL type for altitude columns, TEXT for others.
                columns_to_ensure = {}
                for col_name in ordered_columns:
                    if col_name.endswith("_GPS_Altitude"):
                        columns_to_ensure[col_name] = "REAL"
                    elif col_name.endswith("_GPS_Latitude") or col_name.endswith("_GPS_Longitude"):
                        columns_to_ensure[col_name] = "REAL" # Also ensure Lat/Lon are REAL
                    else:
                        columns_to_ensure[col_name] = "TEXT" # Default to TEXT
                
                self.add_columns_if_needed(columns_to_ensure)
                # Create indexes for new GPS columns if any were dynamically added
                self.create_gps_indexes_if_needed(cursor, columns_to_ensure)

                # 3. Build the INSERT statement
                placeholders = ", ".join(["?"] * len(ordered_columns))
                column_names_sql = ", ".join([f'"{col}"' for col in ordered_columns]) # Quote column names

                # Prepare batch of values
                batch_values = []
                for data_dict in image_data:
                    # 4. For each dict, use get(key, None) for all columns
                    values = tuple(data_dict.get(col) for col in ordered_columns)
                    batch_values.append(values)
                
                if batch_values:
                    insert_sql = SQL_INSERT_IMAGE.format(
                        table_name=self.table_name,
                        columns=column_names_sql,
                        placeholders=placeholders
                    )
                    
                    try:
                        cursor.executemany(insert_sql, batch_values)
                        conn.commit()
                        logger.info(f"Bulk inserted {len(batch_values)} records with {len(ordered_columns)} columns each.")
                    except sqlite3.Error as e:
                        logger.error(f"Error during bulk insert executemany: {str(e)}")
                        # Optionally, try individual inserts or log problematic data
                        conn.rollback() # Rollback the batch if any part fails
                        raise # Re-raise to indicate batch failure
                
        except sqlite3.Error as e:
            logger.error(f"Error in bulk_insert_images connection or setup: {str(e)}")
            raise

    def get_images_in_radius(
        self,
        lat: float,
        lon: float,
        lat_delta: float,
        lon_delta: float,
        folder: Optional[str] = None,
        session: Optional[str] = None,
        tag_filters: Optional[Dict[str, str]] = None
    ) -> List[Tuple[str, float, float, Optional[float], str, str]]:
        """Get images within a bounding box (rough radius filter).
        
        Args:
            lat: Center latitude
            lon: Center longitude
            lat_delta: Latitude range (+/-)
            lon_delta: Longitude range (+/-)
            folder: Optional folder filter
            session: Optional session filter
            tag_filters: Optional dictionary of tag column names to values
            
        Returns:
            List of tuples (path, lat, lon, altitude, folder, session)
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Detect GPS column names dynamically using XML->EXIF->JSON precedence
                lat_col, lon_col = self.get_gps_column_names()
                if not lat_col or not lon_col:
                    logger.warning("No GPS coordinate columns found in database")
                    return []
                
                # Find all GPS altitude columns
                cursor.execute(f"PRAGMA table_info({self.table_name})")
                columns = [row[1] for row in cursor.fetchall()]
                alt_columns = [col for col in columns if 'altitude' in col.lower() and 'gps' in col.lower()]
                
                # Build COALESCE expression to get altitude from any source
                # Prioritize XML, then EXIF, then JSON. Each column is wrapped
                # with NULLIF(TRIM(col), '') so that empty strings do not
                # prevent fallback to the next source.
                if alt_columns:
                    sorted_alt_columns = sorted(
                        alt_columns,
                        key=lambda x: (
                            0 if x.startswith('XML_')
                            else 1 if x.startswith('EXIF_')
                            else 2
                        ),
                    )
                    trimmed_cols = [f"NULLIF(TRIM({col}), '')" for col in sorted_alt_columns]
                    coalesce_expr = f"COALESCE({', '.join(trimmed_cols)})"
                    
                    query = f"""
                        SELECT path, {lat_col}, {lon_col}, {coalesce_expr} as altitude, 
                               File_Location_Folder, File_Location_Session
                        FROM {self.table_name}
                        WHERE {lat_col} IS NOT NULL
                        AND {lon_col} IS NOT NULL
                        AND TRIM({lat_col}) != ''
                        AND TRIM({lon_col}) != ''
                        AND CAST({lat_col} AS REAL) BETWEEN :lat - :lat_delta AND :lat + :lat_delta
                        AND CAST({lon_col} AS REAL) BETWEEN :lon - :lon_delta AND :lon + :lon_delta
                    """
                else:
                    query = f"""
                        SELECT path, {lat_col}, {lon_col}, NULL as altitude, 
                               File_Location_Folder, File_Location_Session
                        FROM {self.table_name}
                        WHERE {lat_col} IS NOT NULL
                        AND {lon_col} IS NOT NULL
                        AND TRIM({lat_col}) != ''
                        AND TRIM({lon_col}) != ''
                        AND CAST({lat_col} AS REAL) BETWEEN :lat - :lat_delta AND :lat + :lat_delta
                        AND CAST({lon_col} AS REAL) BETWEEN :lon - :lon_delta AND :lon + :lon_delta
                    """
                
                params = {
                    'lat': lat,
                    'lon': lon,
                    'lat_delta': lat_delta,
                    'lon_delta': lon_delta
                }
                
                # Add folder/session filters if specified
                if folder:
                    query += " AND File_Location_Folder = :folder"
                    params['folder'] = folder
                if session:
                    query += " AND File_Location_Session = :session"
                    params['session'] = session
                
                # Add tag filters if specified
                if tag_filters:
                    for tag_column, tag_value in tag_filters.items():
                        param_name = f"tag_{tag_column.lower()}"
                        query += f" AND {tag_column} = :{param_name}"
                        params[param_name] = tag_value
                
                cursor.execute(query, params)
                return cursor.fetchall()
                
        except sqlite3.Error as e:
            logger.error(f"Error in radius search: {str(e)}")
            return []

    def get_unique_folders(self) -> List[str]:
        """Get list of unique folder names in database."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT DISTINCT File_Location_Folder
                    FROM {self.table_name}
                    WHERE File_Location_Folder IS NOT NULL
                    ORDER BY File_Location_Folder
                """)
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting folders: {str(e)}")
            return []

    def get_unique_sessions(self) -> List[str]:
        """Get list of unique session names in database."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT DISTINCT File_Location_Session
                    FROM {self.table_name}
                    WHERE File_Location_Session IS NOT NULL
                    ORDER BY File_Location_Session
                """)
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting sessions: {str(e)}")
            return []

    def get_image_data(self, path: str) -> Optional[Dict[str, Any]]:
        """Get all data for a specific image.
        
        Args:
            path: Image file path
            
        Returns:
            Dictionary of image data or None if not found
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT * FROM {self.table_name}
                    WHERE path = ?
                """, (path,))
                
                row = cursor.fetchone()
                if row:
                    # Get column names from cursor description
                    columns = [desc[0] for desc in cursor.description]
                    return dict(zip(columns, row))
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Error getting image data: {str(e)}")
            return None

    def get_gps_column_names(self) -> Tuple[Optional[str], Optional[str]]:
        """Detect the GPS latitude and longitude column names in the database.
        
        Returns:
            Tuple of (latitude_column, longitude_column) or (None, None) if not found
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"PRAGMA table_info({self.table_name})")
                columns = [row[1] for row in cursor.fetchall()]
                
                prefixes = ['XML_', 'EXIF_', 'JSON_']
                for prefix in prefixes:
                    lat_candidates = [c for c in columns if c.lower().startswith(prefix.lower()) and 'latitude' in c.lower()]
                    lon_candidates = [c for c in columns if c.lower().startswith(prefix.lower()) and 'longitude' in c.lower()]
                    if lat_candidates and lon_candidates:
                        lat_col = lat_candidates[0]
                        lon_col = lon_candidates[0]
                        # Check if any data exists in these columns
                        cursor.execute(
                            f"SELECT 1 FROM {self.table_name} WHERE {lat_col} IS NOT NULL AND TRIM({lat_col}) != '' "
                            f"AND {lon_col} IS NOT NULL AND TRIM({lon_col}) != '' LIMIT 1"
                        )
                        if cursor.fetchone():
                            return lat_col, lon_col

                # If no columns have data, fall back to first available pair
                for prefix in prefixes:
                    lat_candidates = [c for c in columns if c.lower().startswith(prefix.lower()) and 'latitude' in c.lower()]
                    lon_candidates = [c for c in columns if c.lower().startswith(prefix.lower()) and 'longitude' in c.lower()]
                    if lat_candidates and lon_candidates:
                        return lat_candidates[0], lon_candidates[0]

                return None, None

        except sqlite3.Error as e:
            logger.error(f"Error detecting GPS columns: {str(e)}")
            return None, None

    def get_images_matching_filters(
        self,
        folder: Optional[str] = None,
        session: Optional[str] = None,
        tag_filters: Optional[Dict[str, str]] = None,
    ) -> List[Tuple[str, str, str]]:
        """Fetch images that match folder/session/tag filters regardless of GPS data.

        Args:
            folder: Optional folder filter.
            session: Optional session filter.
            tag_filters: Optional dictionary mapping tag columns to values.

        Returns:
            List of tuples ``(path, folder, session)`` for matching images.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                query = f"SELECT path, File_Location_Folder, File_Location_Session FROM {self.table_name} WHERE 1=1"
                params: Dict[str, Any] = {}

                if folder:
                    query += " AND File_Location_Folder = :folder"
                    params["folder"] = folder
                if session:
                    query += " AND File_Location_Session = :session"
                    params["session"] = session

                if tag_filters:
                    for tag_column, tag_value in tag_filters.items():
                        param_name = f"tag_{tag_column.lower()}"
                        query += f" AND {tag_column} = :{param_name}"
                        params[param_name] = tag_value

                cursor.execute(query, params)
                return cursor.fetchall()

        except sqlite3.Error as e:
            logger.error(f"Error getting images by filters: {str(e)}")
            return []
