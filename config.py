"""Configuration settings and constants for the EXIF Extractor application."""

import os
from typing import List, Dict, Any

# File extensions
SUPPORTED_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tiff', '.bmp')

# Database settings
DEFAULT_DB_EXTENSION = '.db'
DEFAULT_TABLE_NAME = 'images'

# GPS EXIF tag constants
GPS_VERSION = 0
GPS_LATITUDE_REF = 1
GPS_LATITUDE = 2
GPS_LONGITUDE_REF = 3
GPS_LONGITUDE = 4
GPS_ALTITUDE_REF = 5
GPS_ALTITUDE = 6
GPS_TIMESTAMP = 7
GPS_SATELLITES = 8
GPS_STATUS = 9
GPS_MEASURE_MODE = 10
GPS_DOP = 11
GPS_SPEED_REF = 12
GPS_SPEED = 13
GPS_TRACK_REF = 14
GPS_TRACK = 15
GPS_DATE_STAMP = 29

# Map settings
DEFAULT_RADIUS_METERS = 500
DEFAULT_MIN_ALTITUDE = -1000
DEFAULT_MAX_ALTITUDE = 10000
DEFAULT_ZOOM_LEVEL = 12
DEFAULT_MAP_TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'

# UI settings
MAIN_WINDOW_MIN_WIDTH = 800
MAIN_WINDOW_MIN_HEIGHT = 600
SPLITTER_RATIO = 0.7  # 70% top, 30% bottom

# Database schema
TABLE_SCHEMA = {
    'path': 'TEXT PRIMARY KEY',
    'File_Location_Folder': 'TEXT',
    'File_Location_Session': 'TEXT',
    # GPS and timing data
    'GPS_Latitude': 'TEXT',
    'GPS_Longitude': 'TEXT',
    'GPS_Altitude': 'TEXT',
    'GPS_TimeStamp': 'TEXT',
    'GPS_DateStamp': 'TEXT',
    'GPS_DateTime': 'TEXT',
    # Camera settings
    'Shutter_Speed': 'TEXT',
    'Aperture': 'TEXT',
    'Focal_Length': 'TEXT',
    'ISO': 'TEXT',
    'Capture_Time': 'TEXT',
    'Camera_Make': 'TEXT',
    'Camera_Model': 'TEXT',
    'Lens': 'TEXT',
    # XML-specific fields
    'Depth': 'TEXT',
    'Pitch': 'TEXT',
    'Roll': 'TEXT',
    'Yaw': 'TEXT',
    # Acquisition data
    'Acquisition_exposure': 'TEXT',
    'Acquisition_digital_gain': 'TEXT',
    'Acquisition_analog_gain': 'TEXT',
    'Acquisition_sensor_gain': 'TEXT',
    'Acquisition_aperture': 'TEXT',
    'Acquisition_focus': 'TEXT',
    'Acquisition_name': 'TEXT',
    'Acquisition_camera_session_name': 'TEXT',
    # Version information
    'Version_software': 'TEXT',
    'Version_fpga': 'TEXT',
    'Version_pic': 'TEXT',
    'Version_serial_number': 'TEXT'
}

# SQL query templates
SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS {table_name} (
    {columns}
)
"""

SQL_INSERT_IMAGE = """
INSERT OR REPLACE INTO {table_name} ({columns})
VALUES ({placeholders})
"""

SQL_SELECT_IMAGES_IN_RADIUS = """
SELECT path, GPS_Latitude, GPS_Longitude, GPS_Altitude,
       File_Location_Folder, File_Location_Session
FROM {table_name}
WHERE GPS_Latitude IS NOT NULL 
AND GPS_Longitude IS NOT NULL
AND GPS_Latitude BETWEEN :lat - :lat_delta AND :lat + :lat_delta
AND GPS_Longitude BETWEEN :lon - :lon_delta AND :lon + :lon_delta
"""

# Index definitions
INDEXES = [
    ('idx_gps_coords', ['GPS_Latitude', 'GPS_Longitude']),
    ('idx_capture_time', ['Capture_Time']),
    ('idx_location', ['File_Location_Folder', 'File_Location_Session'])
] 