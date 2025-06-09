"""Background workers for the EXIF Extractor application."""

import logging
import os
import json
import math
from typing import List, Dict, Any, Optional, Tuple
from fractions import Fraction

from PyQt6.QtCore import QThread, pyqtSignal
from geopy.distance import geodesic
import sqlite3

from exif_utils import extract_exif_data
from database import DatabaseManager

# Configure logging
logger = logging.getLogger(__name__)

class ExifExtractorWorker(QThread):
    """Worker thread for extracting EXIF data from a single image."""
    
    progress = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)
    exif_extracted = pyqtSignal(dict, bool)

    def __init__(self, image_path: str):
        """Initialize the worker.
        
        Args:
            image_path: Path to the image file
        """
        super().__init__()
        self.image_path = image_path

    def run(self):
        """Extract EXIF data from the image."""
        try:
            # Extract metadata (either EXIF or XML comment)
            metadata, is_xml = extract_exif_data(self.image_path)
            
            if not metadata:
                self.error.emit(f"No metadata found in {self.image_path}")
                return
            
            # Add folder location data
            folder = os.path.basename(os.path.dirname(self.image_path))
            session_path = os.path.dirname(os.path.dirname(self.image_path))
            session = os.path.basename(session_path) if session_path else ''
            
            metadata['File_Location_Folder'] = folder
            metadata['File_Location_Session'] = session
            
            self.exif_extracted.emit(metadata, is_xml)
            self.finished.emit()
            
        except Exception as e:
            logger.error(f"Error in ExifExtractorWorker: {str(e)}")
            self.error.emit(f"Error reading metadata from {self.image_path}: {str(e)}")

class BatchProcessWorker(QThread):
    """Worker thread for processing multiple images and storing in database."""
    
    progress = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)
    file_processed = pyqtSignal(str)

    def __init__(self, image_files: List[str], db_path: str, 
                 field_mapping_config: Dict,
                 selected_fields: List[str],
                 append_mode: bool = False,
                 image_tags: Dict[str, Dict[str, str]] = None,
                 unique_tag_names: Optional[List[str]] = None):
        """Initialize the worker.
        
        Args:
            image_files: List of image file paths
            db_path: Path to the database file
            field_mapping_config: Dictionary holding rules for mapping raw keys to DB keys
            selected_fields: List of DB schema fields selected in the GUI for extraction
            append_mode: Whether to append data to existing records
            image_tags: Dictionary mapping image paths to their applicable tags
            unique_tag_names: List of all unique tag column names discovered from tags.config
        """
        super().__init__()
        self.image_files = image_files
        self.db_path = db_path
        self.field_mapping_config = field_mapping_config
        self.selected_fields = selected_fields
        self.append_mode = append_mode
        self.image_tags = image_tags or {}
        self.unique_tag_names = unique_tag_names or []
        self.db_manager = DatabaseManager(db_path)

    def _apply_mapping_and_filter(self, raw_metadata: Dict[str, Any], is_xml_source: bool) -> Dict[str, Any]:
        """Process prefixed metadata fields and include all data in database with prefixes."""
        final_data_for_db = {}

        # NEW APPROACH: Include ALL metadata with prefixes as column names
        # This preserves all data from all sources (EXIF_, JSON_, XML_)
        for raw_key, raw_value in raw_metadata.items():
            # Use the prefixed field name directly as the database column name
            # This way we preserve the source information (EXIF_, JSON_, XML_)
            final_data_for_db[raw_key] = raw_value
        
        logger.debug(f"All prefixed metadata for DB: {len(final_data_for_db)} fields")
        logger.debug(f"Sample fields: {list(final_data_for_db.keys())[:10]}")
        
        return final_data_for_db

    def run(self):
        """Process all images and store in database."""
        try:
            # --- Database Setup --- 
            if not self.append_mode and os.path.exists(self.db_path):
                try:
                    os.remove(self.db_path)
                    logger.info(f"Removed existing database for new creation: {self.db_path}")
                except OSError as e_remove:
                    logger.error(f"Error removing existing database {self.db_path}: {e_remove}")
                    self.error.emit(f"Could not remove existing database: {e_remove}")
                    return # Cannot proceed if DB removal fails

            self.db_manager.initialize_database() # Creates table if not exists, adds default columns/indexes
            logger.info("Database initialized (table and base columns/indexes ensured).")

            # Ensure all discovered tag columns exist before processing images
            if self.unique_tag_names:
                logger.info(f"Ensuring {len(self.unique_tag_names)} discovered tag columns exist...")
                # DatabaseManager.add_columns_if_needed will handle type (TEXT COLLATE NOCASE for tags) and indexing.
                # We need to pass it as a dictionary {column_name: type_string}
                tag_columns_to_ensure = {tag_name: "TEXT COLLATE NOCASE" for tag_name in self.unique_tag_names}
                self.db_manager.add_columns_if_needed(tag_columns_to_ensure)
                logger.info("Finished ensuring tag columns.")
            # --- End Database Setup ---

            total = len(self.image_files)
            batch_size = 100  # Process in batches for better performance
            current_batch_for_db = []
            
            for i, image_path in enumerate(self.image_files):
                try:
                    logger.debug(f"BatchWorker processing: {image_path}")
                    raw_metadata_from_exif_utils, is_xml = extract_exif_data(image_path)
                    
                    if not raw_metadata_from_exif_utils:
                        logger.warning(f"No metadata extracted by exif_utils for {image_path}, skipping.")
                        continue
                    logger.debug(f"Raw metadata for {image_path} (is_xml={is_xml}): {raw_metadata_from_exif_utils}")

                    # Apply mapping (raw keys to DB keys) and filter (based on GUI selected DB keys)
                    data_for_db = self._apply_mapping_and_filter(raw_metadata_from_exif_utils, is_xml)
                    
                    # Add essential path information (will overwrite if they were mapped/selected with these names)
                    data_for_db['path'] = image_path
                    data_for_db['File_Location_Folder'] = os.path.basename(os.path.dirname(image_path))
                    parent_dir = os.path.dirname(os.path.dirname(image_path))
                    data_for_db['File_Location_Session'] = os.path.basename(parent_dir) if parent_dir else ''
                    
                    # Apply tags from tags.config files
                    if image_path in self.image_tags:
                        applicable_tags = self.image_tags[image_path]
                        data_for_db.update(applicable_tags)
                        logger.debug(f"Applied {len(applicable_tags)} tags to {image_path}")
                    
                    # Check if we have metadata beyond just path information
                    # Count non-path fields (those that start with EXIF_, JSON_, or XML_)
                    metadata_field_count = sum(1 for k in data_for_db.keys() 
                                             if k.startswith(('EXIF_', 'JSON_', 'XML_')))
                    
                    if metadata_field_count == 0:
                        logger.warning(f"No metadata fields found for {image_path}. Only path info available. Skipping DB insert.")
                        continue

                    logger.debug(f"Data for DB for {image_path} (post-pathinfo): {data_for_db}")
                    current_batch_for_db.append(data_for_db)
                    
                    if len(current_batch_for_db) >= batch_size:
                        # --- BEGIN DEBUG: Log types in batch before bulk insert ---
                        logger.debug(f"Preparing to bulk insert batch of {len(current_batch_for_db)} items.")
                        for idx, item_data in enumerate(current_batch_for_db):
                            logger.debug(f"  Item {idx} in batch - Path: {item_data.get('path')}")
                            for key, value in item_data.items():
                                if isinstance(value, Fraction):
                                    logger.error(f"    WORKER: UNEXPECTED FRACTION in batch: Key: {key}, Type: {type(value)}, Value: {value}")
                                elif isinstance(value, tuple) and value and all(isinstance(x, Fraction) for x in value):
                                    logger.error(f"    WORKER: UNEXPECTED TUPLE OF FRACTIONS in batch: Key: {key}, Type: {type(value)}, Value: {value}")
                                else:
                                    logger.debug(f"    Key: {key}, Type: {type(value)}")
                        # --- END DEBUG ---
                        self.db_manager.bulk_insert_images(current_batch_for_db)
                        current_batch_for_db = []
                    
                    self.progress.emit(int((i + 1) / total * 100))
                    self.file_processed.emit(image_path)
                    
                except Exception as e:
                    logger.error(f"Error processing file {image_path} in BatchWorker: {str(e)}", exc_info=True)
                    self.error.emit(f"Error processing {image_path}: {str(e)}")
                    # Decide if we should continue with next file or stop batch
                    continue # For now, continue with the next file
            
            # Process remaining batch
            if current_batch_for_db:
                # --- BEGIN DEBUG: Log types in final batch before bulk insert ---
                logger.debug(f"Preparing to bulk insert FINAL batch of {len(current_batch_for_db)} items.")
                for idx, item_data in enumerate(current_batch_for_db):
                    logger.debug(f"  Item {idx} in FINAL batch - Path: {item_data.get('path')}")
                    for key, value in item_data.items():
                        if isinstance(value, Fraction):
                            logger.error(f"    WORKER: UNEXPECTED FRACTION in FINAL batch: Key: {key}, Type: {type(value)}, Value: {value}")
                        elif isinstance(value, tuple) and value and all(isinstance(x, Fraction) for x in value):
                            logger.error(f"    WORKER: UNEXPECTED TUPLE OF FRACTIONS in FINAL batch: Key: {key}, Type: {type(value)}, Value: {value}")
                        else:
                            logger.debug(f"    Key: {key}, Type: {type(value)}")
                # --- END DEBUG ---
                self.db_manager.bulk_insert_images(current_batch_for_db)
            
            self.finished.emit()
            
        except sqlite3.Error as db_e:
            # Catch database-specific errors separately
            logger.error(f"Database error in BatchProcessWorker run loop: {str(db_e)}", exc_info=True)
            self.error.emit(f"Database error during batch processing: {str(db_e)}")
        except Exception as e:
            # This catches other non-database errors not tied to a specific file
            logger.error(f"Fatal error in BatchProcessWorker run loop: {str(e)}", exc_info=True)
            self.error.emit(f"General batch processing error: {str(e)}")

class RadiusSearchWorker(QThread):
    """Worker thread for searching images within a radius."""
    
    progress = pyqtSignal(int)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(
        self,
        db_path: str,
        latitude: float,
        longitude: float,
        radius: float,
        min_alt: Optional[float] = None,
        max_alt: Optional[float] = None,
        folder: Optional[str] = None,
        session: Optional[str] = None,
        tag_filters: Optional[Dict[str, str]] = None
    ):
        """Initialize the worker.
        
        Args:
            db_path: Path to the database file
            latitude: Center latitude
            longitude: Center longitude
            radius: Search radius in meters
            min_alt: Minimum altitude filter
            max_alt: Maximum altitude filter
            folder: Optional folder filter
            session: Optional session filter
            tag_filters: Optional dictionary of tag column names to values
        """
        super().__init__()
        self.db_manager = DatabaseManager(db_path)
        self.latitude = latitude
        self.longitude = longitude
        self.radius = radius
        self.min_alt = min_alt
        self.max_alt = max_alt
        self.folder = folder
        self.session = session
        self.tag_filters = tag_filters or {}

    def run(self):
        """Search for images within the specified radius."""
        try:
            logger.debug("RadiusSearchWorker DEBUG:")
            logger.debug(f"  Coordinates: {self.latitude}, {self.longitude}")
            logger.debug(f"  Radius: {self.radius}m")
            logger.debug(f"  Folder filter: {self.folder}")
            logger.debug(f"  Session filter: {self.session}")
            logger.debug(f"  Tag filters: {self.tag_filters}")
            logger.debug(f"  Min altitude: {self.min_alt}")
            logger.debug(f"  Max altitude: {self.max_alt}")
            
            # Convert radius to degrees (approximate)
            # 1 degree of latitude = ~111km
            lat_delta = self.radius / 111000  # Convert meters to degrees

            # Avoid division by zero near the poles when computing longitude delta
            lat_rad = math.radians(self.latitude)
            cos_lat = math.cos(lat_rad)
            if abs(cos_lat) < 1e-6:
                cos_lat = 1e-6

            lon_delta = lat_delta / abs(cos_lat)
            
            # Get images in bounding box
            results = self.db_manager.get_images_in_radius(
                self.latitude,
                self.longitude,
                lat_delta,
                lon_delta,
                self.folder,
                self.session,
                self.tag_filters
            )
            logger.info(f"Database returned {len(results)} images from bounding box")
            
            # Filter by exact distance and altitude
            images_within_radius = []
            total = len(results)
            
            for i, (path, lat, lon, altitude, folder, session) in enumerate(results):
                try:
                    lat = float(lat)
                    lon = float(lon)
                    
                    # Calculate exact distance
                    distance = geodesic(
                        (self.latitude, self.longitude),
                        (lat, lon)
                    ).kilometers
                    
                    # Check if within radius
                    if distance <= (self.radius / 1000):  # Convert meters to km
                        # Parse altitude if present
                        if altitude is not None and altitude != '':
                            try:
                                alt_value = float(str(altitude).replace('m', ''))
                                # Apply altitude filters
                                if self.min_alt is not None and alt_value < self.min_alt:
                                    continue
                                if self.max_alt is not None and alt_value > self.max_alt:
                                    continue
                            except (ValueError, AttributeError):
                                alt_value = None
                        else:
                            alt_value = None
                        
                        images_within_radius.append(
                            (path, distance, alt_value, folder, session)
                        )
                    
                    self.progress.emit(int((i + 1) / total * 100))
                    
                except (ValueError, TypeError) as e:
                    logger.error(f"Error processing result {path}: {str(e)}")
                    continue
            
            self.finished.emit(images_within_radius)
            
        except Exception as e:
            logger.error(f"Error in RadiusSearchWorker: {str(e)}")
            self.error.emit(str(e)) 