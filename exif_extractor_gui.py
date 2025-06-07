import sys
import os
import traceback
import shutil
import json
import re
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Dict, Any, List, Tuple, Optional
import logging

# Add the current directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Configure logging for the GUI module
logger = logging.getLogger(__name__) # Create a logger instance
# Basic configuration (can be expanded if needed, e.g., to write to a file or set level)
# Ensure this is called only once, or appropriately guarded if other modules also call basicConfig.
if not logging.getLogger().hasHandlers(): # Check if root logger already has handlers
    logging.basicConfig(level=logging.INFO, 
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Third-party imports
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import sqlite3
from geopy.distance import geodesic
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import folium

# Local imports
from config import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_ALTITUDE, GPS_TIMESTAMP,
    GPS_DATE_STAMP, GPS_LATITUDE_REF, GPS_LONGITUDE_REF,
    GPS_ALTITUDE_REF, MAIN_WINDOW_MIN_WIDTH, MAIN_WINDOW_MIN_HEIGHT
)

# Define supported image extensions
SUPPORTED_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tiff', '.bmp')

# Configuration management functions
def load_config():
    """Load configuration from config.json file."""
    config_file = "config.json"
    default_config = {
        "image_directory": "",
        "database_path": "",
        "directory_history": []
    }
    
    try:
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                config = json.load(f)
                # Ensure all required keys exist
                for key, default_value in default_config.items():
                    if key not in config:
                        config[key] = default_value
                return config
        else:
            return default_config
    except Exception as e:
        print(f"Error loading config: {e}")
        return default_config

def update_config(key, value):
    """Update a configuration value and save to file."""
    config = load_config()
    config[key] = value
    
    # Special handling for directory history
    if key == "image_directory" and value:
        if "directory_history" not in config:
            config["directory_history"] = []
        
        # Add to history if not already there
        if value not in config["directory_history"]:
            config["directory_history"].insert(0, value)
            # Keep only last 5 entries
            config["directory_history"] = config["directory_history"][:5]
    
    try:
        with open("config.json", 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

from workers import ExifExtractorWorker, BatchProcessWorker, RadiusSearchWorker # Added RadiusSearchWorker
from database import DatabaseManager
from exif_utils import (
    convert_to_degrees, format_gps_timestamp, get_gps_info, 
    format_shutter_speed, parse_dms_string_to_dd, # Added parse_dms_string_to_dd
    extract_attributes, convert_to_float, GUIUtils # Added these for GUI functions and GUIUtils
)

# Tags configuration processing functions
def parse_tags_config(config_path: str) -> Dict[str, str]:
    """Parse a tags.config file and return a dictionary of tag name -> value pairs.

    Tag names are normalized to lowercase before converting them into SQL column
    names so that different casings of the same tag are treated identically.
    """
    tags = {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                # Skip empty lines and comments that don't start with #
                if not line or (line.startswith('#') and ':' not in line):
                    continue
                
                # Process tag lines: #<tag_name>: <tag_value>
                if line.startswith('#') and ':' in line:
                    try:
                        # Remove the leading # and split on first colon
                        tag_line = line[1:].strip()
                        if ':' in tag_line:
                            tag_name, tag_value = tag_line.split(':', 1)
                            tag_name = tag_name.strip().lower()
                            tag_value = tag_value.strip()
                            
                            # Validate tag name (must be valid SQL column name)
                            if tag_name and tag_name.replace('_', '').replace('-', '').isalnum():
                                # Convert tag name to valid SQL column name using lower case
                                sql_tag_name = f"Tag_{tag_name.replace('-', '_')}"
                                tags[sql_tag_name] = tag_value
                            else:
                                print(f"Warning: Invalid tag name '{tag_name}' in {config_path}:{line_num}")
                    except Exception as e:
                        print(f"Error parsing line {line_num} in {config_path}: {e}")
                        continue
                        
    except Exception as e:
        print(f"Error reading tags config file {config_path}: {e}")
    
    return tags

def find_applicable_tags(image_path: str) -> Dict[str, str]:
    """Find all applicable tags for an image by walking up the directory tree."""
    applicable_tags = {}
    
    # Start from the image's directory and walk up to root
    current_dir = os.path.dirname(os.path.abspath(image_path))
    
    # Collect tags from all levels (parent tags can be overridden by child tags)
    tags_stack = []
    
    while current_dir:
        config_path = os.path.join(current_dir, 'tags.config')
        if os.path.exists(config_path):
            level_tags = parse_tags_config(config_path)
            if level_tags:
                tags_stack.append((current_dir, level_tags))
        
        # Move up one directory level
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:  # Reached root
            break
        current_dir = parent_dir
    
    # Apply tags from top-level down (so child configs override parent configs)
    for config_dir, level_tags in reversed(tags_stack):
        applicable_tags.update(level_tags)
    
    return applicable_tags

def ensure_tag_columns_exist(db_path: str, tag_names: List[str]) -> bool:
    """Ensure all tag columns exist in the database, creating them if necessary."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get existing columns
        cursor.execute("PRAGMA table_info(images)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        
        # Add missing tag columns
        for tag_name in tag_names:
            if tag_name not in existing_columns:
                try:
                    # Add new column with TEXT type, default NULL, and case-insensitive collation
                    cursor.execute(f"ALTER TABLE images ADD COLUMN \"{tag_name}\" TEXT COLLATE NOCASE")
                    print(f"Added new tag column: {tag_name} with COLLATE NOCASE")
                except sqlite3.Error as e:
                    print(f"Error adding column {tag_name}: {e}")
                    return False
        
        conn.commit()
        conn.close()
        return True
        
    except Exception as e:
        print(f"Error ensuring tag columns exist: {e}")
        return False

def apply_tags_to_image_record(cursor, image_id: int, tags: Dict[str, str]):
    """Apply tags to an existing image record in the database."""
    if not tags:
        return
    
    try:
        # Build UPDATE query for all tags
        set_clauses = []
        values = []
        
        for tag_name, tag_value in tags.items():
            set_clauses.append(f"{tag_name} = ?")
            values.append(tag_value)
        
        if set_clauses:
            values.append(image_id)  # For WHERE clause
            update_query = f"UPDATE images SET {', '.join(set_clauses)} WHERE rowid = ?"
            cursor.execute(update_query, values)
            
    except Exception as e:
        print(f"Error applying tags to image record {image_id}: {e}")

def process_tags_for_batch(image_files: List[str], db_path: str) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Process tags.config files for a batch of images.
    
    Returns:
        A tuple containing:
            - all_image_tags: Dict mapping image_path to its resolved tags.
            - all_unique_tag_names: List of all unique tag column names found.
    """
    # Collect all tags for all images
    all_image_tags: Dict[str, Dict[str, str]] = {}
    all_unique_tag_names_set: set[str] = set()
    
    logger.info("Processing tags.config files to discover tags...")
    for image_path in image_files:
        tags = find_applicable_tags(image_path)
        if tags:
            normalized_tags = {}
            for t_name, t_value in tags.items():
                if t_name.startswith('Tag_'):
                    normalized_name = 'Tag_' + t_name[4:].lower()
                else:
                    normalized_name = t_name.lower()
                normalized_tags[normalized_name] = t_value

            all_image_tags[image_path] = normalized_tags
            all_unique_tag_names_set.update(normalized_tags.keys())
    
    all_unique_tag_names_list = sorted(list(all_unique_tag_names_set))
    
    if all_unique_tag_names_list:
        logger.info(f"Discovered {len(all_unique_tag_names_list)} unique tag types from tags.config files: {', '.join(all_unique_tag_names_list)}")
    else:
        logger.info("No tags discovered from tags.config files.")
            
    # The responsibility of ensuring columns exist is moved to BatchProcessWorker
    # We no longer call ensure_tag_columns_exist here.
    
    return all_image_tags, all_unique_tag_names_list



def extract_xml_metadata(img_path):
    """Extract metadata from XML in JPG comment field."""
    try:
        img = Image.open(img_path)
        comment = img.info.get('comment')
        
        if comment is None:
            return None
            
        comment = comment.decode('utf-8', 'ignore')
        metadata = {}
        
        # Extract coordinates
        coords_match = re.search(r'<Coords\s+([^>]+?)/?>', comment)
        if coords_match:
            coords_attrs = extract_attributes(coords_match.group(1))
            metadata['GPS_Latitude'] = convert_to_float(coords_attrs.get('lat'))
            metadata['GPS_Longitude'] = convert_to_float(coords_attrs.get('long'))
        
        # Extract depth and altitude
        depth_match = re.search(r'<Depth\s+([^>]+?)/?>', comment)
        if depth_match:
            depth_attrs = extract_attributes(depth_match.group(1))
            metadata['GPS_Altitude'] = convert_to_float(depth_attrs.get('altitude'))
            metadata['Depth'] = convert_to_float(depth_attrs.get('depth'))
        
        # Extract direction
        direction_match = re.search(r'<Direction\s+([^>]+?)/?>', comment)
        if direction_match:
            direction_attrs = extract_attributes(direction_match.group(1))
            metadata['Pitch'] = convert_to_float(direction_attrs.get('pitch'))
            metadata['Roll'] = convert_to_float(direction_attrs.get('roll'))
            metadata['Yaw'] = convert_to_float(direction_attrs.get('yaw'))
        
        # Extract Position wrapper attributes
        position_match = re.search(r'<Position\s+([^>]+?)>', comment)
        if position_match:
            position_attrs = extract_attributes(position_match.group(1))
            metadata['Position_Extrapolated'] = position_attrs.get('extrapolated')
            metadata['Position_Time'] = position_attrs.get('time')
            metadata['Position_Received'] = position_attrs.get('received')
            metadata['Position_Age'] = convert_to_float(position_attrs.get('age'))
            metadata['Position_Transponder_ID'] = position_attrs.get('transponder_id')
        
        # Extract acquisition data
        acq_match = re.search(r'<acquisition>(.*?)</acquisition>', comment, re.DOTALL)
        if acq_match:
            acq_content = acq_match.group(1)
            # Extract simple tag values - expanded list to include more fields
            for tag in ['exposure', 'digital_gain', 'analog_gain', 'sensor_gain', 'aperture', 'focus', 'name', 'camera_session_name', 'camera_sub_session_name', 'focus_enc', 'width', 'height', 'seq_slot', 'dequeue_time']:
                tag_match = re.search(f'<{tag}>(.*?)</{tag}>', acq_content)
                if tag_match:
                    metadata[f'Acquisition_{tag}'] = tag_match.group(1)
        
        # Extract version and hardware information
        versions_match = re.search(r'<versions>(.*?)</versions>', comment, re.DOTALL)
        if versions_match:
            versions_content = versions_match.group(1)
            # Extract version information
            for tag in ['software', 'fpga', 'pic', 'serial_number']:
                tag_match = re.search(f'<{tag}>(.*?)</{tag}>', versions_content)
                if tag_match:
                    metadata[f'Version_{tag}'] = tag_match.group(1)
        
        # Extract image time and date
        img_attrs = extract_attributes(re.search(r'<image\s+([^>]+?)/?>', comment).group(1))
        if 'time' in img_attrs and 'date' in img_attrs:
            metadata['Capture_Time'] = f"{img_attrs['date']} {img_attrs['time']}"
        if 'acq_index' in img_attrs:
            metadata['Acquisition_Index'] = img_attrs.get('acq_index')
        
        return metadata
    except Exception as e:
        print(f"Error extracting XML metadata: {str(e)}")
        return None

# PyQt6 imports
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                           QProgressBar, QTableWidget, QTableWidgetItem, 
                           QMessageBox, QCheckBox, QHeaderView, QTabWidget,
                           QLineEdit, QSpinBox, QDoubleSpinBox, QSplitter,
                           QGroupBox, QSlider, QComboBox, QScrollArea, QGridLayout, QDialog, QFormLayout, QTreeView, QButtonGroup, QRadioButton)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QObject, pyqtSlot, QTimer, QDir
from PyQt6.QtGui import QImage, QPixmap, QStandardItemModel, QStandardItem, QColor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtGui import QShortcut

# Add debug prints
print("Starting EXIF Extractor GUI application...")
print("All imports successful")

try:
    def format_gps_timestamp(timestamp):
        """Format GPS timestamp into readable format."""
        try:
            if isinstance(timestamp, tuple) and len(timestamp) == 3:
                hours = int(float(timestamp[0]))
                minutes = int(float(timestamp[1]))
                seconds = int(float(timestamp[2]))
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        except Exception as e:
            print(f"Error formatting GPS timestamp {timestamp}: {str(e)}")
        return str(timestamp)

    def get_gps_info(exif):
        """Extract GPS information from EXIF data."""
        if not exif:
            return {}

        gps_data = {}
        
        # Find the GPS IFD in EXIF
        for tag_id, value in exif.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == 'GPSInfo':
                try:
                    # Process each GPS tag
                    for gps_tag in value.keys():
                        sub_value = value[gps_tag]
                        sub_name = GPSTAGS.get(gps_tag, str(gps_tag))
                        
                        if gps_tag == 2:  # Latitude
                            if isinstance(sub_value, tuple) and len(sub_value) == 3:
                                lat = convert_to_degrees(sub_value)
                                if lat is not None:
                                    ref = value.get(1, 'N')  # Get the N/S reference
                                    if ref != 'N':
                                        lat = -lat
                                    gps_data['GPS_Latitude'] = f"{lat:.6f}"
                        
                        elif gps_tag == 4:  # Longitude
                            if isinstance(sub_value, tuple) and len(sub_value) == 3:
                                lon = convert_to_degrees(sub_value)
                                if lon is not None:
                                    ref = value.get(3, 'E')  # Get the E/W reference
                                    if ref != 'E':
                                        lon = -lon
                                    gps_data['GPS_Longitude'] = f"{lon:.6f}"
                        
                        elif gps_tag == 6:  # Altitude
                            try:
                                alt = float(sub_value)
                                ref = value.get(5, 0)  # Get the altitude reference (0=above sea level, 1=below sea level)
                                if ref == 1:
                                    alt = -alt
                                gps_data['GPS_Altitude'] = f"{alt:.1f}m"
                            except (ValueError, TypeError):
                                pass
                        
                        elif gps_tag == 7:  # Timestamp
                            if isinstance(sub_value, tuple):
                                gps_data['GPS_TimeStamp'] = format_gps_timestamp(sub_value)
                        
                        elif gps_tag == 29:  # Datestamp
                            gps_data['GPS_DateStamp'] = str(sub_value)

                except Exception as e:
                    print(f"Error processing GPS data: {str(e)}")
                    continue

        # If we have both date and time, combine them
        if 'GPS_DateStamp' in gps_data and 'GPS_TimeStamp' in gps_data:
            try:
                date_str = gps_data['GPS_DateStamp']
                time_str = gps_data['GPS_TimeStamp']
                datetime_str = f"{date_str} {time_str}"
                gps_data['GPS_DateTime'] = datetime_str
            except Exception as e:
                print(f"Error combining GPS date and time: {str(e)}")

        return gps_data

    def format_shutter_speed(value):
        """Convert shutter speed to a readable format."""
        if isinstance(value, Fraction):
            if value.denominator == 1:
                return str(value.numerator)
            if value < 1:
                return f"1/{int(1/float(value))}"
            return f"{value.numerator}/{value.denominator}"
        return str(value)

    class MapHandler(QObject):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.parent = parent

        @pyqtSlot(float, float, float)
        def handleMapClick(self, lat, lon, radius):
            if self.parent:
                self.parent.selection_made.emit(lat, lon, radius)

    class MapWidget(QWidget):
        selection_made = pyqtSignal(float, float, float)  # lat, lon, radius

        def __init__(self):
            super().__init__()
            layout = QVBoxLayout(self)
            
            # Create the web view for the map
            self.web_view = QWebEngineView()
            layout.addWidget(self.web_view)
            
            # Add load finished diagnostic
            self.web_view.loadFinished.connect(lambda ok: print("Map loadFinished:", ok))
            
            # Set up the channel to handle JavaScript communication
            self.channel = QWebChannel()
            self.handler = MapHandler(self)
            self.channel.registerObject('handler', self.handler)
            self.web_view.page().setWebChannel(self.channel)
            
            # Create initial map
            self.create_map()

        def get_html_template(self, map_content: str) -> str:
            """Returns the HTML template with the map content."""
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
                <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
                <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
                <style>
                    html, body {{
                        height: 100%;
                        margin: 0;
                        padding: 0;
                    }}
                    #map {{
                        height: 100%;
                        width: 100%;
                    }}
                </style>
            </head>
            <body>
                <div id="map"></div>
                <script>
                    // Initialize the map
                    var map = L.map('map').setView([0, 0], 2);
                    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                        maxZoom: 25,  // Increased max zoom level
                        attribution: '© OpenStreetMap contributors'
                    }}).addTo(map);

                    var circle = null;
                    var centerMarker = null;
                    var currentRadius = 500;  // Default radius in meters

                    // Initialize QWebChannel
                    new QWebChannel(qt.webChannelTransport, function (channel) {{
                        window.handler = channel.objects.handler;
                        
                        // Set up click handler
                        map.on('click', function(e) {{
                            if (circle) {{
                                map.removeLayer(circle);
                            }}
                            if (centerMarker) {{
                                map.removeLayer(centerMarker);
                            }}
                            
                            centerMarker = L.marker(e.latlng).addTo(map);
                            circle = L.circle(e.latlng, {{
                                color: 'red',
                                fillColor: '#f03',
                                fillOpacity: 0.2,
                                radius: currentRadius
                            }}).addTo(map);
                            
                            window.handler.handleMapClick(
                                e.latlng.lat,
                                e.latlng.lng,
                                currentRadius
                            );
                        }});

                        // Function to update circle radius
                        window.updateRadius = function(newRadius) {{
                            currentRadius = newRadius;
                            if (circle && centerMarker) {{
                                let center = centerMarker.getLatLng();
                                map.removeLayer(circle);
                                circle = L.circle(center, {{
                                    color: 'red',
                                    fillColor: '#f03',
                                    fillOpacity: 0.2,
                                    radius: currentRadius
                                }}).addTo(map);
                                
                                // Notify Python of the update
                                window.handler.handleMapClick(
                                    center.lat,
                                    center.lng,
                                    currentRadius
                                );
                            }}
                        }};

                        // Add the markers
                        {map_content}
                    }});
                </script>
            </body>
            </html>
            """

        def create_map(self, center: Tuple[float, float] = (0, 0)):
            """Create the map centered at the given coordinates."""
            # Generate JavaScript to set the view
            map_content = f"map.setView([{center[0]}, {center[1]}], 2);"
            
            # Set the HTML directly
            html = self.get_html_template(map_content)
            self.web_view.setHtml(html, QUrl("qrc:///"))

        def add_image_markers(self, coordinates: List[Tuple[float, float, str]]):
            """Add markers for images to the map."""
            if not coordinates:
                return
            
            # Calculate center point for the map
            lats = [lat for lat, _, _ in coordinates]
            lons = [lon for _, lon, _ in coordinates]
            center_lat = sum(lats) / len(lats)
            center_lon = sum(lons) / len(lons)
            
            # Generate JavaScript for markers
            markers_js = []
            for lat, lon, path in coordinates:
                markers_js.append(
                    f"L.marker([{lat}, {lon}])"
                    f".bindPopup('{os.path.basename(path)}').addTo(map);"
                )
            
            map_content = f"""
                map.setView([{center_lat}, {center_lon}], 12);
                {' '.join(markers_js)}
            """
            
            # Set the HTML directly
            html = self.get_html_template(map_content)
            self.web_view.setHtml(html, QUrl("qrc:///"))
            print(f"Added {len(coordinates)} markers to map")  # Debug print

    class RoutePlaybackWidget(QWidget):
        def __init__(self):
            super().__init__()
            self.setup_ui()
            self.route_data = []
            self.current_index = 0
            self.is_playing = False
            self.playback_speed = 1.0
            self.timer = QTimer()
            self.timer.timeout.connect(self.update_playback)
            self.selected_files = set()
            self.current_folder = None
            self.current_session = None

        def setup_ui(self):
            layout = QVBoxLayout(self)

            # Create splitter for map and controls
            splitter = QSplitter(Qt.Orientation.Vertical)
            layout.addWidget(splitter)

            # Top section with map and altitude profile
            top_widget = QWidget()
            top_layout = QVBoxLayout(top_widget)
            
            # Map view
            self.map_widget = QWebEngineView()
            top_layout.addWidget(self.map_widget, stretch=2)
            
            # Altitude profile using Plotly
            self.altitude_widget = QWebEngineView()
            top_layout.addWidget(self.altitude_widget, stretch=1)
            
            top_widget.setLayout(top_layout)
            splitter.addWidget(top_widget)

            # Bottom section with controls
            controls_widget = QWidget()
            controls_layout = QVBoxLayout(controls_widget)

            # Database selection section
            selection_group = QGroupBox("Image Selection")
            selection_layout = QVBoxLayout()

            # Database selection
            db_layout = QHBoxLayout()
            db_label = QLabel("Database:")
            self.db_path = QLabel("Not selected")
            db_btn = QPushButton("Select Database")
            db_btn.clicked.connect(self.select_database)
            db_layout.addWidget(db_label)
            db_layout.addWidget(self.db_path)
            db_layout.addWidget(db_btn)
            selection_layout.addLayout(db_layout)

            # Add folder and session filter dropdowns
            filter_layout = QHBoxLayout()
            
            # Folder filter
            folder_layout = QHBoxLayout()
            folder_label = QLabel("Filter by Folder:")
            self.folder_combo = QComboBox()
            self.folder_combo.addItem("All Folders")
            self.folder_combo.currentTextChanged.connect(self.apply_filters)
            folder_layout.addWidget(folder_label)
            folder_layout.addWidget(self.folder_combo)
            filter_layout.addLayout(folder_layout)
            
            # Session filter
            session_layout = QHBoxLayout()
            session_label = QLabel("Filter by Session:")
            self.session_combo = QComboBox()
            self.session_combo.addItem("All Sessions")
            self.session_combo.currentTextChanged.connect(self.apply_filters)
            session_layout.addWidget(session_label)
            session_layout.addWidget(self.session_combo)
            filter_layout.addLayout(session_layout)
            
            selection_layout.addLayout(filter_layout)

            # Add Select All checkbox above the table
            select_all_layout = QHBoxLayout()
            self.select_all_checkbox = QCheckBox("Select All Images")
            self.select_all_checkbox.stateChanged.connect(self.toggle_all_selections)
            select_all_layout.addWidget(self.select_all_checkbox)
            select_all_layout.addStretch()
            selection_layout.addLayout(select_all_layout)

            # Image list from database
            self.files_list = QTableWidget()
            self.files_list.setColumnCount(5)  # Added column for folder/session info
            self.files_list.setHorizontalHeaderLabels(["Select", "File Path", "Capture Time", "Location", "Folder/Session"])
            self.files_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.files_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.files_list.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            self.files_list.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            selection_layout.addWidget(self.files_list)

            # Load and clear buttons
            button_layout = QHBoxLayout()
            load_route_btn = QPushButton("Load Selected as Route")
            load_route_btn.clicked.connect(self.load_selected_as_route)
            clear_btn = QPushButton("Clear Selection")
            clear_btn.clicked.connect(self.clear_selection)
            button_layout.addWidget(load_route_btn)
            button_layout.addWidget(clear_btn)
            selection_layout.addLayout(button_layout)

            selection_group.setLayout(selection_layout)
            controls_layout.addWidget(selection_group)

            # Playback controls
            playback_controls = QHBoxLayout()
            
            self.play_button = QPushButton("Play")
            self.play_button.clicked.connect(self.toggle_playback)
            playback_controls.addWidget(self.play_button)
            
            self.speed_combo = QComboBox()
            self.speed_combo.addItems(["0.5x", "1x", "2x", "4x", "8x"])
            self.speed_combo.setCurrentText("1x")
            self.speed_combo.currentTextChanged.connect(self.change_speed)
            playback_controls.addWidget(self.speed_combo)
            
            self.reset_button = QPushButton("Reset")
            self.reset_button.clicked.connect(self.reset_playback)
            playback_controls.addWidget(self.reset_button)
            
            controls_layout.addLayout(playback_controls)

            # Timeline slider
            self.timeline_slider = QSlider(Qt.Orientation.Horizontal)
            self.timeline_slider.setMinimum(0)
            self.timeline_slider.setMaximum(100)
            self.timeline_slider.valueChanged.connect(self.slider_changed)
            controls_layout.addWidget(self.timeline_slider)

            # Statistics
            stats_group = QGroupBox("Route Statistics")
            stats_layout = QVBoxLayout()
            
            self.total_distance_label = QLabel("Total Distance: 0.0 km")
            self.elevation_gain_label = QLabel("Elevation Gain: 0.0 m")
            self.current_altitude_label = QLabel("Current Altitude: 0.0 m")
            self.current_time_label = QLabel("Current Time: --:--:--")
            
            stats_layout.addWidget(self.total_distance_label)
            stats_layout.addWidget(self.elevation_gain_label)
            stats_layout.addWidget(self.current_altitude_label)
            stats_layout.addWidget(self.current_time_label)
            
            stats_group.setLayout(stats_layout)
            controls_layout.addWidget(stats_group)

            splitter.addWidget(controls_widget)
            
            # Set the initial sizes of the splitter (70% top, 30% bottom)
            total_height = 800  # Default height
            splitter.setSizes([int(total_height * 0.7), int(total_height * 0.3)])

        def select_database(self):
            """Select and load images from a database file."""
            try:
                conn = sqlite3.connect(self.db_path.text())
                cursor = conn.cursor()

                # First, detect the actual GPS column names in the database
                cursor.execute("PRAGMA table_info(images)")
                columns = [row[1] for row in cursor.fetchall()]
                
                # Find GPS latitude and longitude columns
                lat_columns = [col for col in columns if 'latitude' in col.lower() and 'gps' in col.lower()]
                lon_columns = [col for col in columns if 'longitude' in col.lower() and 'gps' in col.lower()]
                alt_columns = [col for col in columns if 'altitude' in col.lower() and 'gps' in col.lower()]
                
                if not lat_columns or not lon_columns:
                    QMessageBox.warning(self, "Error", "No GPS coordinate columns found in database.")
                    conn.close()
                    return
                
                # Use the first available GPS coordinate columns
                lat_col = lat_columns[0]
                lon_col = lon_columns[0]
                alt_col = alt_columns[0] if alt_columns else None

                # First, get unique folders and sessions for filters
                cursor.execute("""
                    SELECT DISTINCT File_Location_Folder
                    FROM images
                    ORDER BY File_Location_Folder
                """)
                folders = [row[0] for row in cursor.fetchall()]
                
                cursor.execute("""
                    SELECT DISTINCT File_Location_Session
                    FROM images
                    ORDER BY File_Location_Session
                """)
                sessions = [row[0] for row in cursor.fetchall()]

                # Update combo boxes
                self.folder_combo.clear()
                self.folder_combo.addItem("All Folders")
                self.folder_combo.addItems(folders)
                
                self.session_combo.clear()
                self.session_combo.addItem("All Sessions")
                self.session_combo.addItems(sessions)

                # Get all images with GPS coordinates and timestamps
                if alt_col:
                    query = f"""
                        SELECT path, {lat_col}, {lon_col}, {alt_col}, Capture_Time,
                               File_Location_Folder, File_Location_Session
                        FROM images 
                        WHERE {lat_col} IS NOT NULL 
                        AND {lon_col} IS NOT NULL
                        ORDER BY Capture_Time
                    """
                else:
                    query = f"""
                        SELECT path, {lat_col}, {lon_col}, NULL, Capture_Time,
                               File_Location_Folder, File_Location_Session
                        FROM images 
                        WHERE {lat_col} IS NOT NULL 
                        AND {lon_col} IS NOT NULL
                        ORDER BY Capture_Time
                    """
                
                cursor.execute(query)
                records = cursor.fetchall()
                self.files_list.setRowCount(len(records))
                
                # Reset select all checkbox state
                self.select_all_checkbox.setChecked(False)
                
                for i, (path, lat, lon, alt, time, folder, session) in enumerate(records):
                    # Create checkbox using the utility function
                    checkbox_widget = GUIUtils.create_table_checkbox_widget(parent=self.files_list)
                    
                    # Convert lat/lon to float for formatting
                    try:
                        lat_float = float(lat)
                        lon_float = float(lon)
                        location_str = f"{lat_float:.6f}, {lon_float:.6f}"
                    except (ValueError, TypeError):
                        location_str = f"{lat}, {lon}"
                    
                    # Add items to table
                    self.files_list.setCellWidget(i, 0, checkbox_widget)
                    self.files_list.setItem(i, 1, QTableWidgetItem(path))
                    self.files_list.setItem(i, 2, QTableWidgetItem(str(time) if time else "Unknown"))
                    self.files_list.setItem(i, 3, QTableWidgetItem(location_str))
                    self.files_list.setItem(i, 4, QTableWidgetItem(f"{folder}/{session}"))

                conn.close()
                QMessageBox.information(self, "Success", f"Loaded {len(records)} images from database")

            except Exception as e:
                print(f"Error loading images from database: {str(e)}")
                QMessageBox.critical(self, "Error", f"Error loading images from database: {str(e)}")

        def apply_filters(self):
            """Apply folder and session filters to the image list."""
            selected_folder = self.folder_combo.currentText()
            selected_session = self.session_combo.currentText()
            
            # Store current selections before filtering
            selected_paths = set()
            for i in range(self.files_list.rowCount()):
                checkbox_widget = self.files_list.cellWidget(i, 0)
                if checkbox_widget and checkbox_widget.findChild(QCheckBox).isChecked():
                    path = self.files_list.item(i, 1).text()
                    selected_paths.add(path)

            try:
                conn = sqlite3.connect(self.db_path.text())
                cursor = conn.cursor()

                # Detect the actual GPS column names in the database
                cursor.execute("PRAGMA table_info(images)")
                columns = [row[1] for row in cursor.fetchall()]
                
                # Find GPS latitude and longitude columns
                lat_columns = [col for col in columns if 'latitude' in col.lower() and 'gps' in col.lower()]
                lon_columns = [col for col in columns if 'longitude' in col.lower() and 'gps' in col.lower()]
                alt_columns = [col for col in columns if 'altitude' in col.lower() and 'gps' in col.lower()]
                
                if not lat_columns or not lon_columns:
                    QMessageBox.warning(self, "Error", "No GPS coordinate columns found in database.")
                    conn.close()
                    return
                
                # Use the first available GPS coordinate columns
                lat_col = lat_columns[0]
                lon_col = lon_columns[0]
                alt_col = alt_columns[0] if alt_columns else None

                # Build the query based on selected filters
                if alt_col:
                    query = f"""
                        SELECT path, {lat_col}, {lon_col}, {alt_col}, Capture_Time,
                               File_Location_Folder, File_Location_Session
                        FROM images 
                        WHERE {lat_col} IS NOT NULL 
                        AND {lon_col} IS NOT NULL
                    """
                else:
                    query = f"""
                        SELECT path, {lat_col}, {lon_col}, NULL, Capture_Time,
                               File_Location_Folder, File_Location_Session
                        FROM images 
                        WHERE {lat_col} IS NOT NULL 
                        AND {lon_col} IS NOT NULL
                    """
                params = []

                if selected_folder != "All Folders":
                    query += " AND File_Location_Folder = ?"
                    params.append(selected_folder)

                if selected_session != "All Sessions":
                    query += " AND File_Location_Session = ?"
                    params.append(selected_session)

                query += " ORDER BY Capture_Time"
                
                cursor.execute(query, params)
                records = cursor.fetchall()
                
                self.files_list.setRowCount(len(records))
                
                for i, (path, lat, lon, alt, time, folder, session) in enumerate(records):
                    # Create checkbox using the utility function
                    checkbox_widget = GUIUtils.create_table_checkbox_widget(parent=self.files_list)
                    
                    # Restore previous selection state
                    if path in selected_paths:
                        # Access the QCheckBox within the widget to set its state
                        actual_checkbox = checkbox_widget.findChild(QCheckBox)
                        if actual_checkbox:
                            actual_checkbox.setChecked(True)
                    
                    # Convert lat/lon to float for formatting
                    try:
                        lat_float = float(lat)
                        lon_float = float(lon)
                        location_str = f"{lat_float:.6f}, {lon_float:.6f}"
                    except (ValueError, TypeError):
                        location_str = f"{lat}, {lon}"
                    
                    # Add items to table
                    self.files_list.setCellWidget(i, 0, checkbox_widget)
                    self.files_list.setItem(i, 1, QTableWidgetItem(path))
                    self.files_list.setItem(i, 2, QTableWidgetItem(str(time) if time else "Unknown"))
                    self.files_list.setItem(i, 3, QTableWidgetItem(location_str))
                    self.files_list.setItem(i, 4, QTableWidgetItem(f"{folder}/{session}"))

                conn.close()

            except Exception as e:
                print(f"Error applying filters: {str(e)}")
                QMessageBox.critical(self, "Error", f"Error applying filters: {str(e)}")

        def load_selected_as_route(self):
            """Load selected images as a route."""
            selected_paths = []
            try:
                for i in range(self.files_list.rowCount()):
                    checkbox_widget = self.files_list.cellWidget(i, 0)
                    if checkbox_widget:
                        checkbox = checkbox_widget.findChild(QCheckBox)
                        if checkbox and checkbox.isChecked():
                            path = self.files_list.item(i, 1).text()
                            selected_paths.append(path)

                if not selected_paths:
                    QMessageBox.warning(self, "Warning", "Please select at least one image")
                    return

                # Clear existing route data
                self.route_data = []
                prev_coords = None
                total_distance = 0

                try:
                    conn = sqlite3.connect(self.db_path.text())
                    cursor = conn.cursor()

                    # First, detect the actual GPS column names in the database
                    cursor.execute("PRAGMA table_info(images)")
                    columns = [row[1] for row in cursor.fetchall()]
                    
                    # Find GPS latitude and longitude columns
                    lat_columns = [col for col in columns if 'latitude' in col.lower() and 'gps' in col.lower()]
                    lon_columns = [col for col in columns if 'longitude' in col.lower() and 'gps' in col.lower()]
                    alt_columns = [col for col in columns if 'altitude' in col.lower() and 'gps' in col.lower()]
                    
                    if not lat_columns or not lon_columns:
                        QMessageBox.warning(self, "Error", "No GPS coordinate columns found in database.")
                        return
                    
                    # Use the first available GPS coordinate columns
                    lat_col = lat_columns[0]
                    lon_col = lon_columns[0]
                    alt_col = alt_columns[0] if alt_columns else None

                    for path in selected_paths:
                        if alt_col:
                            query = f"""
                                SELECT {lat_col}, {lon_col}, {alt_col}, Capture_Time
                                FROM images 
                                WHERE path = ?
                            """
                        else:
                            query = f"""
                                SELECT {lat_col}, {lon_col}, NULL, Capture_Time
                                FROM images 
                                WHERE path = ?
                            """
                        
                        cursor.execute(query, (path,))
                        
                        record = cursor.fetchone()
                        if record:
                            lat, lon, alt, timestamp = record
                            try:
                                lat = float(lat)
                                lon = float(lon)
                                if alt:
                                    alt = float(alt.replace('m', '')) if isinstance(alt, str) else float(alt)
                                else:
                                    alt = 0
                            except (ValueError, TypeError, AttributeError):
                                print(f"Error converting coordinates for {path}: lat={lat}, lon={lon}, alt={alt}")
                                continue
                            
                            # Calculate distance from previous point
                            if prev_coords:
                                distance = geodesic(prev_coords, (lat, lon)).kilometers
                                total_distance += distance
                            prev_coords = (lat, lon)

                            # Parse timestamp - handle None and invalid formats
                            parsed_timestamp = None
                            if timestamp:
                                try:
                                    parsed_timestamp = datetime.strptime(str(timestamp), '%Y:%m:%d %H:%M:%S')
                                except ValueError:
                                    try:
                                        # Try alternate format if first one fails
                                        parsed_timestamp = datetime.strptime(str(timestamp), '%Y-%m-%d %H:%M:%S')
                                    except ValueError:
                                        print(f"Could not parse timestamp {timestamp} for {path}")

                            # Add to route data
                            self.route_data.append({
                                'latitude': lat,
                                'longitude': lon,
                                'altitude': alt,
                                'timestamp': parsed_timestamp,
                                'path': path,
                                'total_distance': total_distance
                            })

                    conn.close()

                    if self.route_data:
                        # Sort route data by timestamp if timestamps are available
                        valid_timestamps = [point for point in self.route_data if point['timestamp'] is not None]
                        if valid_timestamps:
                            self.route_data.sort(key=lambda x: x['timestamp'] if x['timestamp'] is not None else datetime.max)
                        
                        self.timeline_slider.setMaximum(len(self.route_data) - 1)
                        self.current_index = 0
                        self.update_display()
                        QMessageBox.information(self, "Success", f"Loaded {len(self.route_data)} images into route")
                    else:
                        QMessageBox.warning(self, "Error", "No valid images with GPS data found")

                except Exception as e:
                    print(f"Error details: {str(e)}")  # Add detailed error logging
                    QMessageBox.critical(self, "Error", f"Error loading route: {str(e)}")
            except Exception as e:
                print(f"Error checking selections: {str(e)}")
                QMessageBox.critical(self, "Error", f"Error checking selected images: {str(e)}")

        def clear_selection(self):
            """Clear all selections and reset filters."""
            self.select_all_checkbox.setChecked(False)
            self.folder_combo.setCurrentText("All Folders")
            self.session_combo.setCurrentText("All Sessions")
            self.route_data = []
            self.update_display()

        def toggle_playback(self):
            """Toggle playback state."""
            self.is_playing = not self.is_playing
            self.play_button.setText("Pause" if self.is_playing else "Play")
            
            if self.is_playing:
                self.timer.start(100)  # Update every 100ms
            else:
                self.timer.stop()

        def change_speed(self, speed_text):
            """Change the playback speed."""
            self.playback_speed = float(speed_text.replace('x', ''))

        def reset_playback(self):
            """Reset playback to the beginning."""
            self.current_index = 0
            self.timeline_slider.setValue(0)
            self.update_display()

        def slider_changed(self, value):
            """Handle timeline slider value change."""
            self.current_index = value
            self.update_display()

        def update_playback(self):
            """Update playback position."""
            if self.is_playing and self.route_data:
                self.current_index = (self.current_index + 1) % len(self.route_data)
                self.timeline_slider.setValue(self.current_index)
                self.update_display()

        def update_display(self):
            """Update all display elements."""
            self.update_map()
            self.update_altitude_profile()
            self.update_statistics()

        def update_map(self):
            """Update the map with the route and current position."""
            if not self.route_data:
                print("No route data available")
                return

            try:
                print(f"Updating map with {len(self.route_data)} points, current index: {self.current_index}")
                
                # If map already exists, just update the marker position
                if hasattr(self, 'map_created') and self.map_created:
                    print("Map exists, updating marker position")
                    current = self.route_data[self.current_index]
                    # Handle timestamp parsing
                    try:
                        if isinstance(current['timestamp'], str):
                            # Try to parse various timestamp formats
                            for fmt in ['%Y-%b-%d %H:%M:%S.%f', '%Y:%m:%d %H:%M:%S']:
                                try:
                                    ts = datetime.strptime(current['timestamp'], fmt).strftime('%H:%M:%S')
                                    break
                                except ValueError:
                                    continue
                            else:
                                ts = '--:--:--'
                        elif isinstance(current['timestamp'], datetime):
                            ts = current['timestamp'].strftime('%H:%M:%S')
                        else:
                            ts = '--:--:--'
                    except Exception as e:
                        print(f"Error parsing timestamp: {e}")
                        ts = '--:--:--'

                    current_alt = current['altitude'] if current['altitude'] is not None else 'N/A'
                    js_call = (
                        f"updateMarkerPosition("
                        f"{current['latitude']}, {current['longitude']}, "
                        f"{current_alt}, '{ts}'"
                        f");"
                    )
                    print(f"Executing JavaScript: {js_call}")
                    self.map_widget.page().runJavaScript(js_call)
                    return

                print("Creating new map")
                # First time creation - build the map
                m = folium.Map(
                    location=[self.route_data[0]['latitude'], self.route_data[0]['longitude']],
                    zoom_start=12
                )

                # Add the route line
                coordinates = [(point['latitude'], point['longitude']) for point in self.route_data]
                print(f"Adding polyline with {len(coordinates)} coordinates")
                folium.PolyLine(
                    coordinates,
                    weight=3,
                    color='blue',
                    opacity=0.8
                ).add_to(m)

                # Get the first point data and map variable name
                first = self.route_data[self.current_index]
                print(f"First point data: {first}")
                
                # Handle timestamp parsing for first point
                try:
                    if isinstance(first['timestamp'], str):
                        # Try to parse various timestamp formats
                        for fmt in ['%Y-%b-%d %H:%M:%S.%f', '%Y:%m:%d %H:%M:%S']:
                            try:
                                ts = datetime.strptime(first['timestamp'], fmt).strftime('%H:%M:%S')
                                break
                            except ValueError:
                                continue
                        else:
                            ts = '--:--:--'
                    elif isinstance(first['timestamp'], datetime):
                        ts = first['timestamp'].strftime('%H:%M:%S')
                    else:
                        ts = '--:--:--'
                except Exception as e:
                    print(f"Error parsing timestamp: {e}")
                    ts = '--:--:--'

                first_alt_text = f"{first['altitude']}m" if first['altitude'] is not None else 'N/A'
                map_var = m.get_name()
                print(f"Map variable name: {map_var}")

                # Create marker and define updater function - wait for map to be ready
                js = f"""
                <script>
                    // Wait for map to be initialized
                    var waitForMap = function(callback) {{
                        if (typeof {map_var} !== 'undefined') {{
                            callback();
                        }} else {{
                            setTimeout(function() {{ waitForMap(callback); }}, 100);
                        }}
                    }};

                    waitForMap(function() {{
                        console.log('Map ready, initializing marker...');
                        // Create the marker once and bind its popup
                        window.currentMarker = L.marker(
                            [{first['latitude']}, {first['longitude']}],
                            {{ icon: L.icon({{ iconUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png' }}) }}
                        )
                            .addTo({map_var})
                            .bindPopup(`Altitude: {first_alt_text}<br>Time: {ts}`);
                        console.log('Marker created:', window.currentMarker);
                    }});

                    // Define our updater
                    function updateMarkerPosition(lat, lng, altitude, timestamp) {{
                        console.log('Updating marker position:', lat, lng, altitude, timestamp);
                        if (window.currentMarker) {{
                            var altText = altitude === 'N/A' ? 'N/A' : altitude + 'm';
                            window.currentMarker.setLatLng([lat, lng])
                                .setPopupContent(`Altitude: ${altText}<br>Time: ${timestamp}`);
                        }}
                    }}
                </script>
                """
                print("Adding JavaScript to map")
                m.get_root().html.add_child(folium.Element(js))

                # Save map to a temporary HTML file
                import tempfile
                import os

                temp_dir = tempfile.gettempdir()
                self.map_file = os.path.join(temp_dir, 'route_map.html')
                print(f"Saving map to: {self.map_file}")
                m.save(self.map_file)

                # Load the map file with the correct security settings
                print("Setting up QWebEngineView settings")
                self.map_widget.settings().setAttribute(
                    self.map_widget.settings().WebAttribute.LocalContentCanAccessRemoteUrls, 
                    True
                )
                self.map_widget.settings().setAttribute(
                    self.map_widget.settings().WebAttribute.LocalContentCanAccessFileUrls, 
                    True
                )
                
                # Load the file using the file:// protocol
                print("Loading map file")
                self.map_widget.load(QUrl.fromLocalFile(self.map_file))
                # Set map_created flag immediately
                self.map_created = True
                print("Map creation complete")

            except Exception as e:
                print(f"Error updating map: {str(e)}")
                print("Full error details:")
                traceback.print_exc()

        def update_altitude_profile(self):
            """Update the altitude profile graph."""
            if not self.route_data:
                return

            try:
                # Check if profile is already created and just update marker position
                if hasattr(self, 'profile_created') and self.profile_created:
                    # Update marker position using JavaScript
                    current = self.route_data[self.current_index]
                    current_alt = current['altitude'] if current['altitude'] is not None else 0
                    self.altitude_widget.page().runJavaScript(f"""
                        if (typeof Plotly !== 'undefined') {{
                            var update = {{
                                x: [[{current['total_distance']}]],
                                y: [[{current_alt}]]
                            }};

                            // Update the second trace (index 1) which is our marker
                            Plotly.update('altitude-plot', update, {{}}, [1]);
                        }}
                    """)
                    return

                # Create distance and altitude arrays
                distances = [point['total_distance'] for point in self.route_data]
                altitudes = [point['altitude'] if point['altitude'] is not None else 0 for point in self.route_data]

                # Create the plot with a specific div ID
                fig = go.Figure()
                
                # Add altitude profile
                fig.add_trace(go.Scatter(
                    x=distances,
                    y=altitudes,
                    mode='lines',
                    name='Altitude',
                    line=dict(color='blue')
                ))

                # Add current position marker
                current = self.route_data[self.current_index]
                fig.add_trace(go.Scatter(
                    x=[current['total_distance']],
                    y=[current['altitude']],
                    mode='markers',
                    marker=dict(color='red', size=10),
                    name='Current Position'
                ))

                # Update layout
                fig.update_layout(
                    title='Altitude Profile',
                    xaxis_title='Distance (km)',
                    yaxis_title='Altitude (m)',
                    showlegend=False,
                    margin=dict(l=0, r=0, t=30, b=0)
                )

                # Save to temporary HTML file with specific div ID
                import tempfile
                import os

                temp_dir = tempfile.gettempdir()
                profile_file = os.path.join(temp_dir, 'altitude_profile.html')
                
                with open(profile_file, 'w', encoding='utf-8') as f:
                    f.write(fig.to_html(include_plotlyjs='cdn', full_html=True, div_id='altitude-plot'))

                # Load the profile with correct security settings
                self.altitude_widget.settings().setAttribute(
                    self.altitude_widget.settings().WebAttribute.LocalContentCanAccessRemoteUrls, 
                    True
                )
                self.altitude_widget.settings().setAttribute(
                    self.altitude_widget.settings().WebAttribute.LocalContentCanAccessFileUrls, 
                    True
                )
                
                # Load the file using the file:// protocol
                self.altitude_widget.load(QUrl.fromLocalFile(profile_file))
                # Set profile_created flag immediately
                self.profile_created = True

            except Exception as e:
                print(f"Error updating altitude profile: {str(e)}")

        def update_statistics(self):
            """Update the statistics labels."""
            if not self.route_data:
                return

            current = self.route_data[self.current_index]
            
            # Calculate total distance
            total_distance = self.route_data[-1]['total_distance']
            
            # Calculate elevation gain
            elevation_gain = 0
            for i in range(1, len(self.route_data)):
                alt_i = self.route_data[i]['altitude'] or 0
                prev_alt = self.route_data[i-1]['altitude'] or 0
                diff = alt_i - prev_alt
                if diff > 0:
                    elevation_gain += diff

            self.total_distance_label.setText(f"Total Distance: {total_distance:.1f} km")
            self.elevation_gain_label.setText(f"Elevation Gain: {elevation_gain:.1f} m")
            if current['altitude'] is None:
                self.current_altitude_label.setText("Current Altitude: N/A")
            else:
                self.current_altitude_label.setText(f"Current Altitude: {current['altitude']:.1f} m")
            
            # Handle timestamp display
            if current['timestamp']:
                time_str = current['timestamp'].strftime('%H:%M:%S')
            else:
                time_str = "--:--:--"
            self.current_time_label.setText(f"Current Time: {time_str}")

        def toggle_all_selections(self, state):
            """Toggle all checkboxes based on the select all checkbox state."""
            try:
                for row in range(self.files_list.rowCount()):
                    checkbox_widget = self.files_list.cellWidget(row, 0)
                    if checkbox_widget:
                        checkbox = checkbox_widget.findChild(QCheckBox)
                        if checkbox:
                            checkbox.setChecked(bool(state))
            except Exception as e:
                print(f"Error in toggle_all_selections: {str(e)}")

    class ImagePreviewDialog(QDialog):
        def __init__(self, image_path, all_images, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Image Preview")
            self.setModal(True)
            
            # Store image paths and current index
            self.all_images = all_images
            self.current_index = all_images.index(image_path)
            
            # Get screen size
            screen = QApplication.primaryScreen().geometry()
            self.screen_width = screen.width() * 0.8  # Use 80% of screen width
            self.screen_height = screen.height() * 0.8  # Use 80% of screen height
            
            # Set initial dialog size
            self.resize(int(self.screen_width), int(self.screen_height))
            
            # Create main layout
            layout = QVBoxLayout(self)
            
            # Create scroll area
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            layout.addWidget(scroll)
            
            # Create container widget for the image
            container = QWidget()
            container_layout = QVBoxLayout(container)
            
            # Create label for image
            self.image_label = QLabel()
            self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            container_layout.addWidget(self.image_label)
            
            # Set the container as the scroll area widget
            scroll.setWidget(container)
            
            # Add navigation and info panel
            nav_layout = QHBoxLayout()
            
            # Previous button
            self.prev_button = QPushButton("← Previous")
            self.prev_button.clicked.connect(self.show_previous)
            self.prev_button.setEnabled(self.current_index > 0)
            nav_layout.addWidget(self.prev_button)
            
            # Image counter
            self.counter_label = QLabel()
            self.counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            nav_layout.addWidget(self.counter_label)
            
            # Next button
            self.next_button = QPushButton("Next →")
            self.next_button.clicked.connect(self.show_next)
            self.next_button.setEnabled(self.current_index < len(self.all_images) - 1)
            nav_layout.addWidget(self.next_button)
            
            layout.addLayout(nav_layout)
            
            # Add file info label
            self.info_label = QLabel()
            self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self.info_label)
            
            # Add button layout
            button_layout = QHBoxLayout()
            
            # Add save button
            save_button = QPushButton("Save Full Resolution Image")
            save_button.clicked.connect(self.save_full_resolution)
            save_button.setMaximumWidth(200)  # Limit button width
            button_layout.addWidget(save_button)
            
            # Add close button
            close_button = QPushButton("Close")
            close_button.clicked.connect(self.close)
            close_button.setMaximumWidth(200)  # Limit button width
            button_layout.addWidget(close_button)
            
            # Add button layout centered
            layout.addLayout(button_layout)
            
            # Load initial image
            self.load_current_image()
            
            # Center dialog on screen
            self.center_on_screen()
            
            # Set up keyboard shortcuts
            QShortcut(Qt.Key.Key_Left, self, self.show_previous)
            QShortcut(Qt.Key.Key_Right, self, self.show_next)
            QShortcut(Qt.Key.Key_Escape, self, self.close)
        
        def save_full_resolution(self):
            """Save the current image at full resolution."""
            try:
                current_image_path = self.all_images[self.current_index]
                file_name = os.path.basename(current_image_path)
                
                # Open file dialog to choose save location
                save_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save Full Resolution Image",
                    file_name,
                    "Images (*.jpg *.jpeg *.png *.tif *.tiff);;All Files (*.*)"
                )
                
                if save_path:
                    # Copy the original file to the new location
                    shutil.copy2(current_image_path, save_path)
                    QMessageBox.information(self, "Success", "Image saved successfully!")
                    
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save image: {str(e)}")
        
        def load_current_image(self):
            """Load and display the current image."""
            try:
                image_path = self.all_images[self.current_index]
                
                # Load the image
                pixmap = QPixmap(image_path)
                if pixmap.isNull():
                    raise Exception("Failed to load image")
                
                # Calculate scaling factor to fit screen while maintaining aspect ratio
                scale_width = self.screen_width / pixmap.width()
                scale_height = self.screen_height / pixmap.height()
                scale = min(scale_width, scale_height)
                
                if scale < 1:  # Only scale down, never up
                    new_width = int(pixmap.width() * scale)
                    new_height = int(pixmap.height() * scale)
                    pixmap = pixmap.scaled(
                        new_width,
                        new_height,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation
                    )
                
                # Set the pixmap to the label
                self.image_label.setPixmap(pixmap)
                
                # Update counter and info
                self.counter_label.setText(f"Image {self.current_index + 1} of {len(self.all_images)}")
                
                # Build info text with file info and tags
                info_text = [
                    f"File: {os.path.basename(image_path)}",
                    f"Size: {pixmap.width()}x{pixmap.height()} pixels"
                ]
                
                # Add tags if available
                try:
                    parent = self.parent()
                    if hasattr(parent, 'db_path') and parent.db_path.text() != "Not selected":
                        with sqlite3.connect(parent.db_path.text()) as conn:
                            cursor = conn.cursor()
                            # Get all tag columns
                            cursor.execute("PRAGMA table_info(images)")
                            tag_columns = [row[1] for row in cursor.fetchall() if row[1].startswith('Tag_')]
                            
                            if tag_columns:
                                # Get tag values for this image
                                query = f'SELECT {", ".join(tag_columns)} FROM images WHERE path = ?'
                                cursor.execute(query, (image_path,))
                                row = cursor.fetchone()
                                
                                if row:
                                    tags = []
                                    for col, value in zip(tag_columns, row):
                                        if value:  # Only show non-empty tags
                                            tag_name = col[4:].replace('_', ' ').title()  # Remove 'Tag_' prefix and format
                                            tags.append(f"{tag_name}: {value}")
                                    
                                    if tags:
                                        info_text.append("\nTags:")
                                        info_text.extend(tags)
                except Exception as e:
                    print(f"Error loading tags for full image view: {str(e)}")
                
                self.info_label.setText("\n".join(info_text))
                
                # Update button states
                self.prev_button.setEnabled(self.current_index > 0)
                self.next_button.setEnabled(self.current_index < len(self.all_images) - 1)
                
            except Exception as e:
                error_msg = f"Error loading image: {str(e)}"
                self.image_label.setText(error_msg)
                self.info_label.setText(error_msg)
                print(error_msg)  # Print to console for debugging
        
        def show_previous(self):
            """Show the previous image in the list."""
            if self.current_index > 0:
                self.current_index -= 1
                self.load_current_image()
        
        def show_next(self):
            """Show the next image in the list."""
            if self.current_index < len(self.all_images) - 1:
                self.current_index += 1
                self.load_current_image()
        
        def center_on_screen(self):
            """Center the dialog on the screen."""
            screen = QApplication.primaryScreen().geometry()
            size = self.geometry()
            x = (screen.width() - size.width()) // 2
            y = (screen.height() - size.height()) // 2
            self.move(x, y)

    class ThumbnailWidget(QWidget):
        clicked = pyqtSignal(str)  # Signal to emit the image path when clicked
        selection_changed = pyqtSignal(str, bool)  # Signal for checkbox changes (path, checked)
        _thumbnail_cache = {}  # Class-level cache for thumbnails
        _max_cache_size = 100  # Maximum number of thumbnails to keep in cache
        
        def __init__(self, image_path, distance, altitude, folder_session, parent=None):
            super().__init__(parent)
            self.image_path = image_path
            self.setFixedSize(200, 250)  # Fixed size for thumbnail widget
            
            layout = QVBoxLayout(self)
            layout.setContentsMargins(5, 5, 5, 5)
            
            # Add checkbox at the top
            self.checkbox = QCheckBox()
            self.checkbox.stateChanged.connect(self.on_selection_changed)
            checkbox_layout = QHBoxLayout()
            checkbox_layout.addWidget(self.checkbox)
            checkbox_layout.addStretch()
            layout.addLayout(checkbox_layout)
            
            # Create image label
            self.image_label = QLabel()
            self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.image_label.setFixedSize(180, 180)
            layout.addWidget(self.image_label)
            
            # Load and display thumbnail using cache
            try:
                # Check cache first
                if self.image_path in self._thumbnail_cache:
                    self.image_label.setPixmap(self._thumbnail_cache[self.image_path])
                else:
                    # Load image using Qt
                    img = QImage(image_path)
                    if img.isNull():
                        raise Exception("Failed to load image")
                    
                    # Scale image to thumbnail size using fast transformation
                    scaled_img = img.scaled(180, 180, 
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.FastTransformation)
                    
                    # Convert to pixmap
                    pixmap = QPixmap.fromImage(scaled_img)
                    
                    # Cache the thumbnail
                    if len(self._thumbnail_cache) >= self._max_cache_size:
                        # Remove oldest item if cache is full
                        self._thumbnail_cache.pop(next(iter(self._thumbnail_cache)))
                    self._thumbnail_cache[self.image_path] = pixmap
                    
                    # Display the thumbnail
                    self.image_label.setPixmap(pixmap)
            except Exception as e:
                print(f"Error loading thumbnail for {image_path}: {str(e)}")
                self.image_label.setText("Error loading\nthumbnail")
            
            # Add info labels
            info_layout = QVBoxLayout()
            info_layout.setSpacing(2)
            
            # File name (shortened)
            file_name = os.path.basename(image_path)
            if len(file_name) > 20:
                file_name = file_name[:17] + "..."
            name_label = QLabel(file_name)
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info_layout.addWidget(name_label)
            
            # Distance and altitude
            altitude_str = f"{altitude:.1f}m" if altitude is not None else "N/A"
            dist_alt_label = QLabel(f"Dist: {distance:.1f}m, Alt: {altitude_str}")
            dist_alt_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info_layout.addWidget(dist_alt_label)
            
            # Folder/Session (shortened)
            if len(folder_session) > 20:
                folder_session = folder_session[:17] + "..."
            folder_label = QLabel(folder_session)
            folder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info_layout.addWidget(folder_label)
            
            # Add tags if available
            try:
                if hasattr(parent, 'db_path') and parent.db_path.text() != "Not selected":
                    with sqlite3.connect(parent.db_path.text()) as conn:
                        cursor = conn.cursor()
                        # Get all tag columns
                        cursor.execute("PRAGMA table_info(images)")
                        tag_columns = [row[1] for row in cursor.fetchall() if row[1].startswith('Tag_')]
                        
                        if tag_columns:
                            # Get tag values for this image
                            query = f'SELECT {", ".join(tag_columns)} FROM images WHERE path = ?'
                            cursor.execute(query, (image_path,))
                            row = cursor.fetchone()
                            
                            if row:
                                tags = []
                                for col, value in zip(tag_columns, row):
                                    if value:  # Only show non-empty tags
                                        tag_name = col[4:].replace('_', ' ').title()  # Remove 'Tag_' prefix and format
                                        tags.append(f"{tag_name}: {value}")
                                
                                if tags:
                                    # Show up to 2 tags in thumbnail
                                    visible_tags = tags[:2]
                                    if len(tags) > 2:
                                        visible_tags.append("...")
                                    
                                    tag_label = QLabel("\n".join(visible_tags))
                                    tag_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                                    tag_label.setStyleSheet("color: #666; font-size: 10px;")
                                    info_layout.addWidget(tag_label)
            except Exception as e:
                print(f"Error loading tags for thumbnail: {str(e)}")
            
            layout.addLayout(info_layout)
        
        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                # Only emit click if not clicking the checkbox
                if not self.checkbox.geometry().contains(event.pos()):
                    self.clicked.emit(self.image_path)
            super().mousePressEvent(event)
        
        def enterEvent(self, event):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            super().enterEvent(event)
        
        def leaveEvent(self, event):
            self.setCursor(Qt.CursorShape.ArrowCursor)
            super().leaveEvent(event)
        
        def on_selection_changed(self, state):
            self.selection_changed.emit(self.image_path, bool(state))
        
        def is_selected(self):
            return self.checkbox.isChecked()
        
        def set_selected(self, selected):
            self.checkbox.setChecked(selected)

    class ThumbnailLoaderWorker(QThread):
        """Worker thread for loading thumbnails asynchronously."""
        thumbnail_ready = pyqtSignal(str, int, int)  # image_path, row, col
        finished = pyqtSignal()

        def __init__(self, image_files, num_columns):
            super().__init__()
            self.image_files = image_files
            self.num_columns = num_columns
            self._stop = False

        def run(self):
            current_row = 0
            current_col = 0

            for img_path in self.image_files:
                if self._stop:
                    break
                    
                self.thumbnail_ready.emit(img_path, current_row, current_col)
                
                current_col += 1
                if current_col >= self.num_columns:
                    current_col = 0
                    current_row += 1

            self.finished.emit()

        def stop(self):
            self._stop = True

    class TagConfigTab(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setup_ui()
            self.current_folder = None
            self.image_files = []
            self.selected_images = set()
            self.root_folder = None
            self.thumbnail_loader = None
            self.loading_label = None
            self.page_size = 20  # Number of thumbnails to load per page
            self.current_page = 0
            self.total_images = 0
            
            # Load initial paths from config
            config = load_config()
            if config["image_directory"] and os.path.exists(config["image_directory"]):
                self.root_folder = config["image_directory"]
                self.root_path.setText(config["image_directory"])
                self.populate_folder_tree(config["image_directory"])
            
            if config["database_path"] and os.path.exists(config["database_path"]):
                self.db_path.setText(config["database_path"])

        def setup_ui(self):
            """Setup the tag configuration tab UI."""
            layout = QHBoxLayout(self)

            # Create main horizontal splitter
            main_splitter = QSplitter(Qt.Orientation.Horizontal)
            layout.addWidget(main_splitter)

            # Left panel - Explorer view
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)

            # Root folder selection
            root_layout = QHBoxLayout()
            root_label = QLabel("Root Folder:")
            self.root_path = QLabel("Not selected")
            root_btn = QPushButton("Browse")
            root_btn.clicked.connect(self.select_root_folder)
            root_layout.addWidget(root_label)
            root_layout.addWidget(self.root_path)
            root_layout.addWidget(root_btn)
            left_layout.addLayout(root_layout)

            # Folder tree view using QStandardItemModel instead
            self.folder_tree = QTreeView()
            self.folder_model = QStandardItemModel()
            self.folder_model.setHorizontalHeaderLabels(['Folders'])
            self.folder_tree.setModel(self.folder_model)
            self.folder_tree.clicked.connect(self.on_folder_selected)
            left_layout.addWidget(self.folder_tree)

            # Add database selection
            db_layout = QHBoxLayout()
            db_label = QLabel("Database:")
            self.db_path = QLabel("Not selected")
            db_btn = QPushButton("Browse")
            db_btn.clicked.connect(self.select_database)
            db_layout.addWidget(db_label)
            db_layout.addWidget(self.db_path)
            db_layout.addWidget(db_btn)
            left_layout.addLayout(db_layout)

            main_splitter.addWidget(left_panel)

            # Right panel with vertical splitter
            right_splitter = QSplitter(Qt.Orientation.Vertical)
            main_splitter.addWidget(right_splitter)

            # Upper right panel - Thumbnails
            upper_right_panel = QWidget()
            upper_right_layout = QVBoxLayout(upper_right_panel)

            # Current folder label and pagination info
            folder_info_layout = QHBoxLayout()
            self.current_folder_label = QLabel("No folder selected")
            self.current_folder_label.setStyleSheet("font-weight: bold;")
            self.pagination_label = QLabel("")
            folder_info_layout.addWidget(self.current_folder_label)
            folder_info_layout.addWidget(self.pagination_label)
            upper_right_layout.addLayout(folder_info_layout)

            # Selection counter
            self.selection_counter = QLabel("Selected: 0 images")
            upper_right_layout.addWidget(self.selection_counter)

            # Thumbnail section
            self.thumbnail_scroll = QScrollArea()
            self.thumbnail_scroll.setWidgetResizable(True)
            self.thumbnail_container = QWidget()
            self.thumbnail_layout = QGridLayout(self.thumbnail_container)
            self.thumbnail_scroll.setWidget(self.thumbnail_container)
            self.thumbnail_scroll.verticalScrollBar().valueChanged.connect(self._handle_scroll)
            upper_right_layout.addWidget(QLabel("Images in Selected Folder:"))
            upper_right_layout.addWidget(self.thumbnail_scroll)

            # Pagination controls
            pagination_layout = QHBoxLayout()
            self.prev_page_btn = QPushButton("Previous")
            self.prev_page_btn.clicked.connect(self._load_previous_page)
            self.next_page_btn = QPushButton("Next")
            self.next_page_btn.clicked.connect(self._load_next_page)
            pagination_layout.addWidget(self.prev_page_btn)
            pagination_layout.addWidget(self.next_page_btn)
            upper_right_layout.addLayout(pagination_layout)

            right_splitter.addWidget(upper_right_panel)

            # Lower right panel - Tag Configuration
            tag_group = QGroupBox("Tag Configuration")
            tag_layout = QVBoxLayout()

            # Add tabs for different tag operations
            tag_tabs = QTabWidget()
            
            # Tab for folder-level tags
            folder_tag_widget = QWidget()
            folder_tag_layout = QVBoxLayout()

            # Existing tags list with editing enabled
            folder_tag_layout.addWidget(QLabel("Existing Folder Tags:"))
            self.tag_list = QTableWidget()
            self.tag_list.setColumnCount(2)
            self.tag_list.setHorizontalHeaderLabels(["Tag Name", "Value"])
            self.tag_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.tag_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.tag_list.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | 
                                        QTableWidget.EditTrigger.EditKeyPressed)
            self.tag_list.itemChanged.connect(self.on_tag_edited)
            folder_tag_layout.addWidget(self.tag_list)

            # Delete tag button
            self.delete_tag_btn = QPushButton("Delete Selected Tag")
            self.delete_tag_btn.clicked.connect(self.delete_selected_tag)
            self.delete_tag_btn.setEnabled(False)
            folder_tag_layout.addWidget(self.delete_tag_btn)

            # Enable delete button when selection changes
            self.tag_list.itemSelectionChanged.connect(self.on_tag_selection_changed)

            # Tag input fields for folder
            folder_input_layout = QFormLayout()
            self.tag_name = QLineEdit()
            self.tag_value = QLineEdit()
            folder_input_layout.addRow("Tag Name:", self.tag_name)
            folder_input_layout.addRow("Tag Value:", self.tag_value)
            folder_tag_layout.addLayout(folder_input_layout)

            # Save button for folder tags
            self.save_btn = QPushButton("Save Tag Config")
            self.save_btn.clicked.connect(self.save_tag_config)
            folder_tag_layout.addWidget(self.save_btn)

            # Index button for folder tags
            self.index_btn = QPushButton("Index Folder")
            self.index_btn.clicked.connect(self.index_folder)
            folder_tag_layout.addWidget(self.index_btn)

            folder_tag_widget.setLayout(folder_tag_layout)
            tag_tabs.addTab(folder_tag_widget, "Folder Tags")

            # Tab for image-specific tags
            image_tag_widget = QWidget()
            image_tag_layout = QVBoxLayout()

            # Tag input fields for selected images
            image_tag_layout.addWidget(QLabel("Apply Tags to Selected Images:"))
            image_input_layout = QFormLayout()
            self.image_tag_name = QLineEdit()
            self.image_tag_value = QLineEdit()
            image_input_layout.addRow("Tag Name:", self.image_tag_name)
            image_input_layout.addRow("Tag Value:", self.image_tag_value)
            image_tag_layout.addLayout(image_input_layout)

            # Apply button for image tags
            self.apply_image_tag_btn = QPushButton("Apply Tag to Selected Images")
            self.apply_image_tag_btn.clicked.connect(self.apply_tag_to_selected_images)
            self.apply_image_tag_btn.setEnabled(False)
            image_tag_layout.addWidget(self.apply_image_tag_btn)

            image_tag_widget.setLayout(image_tag_layout)
            tag_tabs.addTab(image_tag_widget, "Image Tags")

            tag_layout.addWidget(tag_tabs)

            tag_group.setLayout(tag_layout)
            right_splitter.addWidget(tag_group)

            # Set initial splitter sizes
            main_splitter.setSizes([300, 700])  # Left panel 300px, Right panel 700px
            right_splitter.setSizes([400, 300])  # Upper panel 400px, Lower panel 300px

        def on_thumbnail_selection_changed(self, image_path, is_selected):
            """Handle thumbnail selection changes."""
            if is_selected:
                self.selected_images.add(image_path)
            else:
                self.selected_images.discard(image_path)
            
            # Update selection counter and button state
            self.selection_counter.setText(f"Selected: {len(self.selected_images)} images")
            self.apply_image_tag_btn.setEnabled(len(self.selected_images) > 0)

        def select_database(self):
            """Select database file."""
            # Load last database path from config
            config = load_config()
            start_dir = os.path.dirname(config["database_path"]) if config["database_path"] else ""
            
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Database File",
                start_dir,
                "SQLite Database (*.db)"
            )
            if file_path:
                self.db_path.setText(file_path)
                # Save to config
                update_config("database_path", file_path)

        def select_root_folder(self):
            """Open dialog to select root folder."""
            # Load last directory from config
            config = load_config()
            start_dir = config["image_directory"] if config["image_directory"] else ""
            
            folder = QFileDialog.getExistingDirectory(self, "Select Root Folder", start_dir)
            if folder:
                self.root_folder = folder
                self.root_path.setText(folder)
                self.populate_folder_tree(folder)
                # Save to config
                update_config("image_directory", folder)

        def populate_folder_tree(self, path):
            """Populate the folder tree with the directory structure."""
            self.folder_model.clear()
            self.folder_model.setHorizontalHeaderLabels(['Folders'])
            root_item = QStandardItem(os.path.basename(path))
            root_item.setData(path, Qt.ItemDataRole.UserRole)
            self.folder_model.appendRow(root_item)
            self._add_folders(root_item, path)
            self.folder_tree.expandAll()

        def _add_folders(self, parent_item, parent_path):
            """Recursively add folders to the tree."""
            try:
                for entry in os.scandir(parent_path):
                    if entry.is_dir() and not entry.name.startswith('.'):
                        child_item = QStandardItem(entry.name)
                        child_item.setData(entry.path, Qt.ItemDataRole.UserRole)
                        parent_item.appendRow(child_item)
                        self._add_folders(child_item, entry.path)
            except PermissionError:
                # Skip folders we don't have permission to access
                pass

        def on_folder_selected(self, index):
            """Handle folder selection in the tree view."""
            item = self.folder_model.itemFromIndex(index)
            if item:
                folder_path = item.data(Qt.ItemDataRole.UserRole)
                self.current_folder = folder_path
                self.current_folder_label.setText(f"Current Folder: {folder_path}")
                self.load_folder_images()
                self.load_existing_tags()

        def load_folder_images(self):
            """Load images from the selected folder."""
            if not self.current_folder:
                return

            # Clear existing thumbnails and selections
            for i in reversed(range(self.thumbnail_layout.count())):
                self.thumbnail_layout.itemAt(i).widget().setParent(None)
            
            # Clear selections when loading a new folder
            self.selected_images.clear()
            self.selection_counter.setText("Selected: 0 images")
            self.apply_image_tag_btn.setEnabled(False)

            # Reset pagination
            self.current_page = 0

            # Get all image files in the folder
            self.image_files = []
            for file in os.listdir(self.current_folder):
                if file.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
                    self.image_files.append(os.path.join(self.current_folder, file))

            self.total_images = len(self.image_files)
            if not self.total_images:
                self.pagination_label.setText("No images found")
                self.prev_page_btn.setEnabled(False)
                self.next_page_btn.setEnabled(False)
                return

            self._load_current_page()

        def _load_current_page(self):
            """Load the current page of thumbnails."""
            # Clear existing thumbnails
            for i in reversed(range(self.thumbnail_layout.count())):
                widget = self.thumbnail_layout.itemAt(i).widget()
                if widget:
                    widget.setParent(None)

            # Update selection counter
            self.selection_counter.setText(f"Selected: {len(self.selected_images)} images")
            self.apply_image_tag_btn.setEnabled(len(self.selected_images) > 0)

            # Stop any existing thumbnail loader
            if self.thumbnail_loader and self.thumbnail_loader.isRunning():
                self.thumbnail_loader.stop()
                self.thumbnail_loader.wait()

            # Calculate page bounds
            start_idx = self.current_page * self.page_size
            end_idx = min(start_idx + self.page_size, self.total_images)
            current_page_files = self.image_files[start_idx:end_idx]

            # Update pagination info
            total_pages = (self.total_images + self.page_size - 1) // self.page_size
            self.pagination_label.setText(f"Page {self.current_page + 1} of {total_pages} ({self.total_images} images)")
            self.prev_page_btn.setEnabled(self.current_page > 0)
            self.next_page_btn.setEnabled(self.current_page < total_pages - 1)

            # Calculate number of columns
            num_columns = max(1, self.thumbnail_container.width() // 220)

            # Start thumbnail loader for current page
            self.thumbnail_loader = ThumbnailLoaderWorker(current_page_files, num_columns)
            self.thumbnail_loader.thumbnail_ready.connect(self._add_thumbnail)
            self.thumbnail_loader.finished.connect(self._loading_finished)
            self.thumbnail_loader.start()

        def _load_next_page(self):
            """Load the next page of thumbnails."""
            total_pages = (self.total_images + self.page_size - 1) // self.page_size
            if self.current_page < total_pages - 1:
                self.current_page += 1
                self._load_current_page()

        def _load_previous_page(self):
            """Load the previous page of thumbnails."""
            if self.current_page > 0:
                self.current_page -= 1
                self._load_current_page()

        def _handle_scroll(self, value):
            """Handle scroll events to implement infinite scrolling."""
            # Prevent multiple scroll handlers from running at once
            if hasattr(self, '_is_loading_next_page') and self._is_loading_next_page:
                return
                
            scrollbar = self.thumbnail_scroll.verticalScrollBar()
            # If we're near the bottom and there are more pages, load the next page
            if value >= scrollbar.maximum() - 100:  # Start loading earlier with smaller page size
                total_pages = (self.total_images + self.page_size - 1) // self.page_size
                if self.current_page < total_pages - 1:
                    self._is_loading_next_page = True
                    self._load_next_page()
                    self._is_loading_next_page = False

        def _add_thumbnail(self, img_path, row, col):
            """Add a single thumbnail to the grid."""
            if self.loading_label and self.loading_label.parent():
                self.loading_label.setParent(None)

            thumbnail = ThumbnailWidget(
                img_path,
                0,  # distance not relevant here
                None,  # altitude not relevant here
                os.path.basename(self.current_folder)
            )
            thumbnail.clicked.connect(self.show_full_image)
            thumbnail.selection_changed.connect(self.on_thumbnail_selection_changed)
            
            # Restore selection state if this image was previously selected
            if img_path in self.selected_images:
                thumbnail.set_selected(True)
                
            self.thumbnail_layout.addWidget(thumbnail, row, col)

        def _loading_finished(self):
            """Handle completion of thumbnail loading."""
            if self.loading_label and self.loading_label.parent():
                self.loading_label.setParent(None)

        def show_full_image(self, image_path):
            """Show full-size image in a dialog."""
            dialog = ImagePreviewDialog(image_path, self.image_files, self)
            dialog.exec()

        def load_existing_tags(self):
            """Load and display existing tags from tags.config files in current and parent directories."""
            # Temporarily disconnect the itemChanged signal to prevent triggering edits during loading
            if hasattr(self, 'tag_list'):
                self.tag_list.itemChanged.disconnect(self.on_tag_edited)

            try:
                self.tag_list.setRowCount(0)  # Clear existing rows
                
                if not self.current_folder:
                    return

                # Start from the current folder and walk up to root
                current_dir = self.current_folder
                tags_by_level = {}  # Dictionary to store tags by their name with level info
                
                while current_dir:
                    config_path = os.path.join(current_dir, "tags.config")
                    if os.path.exists(config_path):
                        try:
                            rel_path = os.path.relpath(current_dir, self.current_folder)
                            level = "" if rel_path == "." else rel_path
                            
                            with open(config_path, 'r', encoding='utf-8') as f:
                                for line in f:
                                    line = line.strip()
                                    if line.startswith('#') and ':' in line:
                                        # Remove the leading # and split on first colon
                                        tag_line = line[1:].strip()
                                        if ':' in tag_line:
                                            tag_name, tag_value = tag_line.split(':', 1)
                                            tag_name = tag_name.strip()
                                            tag_value = tag_value.strip()
                                            
                                            if tag_name and tag_value:  # Only add if both are non-empty
                                                # Only store if we haven't seen this tag before or if it's from current directory
                                                if tag_name not in tags_by_level or level == "":
                                                    tags_by_level[tag_name] = (level, tag_value)
                        except Exception as e:
                            logger.error(f"Error reading tags from {config_path}: {str(e)}")
                    
                    # Move up one directory level
                    parent_dir = os.path.dirname(current_dir)
                    if parent_dir == current_dir:  # Reached root
                        break
                    current_dir = parent_dir

                # Add tags to the table
                # First add current directory tags
                for tag_name, (level, value) in sorted(tags_by_level.items()):
                    if level == "":
                        row = self.tag_list.rowCount()
                        self.tag_list.insertRow(row)
                        self.tag_list.setItem(row, 0, QTableWidgetItem(tag_name))
                        self.tag_list.setItem(row, 1, QTableWidgetItem(value))

                # Then add inherited tags
                for tag_name, (level, value) in sorted(tags_by_level.items()):
                    if level != "":
                        row = self.tag_list.rowCount()
                        self.tag_list.insertRow(row)
                        name_item = QTableWidgetItem(f"{tag_name} (inherited from {level})")
                        name_item.setForeground(QColor(128, 128, 128))  # Gray color for inherited tags
                        value_item = QTableWidgetItem(value)
                        value_item.setForeground(QColor(128, 128, 128))
                        self.tag_list.setItem(row, 0, name_item)
                        self.tag_list.setItem(row, 1, value_item)

            except Exception as e:
                logger.error(f"Error loading tags: {str(e)}")
                QMessageBox.warning(self, "Warning", f"Failed to load existing tags: {str(e)}")
            finally:
                # Reconnect the itemChanged signal
                if hasattr(self, 'tag_list'):
                    self.tag_list.itemChanged.connect(self.on_tag_edited)

        def save_tag_config(self):
            """Save tag configuration to the current folder."""
            if not self.current_folder:
                QMessageBox.warning(self, "Error", "Please select a folder first.")
                return

            try:
                # Get all tags from the table
                tags = []
                for row in range(self.tag_list.rowCount()):
                    name_item = self.tag_list.item(row, 0)
                    value_item = self.tag_list.item(row, 1)
                    
                    # Skip if either item is None
                    if name_item is None or value_item is None:
                        continue
                        
                    tag_name = name_item.text().strip()
                    tag_value = value_item.text().strip()
                    
                    # Only add if both name and value are non-empty
                    if tag_name and tag_value:
                        tags.append((tag_name, tag_value))

                # Save to file
                self.save_tags_to_file(tags)

                QMessageBox.information(self, "Success", "Tag configuration saved successfully!")

            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save tag configuration: {str(e)}")

            # Refresh the display
            self.load_existing_tags()

        def index_folder(self):
            """Index the current folder and apply tags to the database."""
            if not self.current_folder:
                QMessageBox.warning(self, "Error", "Please select a folder first.")
                return

            if not self.db_path.text() or self.db_path.text() == "Not selected":
                QMessageBox.warning(self, "Error", "Please select a database file first.")
                return

            try:
                # Get all image files recursively
                image_files = []
                for root, _, files in os.walk(self.current_folder):
                    for file in files:
                        if file.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
                            image_files.append(os.path.join(root, file))

                if not image_files:
                    QMessageBox.warning(self, "Warning", "No images found in the selected folder or its subdirectories.")
                    return

                # First, get existing tag values from the database
                existing_tags = {}
                try:
                    with sqlite3.connect(self.db_path.text()) as conn:
                        cursor = conn.cursor()
                        # Get all columns that start with 'Tag_'
                        cursor.execute("PRAGMA table_info(images)")
                        tag_columns = [row[1] for row in cursor.fetchall() if row[1].startswith('Tag_')]
                        
                        if tag_columns:
                            # Get existing values for all images
                            placeholders = ','.join('?' * len(image_files))
                            query = f'SELECT path, {", ".join(tag_columns)} FROM images WHERE path IN ({placeholders})'
                            cursor.execute(query, image_files)
                            for row in cursor.fetchall():
                                path = row[0]
                                existing_tags[path] = {
                                    col: value for col, value in zip(tag_columns, row[1:])
                                    if value is not None  # Only keep non-null values
                                }
                except Exception as e:
                    print(f"Warning: Could not retrieve existing tags: {e}")

                # Process tags for each image using the find_applicable_tags function
                image_tags = {}
                for image_path in image_files:
                    # Start with existing tags for this image
                    combined_tags = existing_tags.get(image_path, {}).copy()
                    
                    # Get new tags from tags.config
                    new_tags = find_applicable_tags(image_path)
                    if new_tags:
                        # Normalize new tag names
                        normalized_new_tags = {}
                        for t_name, t_value in new_tags.items():
                            if t_name.startswith('Tag_'):
                                normalized_name = 'Tag_' + t_name[4:].lower()
                            else:
                                normalized_name = t_name.lower()
                            normalized_new_tags[normalized_name] = t_value
                        
                        # Update combined tags, preserving existing values unless overwritten by new ones
                        combined_tags.update(normalized_new_tags)
                    
                    if combined_tags:
                        image_tags[image_path] = combined_tags

                if not image_tags:
                    QMessageBox.warning(self, "Warning", "No applicable tags found in tags.config files.")
                    return

                # Get unique tag names for column creation
                all_tag_names = set()
                for tags in image_tags.values():
                    all_tag_names.update(tags.keys())

                # Start the batch processing
                self.batch_worker = BatchProcessWorker(
                    image_files,
                    self.db_path.text(),
                    {},  # No field mapping needed
                    [],  # No field selection needed
                    True,  # Always append mode
                    image_tags,  # Apply combined tags to each image
                    list(all_tag_names)  # List of all tag names for column creation
                )
                self.batch_worker.finished.connect(lambda: QMessageBox.information(
                    self, 
                    "Success", 
                    f"Successfully indexed {len(image_files)} images with tags from tags.config files."
                ))
                self.batch_worker.error.connect(lambda msg: QMessageBox.critical(self, "Error", f"Indexing failed: {msg}"))
                self.batch_worker.start()

            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to index folder: {str(e)}")

        def on_tag_selection_changed(self):
            """Enable/disable delete button based on selection."""
            self.delete_tag_btn.setEnabled(len(self.tag_list.selectedItems()) > 0)

        def delete_selected_tag(self):
            """Delete the selected tag from the table and config file."""
            selected_rows = set(item.row() for item in self.tag_list.selectedItems())
            if not selected_rows:
                return

            reply = QMessageBox.question(
                self,
                "Confirm Delete",
                "Are you sure you want to delete the selected tag(s)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                try:
                    # Get all existing tags
                    tags = []
                    for row in range(self.tag_list.rowCount()):
                        if row not in selected_rows:  # Skip selected (to be deleted) rows
                            name_item = self.tag_list.item(row, 0)
                            value_item = self.tag_list.item(row, 1)
                            
                            # Skip if either item is None
                            if name_item is None or value_item is None:
                                continue
                                
                            tag_name = name_item.text().strip()
                            tag_value = value_item.text().strip()
                            
                            # Only add if both name and value are non-empty
                            if tag_name and tag_value:
                                tags.append((tag_name, tag_value))

                    # Save remaining tags back to file
                    self.save_tags_to_file(tags)

                    # Refresh the display
                    self.load_existing_tags()

                except Exception as e:
                    logger.error(f"Error deleting tag: {str(e)}")
                    QMessageBox.critical(self, "Error", f"Failed to delete tag(s): {str(e)}")

        def on_tag_edited(self, item):
            """Handle when a tag is edited in the table."""
            if not self.current_folder:
                return

            # Get the current row's tag name and value
            row = item.row()
            name_item = self.tag_list.item(row, 0)
            value_item = self.tag_list.item(row, 1)
            
            # Store the current value before disconnecting the signal
            self._last_valid_value = item.text()

            # Temporarily disconnect to prevent recursive calls
            self.tag_list.itemChanged.disconnect(self.on_tag_edited)

            try:
                # Get the edited values
                tag_name = name_item.text().strip() if name_item else ""
                tag_value = value_item.text().strip() if value_item else ""

                # Validate both fields are non-empty
                if not tag_name or not tag_value:
                    QMessageBox.warning(self, "Invalid Input", "Both tag name and value must be non-empty.")
                    # Restore previous value
                    item.setText(self._last_valid_value)
                    return

                # Validate tag name (only if tag name was edited)
                if item.column() == 0:
                    if not tag_name.replace('_', '').replace('-', '').isalnum():
                        QMessageBox.warning(self, "Invalid Tag Name",
                                         "Tag names must contain only letters, numbers, underscores, or hyphens.")
                        # Restore previous value
                        item.setText(self._last_valid_value)
                        return

                # Get all current tags
                tags = []
                for row in range(self.tag_list.rowCount()):
                    name_item = self.tag_list.item(row, 0)
                    value_item = self.tag_list.item(row, 1)
                    
                    # Skip if either item is None
                    if name_item is None or value_item is None:
                        continue
                        
                    tag_name = name_item.text().strip()
                    tag_value = value_item.text().strip()
                    
                    # Only add if both name and value are non-empty
                    if tag_name and tag_value:
                        tags.append((tag_name, tag_value))

                # Save all tags back to file
                self.save_tags_to_file(tags)

            except Exception as e:
                logger.error(f"Error editing tag: {str(e)}")
                QMessageBox.critical(self, "Error", f"Failed to save tag changes: {str(e)}")
            finally:
                # Reconnect signal
                self.tag_list.itemChanged.connect(self.on_tag_edited)

        def save_tags_to_file(self, tags):
            """Save the given tags to the tags.config file."""
            if not self.current_folder:
                return

            try:
                config_path = os.path.join(self.current_folder, "tags.config")
                with open(config_path, 'w', encoding='utf-8') as f:
                    for tag_name, tag_value in tags:
                        f.write(f"#{tag_name}: {tag_value}\n")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save tags: {str(e)}")

        def apply_tag_to_selected_images(self):
            """Apply the specified tag to all selected images in the database."""
            if not self.selected_images:
                return

            if not self.db_path.text() or self.db_path.text() == "Not selected":
                QMessageBox.warning(self, "Error", "Please select a database file first.")
                return

            tag_name = self.image_tag_name.text().strip()
            tag_value = self.image_tag_value.text().strip()

            if not tag_name or not tag_value:
                QMessageBox.warning(self, "Error", "Please enter both tag name and value.")
                return

            # Validate tag name
            if not tag_name.replace('_', '').replace('-', '').isalnum():
                QMessageBox.warning(self, "Invalid Tag Name",
                                 "Tag names must contain only letters, numbers, underscores, or hyphens.")
                return

            try:
                # Normalize tag name
                db_tag_name = f"Tag_{tag_name.lower().replace('-', '_')}"
                print(f"Applying tag: {db_tag_name} = {tag_value}")
                print(f"Selected images: {list(self.selected_images)}")

                with sqlite3.connect(self.db_path.text()) as conn:
                    cursor = conn.cursor()

                    # Ensure the tag column exists
                    cursor.execute("PRAGMA table_info(images)")
                    existing_columns = {row[1] for row in cursor.fetchall()}
                    print(f"Existing columns: {existing_columns}")
                    
                    if db_tag_name not in existing_columns:
                        print(f"Creating new column: {db_tag_name}")
                        cursor.execute(f'ALTER TABLE images ADD COLUMN [{db_tag_name}] TEXT COLLATE NOCASE')

                    # First verify the images exist in the database
                    placeholders = ','.join('?' * len(self.selected_images))
                    cursor.execute(
                        f'SELECT path FROM images WHERE path IN ({placeholders})',
                        list(self.selected_images)
                    )
                    found_paths = {row[0] for row in cursor.fetchall()}
                    print(f"Found paths in database: {found_paths}")

                    if not found_paths:
                        raise Exception("None of the selected images were found in the database. Try indexing the folder first.")

                    missing_paths = self.selected_images - found_paths
                    if missing_paths:
                        print(f"Some images not found in database: {missing_paths}")

                    # Update all found images with the new tag
                    if found_paths:
                        placeholders = ','.join('?' * len(found_paths))
                        update_query = f'UPDATE images SET [{db_tag_name}] = ? WHERE path IN ({placeholders})'
                        params = [tag_value] + list(found_paths)
                        print(f"Update query: {update_query}")
                        print(f"Parameters: {params}")
                        cursor.execute(update_query, params)

                        # Verify the update worked
                        verify_query = f'SELECT path, [{db_tag_name}] FROM images WHERE path IN ({placeholders})'
                        cursor.execute(verify_query, list(found_paths))
                        updated_records = cursor.fetchall()
                        print(f"Updated records: {updated_records}")
                        
                        updated_count = len([r for r in updated_records if r[1] == tag_value])
                        if updated_count != len(found_paths):
                            raise Exception(f"Only {updated_count} of {len(found_paths)} images were updated")

                    conn.commit()

                # Clear the input fields
                self.image_tag_name.clear()
                self.image_tag_value.clear()

                if missing_paths:
                    QMessageBox.warning(
                        self,
                        "Partial Success",
                        f"Applied tag '{tag_name}' with value '{tag_value}' to {len(found_paths)} images.\n"
                        f"Warning: {len(missing_paths)} images were not found in the database. Try indexing the folder first."
                    )
                else:
                    QMessageBox.information(
                        self,
                        "Success",
                        f"Applied tag '{tag_name}' with value '{tag_value}' to {len(found_paths)} images."
                    )

            except Exception as e:
                print(f"Error applying tag to images: {str(e)}")
                QMessageBox.critical(self, "Error", f"Failed to apply tag to images: {str(e)}")

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("EXIF Extractor Pro")
            self.setMinimumSize(MAIN_WINDOW_MIN_WIDTH, MAIN_WINDOW_MIN_HEIGHT)
            self.image_files = []
            self.current_preview_index = -1
            self.db_manager = None
            self.selected_fields = []
            self.field_mapping_config = {}
            self.load_field_mapping()

            # Create tab widget
            self.tabs = QTabWidget()
            self.setCentralWidget(self.tabs)

            # Create tabs content widgets
            self.exif_tab = QWidget()
            self.search_tab = QWidget()
            self.route_tab = RoutePlaybackWidget()
            self.tag_config_tab = TagConfigTab()  # Add new tab

            self.tabs.addTab(self.exif_tab, "EXIF Extraction")
            self.tabs.addTab(self.search_tab, "Search")
            self.tabs.addTab(self.route_tab, "Route Playback")
            self.tabs.addTab(self.tag_config_tab, "Tag Configuration")  # Add new tab

            self.setup_exif_ui()
            self.setup_search_ui()

            # Set EXIF Extraction as the default tab
            self.tabs.setCurrentWidget(self.exif_tab)
            self._is_manual_coord_update = False

        def setup_exif_ui(self):
            """Setup the EXIF extraction tab UI."""
            layout = QVBoxLayout(self.exif_tab)
            
            # Create top section
            top_section = QWidget()
            top_layout = QVBoxLayout(top_section)
            
            # Add tags.config information section
            tags_info_group = QGroupBox("Tags.config Support")
            tags_info_layout = QVBoxLayout()
            tags_info_text = QLabel(
                "Place 'tags.config' files in your directory structure to automatically tag images.\n"
                "Format: #Tag_Name: Tag_Value (e.g., #Survey_Type: Pipeline_Inspection)\n"
                "Tag names are normalized to lower case for consistent column names.\n"
                "Tags are applied hierarchically - parent directory tags affect all subdirectories."
            )
            tags_info_text.setWordWrap(True)
            tags_info_text.setStyleSheet("color: #666; font-size: 10px; padding: 5px;")
            tags_info_layout.addWidget(tags_info_text)
            tags_info_group.setLayout(tags_info_layout)
            top_layout.addWidget(tags_info_group)
            
            # Create source directory selection
            source_layout = QHBoxLayout()
            source_label = QLabel("Source Directory:")
            self.source_path = QLineEdit()
            self.source_path.setReadOnly(True)
            source_btn = QPushButton("Browse")
            source_btn.clicked.connect(self.select_source_dir)
            source_layout.addWidget(source_label)
            source_layout.addWidget(self.source_path)
            source_layout.addWidget(source_btn)
            top_layout.addLayout(source_layout)
            
            # Create database file selection
            db_layout = QHBoxLayout()
            db_label = QLabel("Database File:")
            self.db_path = QLineEdit()
            self.db_path.setReadOnly(True)
            db_btn = QPushButton("Browse")
            db_btn.clicked.connect(self.select_db_file)
            db_layout.addWidget(db_label)
            db_layout.addWidget(self.db_path)
            db_layout.addWidget(db_btn)
            top_layout.addLayout(db_layout)
            
            # Create append mode checkbox
            self.append_mode = QCheckBox("Append to existing database")
            top_layout.addWidget(self.append_mode)
            
            # Add top section to main layout
            layout.addWidget(top_section)
            
            # Create preview button
            preview_layout = QHBoxLayout()
            self.preview_btn = QPushButton("Preview First Image")
            self.preview_btn.clicked.connect(self.preview_first_image)
            self.preview_btn.setEnabled(False)
            preview_layout.addWidget(self.preview_btn)
            layout.addLayout(preview_layout)
            
            # Create table for displaying metadata
            self.table = QTableWidget()
            self.table.setColumnCount(3)
            self.table.setHorizontalHeaderLabels(["Select", "Field", "Value"])
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.table.setColumnWidth(0, 50)
            layout.addWidget(self.table)
            
            # Create process button and progress bar
            bottom_layout = QHBoxLayout()
            self.process_btn = QPushButton("Process Images")
            self.process_btn.clicked.connect(self.process_images)
            self.process_btn.setEnabled(False)
            bottom_layout.addWidget(self.process_btn)
            
            self.progress_bar = QProgressBar()
            bottom_layout.addWidget(self.progress_bar)
            layout.addLayout(bottom_layout)
            
            # Create status label
            self.status_label = QLabel()
            layout.addWidget(self.status_label)

        def setup_search_ui(self):
            """Setup the radius search tab UI."""
            layout = QVBoxLayout(self.search_tab)
            
            # Create splitter for left panel and map
            splitter = QSplitter(Qt.Orientation.Horizontal)
            layout.addWidget(splitter)
            
            # Left panel for controls
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            
            # Database selection
            db_layout = QHBoxLayout()
            db_label = QLabel("Database File:")
            self.search_db_path = QLabel("Not selected")
            db_btn = QPushButton("Browse")
            db_btn.clicked.connect(self.select_search_database)
            
            db_layout.addWidget(db_label)
            db_layout.addWidget(self.search_db_path)
            db_layout.addWidget(db_btn)
            left_layout.addLayout(db_layout)

            # Add folder and session filter dropdowns
            filter_group = QGroupBox("Image Filters")
            filter_layout = QVBoxLayout()
            
            # Folder filter
            folder_layout = QHBoxLayout()
            folder_label = QLabel("Filter by Folder:")
            self.search_folder_combo = QComboBox()
            self.search_folder_combo.addItem("All Folders")
            folder_layout.addWidget(folder_label)
            folder_layout.addWidget(self.search_folder_combo)
            filter_layout.addLayout(folder_layout)
            
            # Session filter
            session_layout = QHBoxLayout()
            session_label = QLabel("Filter by Session:")
            self.search_session_combo = QComboBox()
            self.search_session_combo.addItem("All Sessions")
            session_layout.addWidget(session_label)
            session_layout.addWidget(self.search_session_combo)
            filter_layout.addLayout(session_layout)

            filter_group.setLayout(filter_layout)
            left_layout.addWidget(filter_group)

            # Tag Filter section
            tag_filter_group = QGroupBox("Tag Filters")
            tag_filter_layout = QVBoxLayout()
            
            # Add search mode controls
            search_mode_layout = QHBoxLayout()
            self.tag_search_mode = QButtonGroup(self)
            
            self.filter_mode_radio = QRadioButton("Filter Mode")
            self.filter_mode_radio.setChecked(True)
            self.filter_mode_radio.toggled.connect(self.on_search_mode_changed)
            self.tag_search_mode.addButton(self.filter_mode_radio)
            
            self.search_mode_radio = QRadioButton("Search Mode")
            self.tag_search_mode.addButton(self.search_mode_radio)
            
            search_mode_layout.addWidget(self.filter_mode_radio)
            search_mode_layout.addWidget(self.search_mode_radio)
            search_mode_layout.addStretch()
            
            tag_filter_layout.addLayout(search_mode_layout)
            
            # Dynamic tag filter controls will be added here when database is loaded
            self.tag_filter_container = QWidget()
            self.tag_filter_layout = QVBoxLayout(self.tag_filter_container)
            
            # Add initial placeholder message
            placeholder_label = QLabel("Click 'Refresh Tag Filters' to load tag filter controls.")
            placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder_label.setStyleSheet("color: #666; font-style: italic; padding: 20px;")
            self.tag_filter_layout.addWidget(placeholder_label)
            
            self.tag_filter_scroll = QScrollArea()  # Store reference for later access
            self.tag_filter_scroll.setWidget(self.tag_filter_container)
            self.tag_filter_scroll.setFixedHeight(150)  # Limit height
            self.tag_filter_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.tag_filter_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.tag_filter_scroll.setWidgetResizable(True)  # Ensure the container can resize
            self.tag_filter_scroll.setFrameStyle(QScrollArea.Shape.StyledPanel)  # Add visible border
            self.tag_filter_scroll.setStyleSheet("QScrollArea { background-color: #f5f5f5; border: 1px solid #ccc; }")  # Light background

            # Search bar for filtering tag controls
            self.tag_search_input = QLineEdit()
            self.tag_search_input.setPlaceholderText("Search tags...")
            self.tag_search_input.textChanged.connect(self.on_tag_filter_changed)  # For filter mode
            self.tag_search_input.returnPressed.connect(self.on_tag_search_submitted)  # For search mode
            
            refresh_tags_btn = QPushButton("Refresh Tag Filters")
            refresh_tags_btn.clicked.connect(self.refresh_tag_filters)
            
            tag_filter_layout.addWidget(refresh_tags_btn)
            tag_filter_layout.addWidget(self.tag_search_input)
            tag_filter_layout.addWidget(self.tag_filter_scroll)
            tag_filter_group.setLayout(tag_filter_layout)
            left_layout.addWidget(tag_filter_group)

            # Manual Coordinate Entry
            manual_coord_group = QGroupBox("Manual Coordinate Entry")
            manual_coord_layout = QFormLayout()

            self.manual_lat_input = QLineEdit()
            self.manual_lat_input.setPlaceholderText("Enter Latitude (e.g., 40.7128 or 40° 44\' 54\" N)")
            self.manual_lat_input.editingFinished.connect(self._handle_manual_coordinate_update)
            self.lat_format_combo = QComboBox()
            self.lat_format_combo.addItems(["Decimal Degrees", "Degrees, Minutes, Seconds"])
            self.lat_format_combo.currentTextChanged.connect(self._handle_manual_coordinate_update)
            lat_input_layout = QHBoxLayout()
            lat_input_layout.addWidget(self.manual_lat_input)
            lat_input_layout.addWidget(self.lat_format_combo)
            manual_coord_layout.addRow(QLabel("Latitude:"), lat_input_layout)

            self.manual_lon_input = QLineEdit()
            self.manual_lon_input.setPlaceholderText("Enter Longitude (e.g., -74.0060 or 74° 00\' 21\" W)")
            self.manual_lon_input.editingFinished.connect(self._handle_manual_coordinate_update)
            self.lon_format_combo = QComboBox()
            self.lon_format_combo.addItems(["Decimal Degrees", "Degrees, Minutes, Seconds"])
            self.lon_format_combo.currentTextChanged.connect(self._handle_manual_coordinate_update)
            lon_input_layout = QHBoxLayout()
            lon_input_layout.addWidget(self.manual_lon_input)
            lon_input_layout.addWidget(self.lon_format_combo)
            manual_coord_layout.addRow(QLabel("Longitude:"), lon_input_layout)
            
            manual_coord_group.setLayout(manual_coord_layout)
            left_layout.addWidget(manual_coord_group)

            # Radius input with immediate update
            radius_layout = QHBoxLayout()
            radius_label = QLabel("Circle Radius (meters):")
            self.radius_input = QSpinBox()
            self.radius_input.setRange(1, 10000)
            self.radius_input.setValue(500)
            self.radius_input.valueChanged.connect(self.update_circle_radius)
            radius_layout.addWidget(radius_label)
            radius_layout.addWidget(self.radius_input)
            left_layout.addLayout(radius_layout)

            # Altitude range inputs
            altitude_group = QGroupBox("Altitude Filter (meters)")
            altitude_layout = QHBoxLayout()
            
            # Minimum altitude
            min_alt_layout = QVBoxLayout()
            min_alt_label = QLabel("Minimum:")
            self.min_alt_input = QSpinBox()
            self.min_alt_input.setRange(-1000, 10000)  # Allow negative for below sea level
            self.min_alt_input.setSpecialValueText("No min")  # Show when value is minimum
            self.min_alt_input.setValue(-1000)  # Default to no minimum
            min_alt_layout.addWidget(min_alt_label)
            min_alt_layout.addWidget(self.min_alt_input)
            
            # Maximum altitude
            max_alt_layout = QVBoxLayout()
            max_alt_label = QLabel("Maximum:")
            self.max_alt_input = QSpinBox()
            self.max_alt_input.setRange(-1000, 10000)
            self.max_alt_input.setSpecialValueText("No max")  # Show when value is minimum
            self.max_alt_input.setValue(10000)  # Default to no maximum
            max_alt_layout.addWidget(max_alt_label)
            max_alt_layout.addWidget(self.max_alt_input)
            
            altitude_layout.addLayout(min_alt_layout)
            altitude_layout.addLayout(max_alt_layout)
            altitude_group.setLayout(altitude_layout)
            left_layout.addWidget(altitude_group)

            # Search button
            self.search_btn = QPushButton("Search by Location")
            self.search_btn.clicked.connect(self.perform_radius_search)
            self.search_btn.setEnabled(False)  # Initially disabled
            left_layout.addWidget(self.search_btn)

            # Selection controls
            selection_layout = QHBoxLayout()
            self.select_all_btn = QPushButton("Select All")
            self.select_all_btn.clicked.connect(self.toggle_select_all)
            self.select_all_btn.setEnabled(False)  # Initially disabled
            
            self.save_selected_btn = QPushButton("Save Selected")
            self.save_selected_btn.clicked.connect(self.save_selected_images)
            self.save_selected_btn.setEnabled(False)  # Initially disabled
            
            selection_layout.addWidget(self.select_all_btn)
            selection_layout.addWidget(self.save_selected_btn)
            left_layout.addLayout(selection_layout)
            
            # Selection counter
            self.selection_counter = QLabel("Selected: 0 images")
            self.selection_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
            left_layout.addWidget(self.selection_counter)
            
            # Create scroll area for thumbnails
            scroll_area = QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            
            # Create widget to hold thumbnails
            self.thumbnail_container = QWidget()
            self.thumbnail_layout = QGridLayout(self.thumbnail_container)
            scroll_area.setWidget(self.thumbnail_container)
            
            # Add scroll area to left panel
            left_layout.addWidget(scroll_area)

            # Progress bar
            self.search_progress = QProgressBar()
            left_layout.addWidget(self.search_progress)

            # Add left panel to splitter
            splitter.addWidget(left_panel)
            
            # Create and add map widget
            # self.map_widget = MapWidget() # Original placement
            if not hasattr(self, 'map_widget') or self.map_widget is None:
                 # Ensure map_widget is imported from the widgets module if it's not already an instance
                from widgets.map_widget import MapWidget as ExternalMapWidget
                self.map_widget = ExternalMapWidget()

            # Connect the map selection signal to our handler
            self.map_widget.selection_made.connect(self.handle_map_selection)
            splitter.addWidget(self.map_widget)
            
            # Set splitter sizes
            splitter.setSizes([400, 600])  # Left panel 400px, Map 600px
            
            # Initialize selection tracking
            self.selected_images = set()

        def refresh_tag_filters(self):
            """Refresh the available tag filters based on the current database."""
            if self.search_db_path.text() == "Not selected":
                QMessageBox.warning(self, "Error", "Please select a database file first.")
                return

            try:
                # Clear existing tag filter controls
                print(f"DEBUG: Clearing {self.tag_filter_layout.count()} existing widgets from layout")
                for i in reversed(range(self.tag_filter_layout.count())):
                    widget = self.tag_filter_layout.itemAt(i).widget()
                    print(f"  Removing widget {i}: {type(widget).__name__}")
                    widget.setParent(None)

                with sqlite3.connect(self.search_db_path.text()) as conn:
                    cursor = conn.cursor()

                    # Get all columns that start with "Tag_"
                    cursor.execute("PRAGMA table_info(images)")
                    columns = [row[1] for row in cursor.fetchall() if row[1].startswith('Tag_')]

                    if not columns:
                        info_label = QLabel("No tag columns found in database.")
                        self.tag_filter_layout.addWidget(info_label)
                        return

                    # For each tag column, get unique values and create filter controls
                    self.tag_filters = {}

                    for tag_column in sorted(columns):
                        cursor.execute(
                            f"SELECT DISTINCT {tag_column} FROM images WHERE {tag_column} IS NOT NULL ORDER BY {tag_column}"
                        )
                        unique_values = [row[0] for row in cursor.fetchall()]

                        if not unique_values:
                            continue

                        tag_layout = QHBoxLayout()

                        display_name = tag_column[4:] if tag_column.startswith('Tag_') else tag_column
                        tag_label = QLabel(f"{display_name}:")
                        tag_label.setFixedWidth(150)

                        tag_combo = QComboBox()
                        tag_combo.setMinimumWidth(200)
                        tag_combo.setMaximumWidth(400)
                        tag_combo.addItem("Any")
                        tag_combo.addItems(unique_values)
                        tag_combo.currentTextChanged.connect(self.on_tag_filter_value_changed)

                        tag_layout.addWidget(tag_label)
                        tag_layout.addWidget(tag_combo)
                        tag_layout.addStretch()

                        self.tag_filters[tag_column] = tag_combo

                        tag_widget = QWidget()
                        tag_widget.setLayout(tag_layout)
                        tag_widget.setMinimumHeight(40)
                        tag_widget.setProperty('tag_name', display_name)
                        self.tag_filter_layout.addWidget(tag_widget)

                        tag_widget.setVisible(True)
                        tag_widget.show()

                        print(f"Added tag filter: {display_name} with {len(unique_values)} values")
                        print(f"Widget added to layout at index {self.tag_filter_layout.count() - 1}")

                    print(f"Total widgets in layout: {self.tag_filter_layout.count()}")

                    self.tag_filter_container.setVisible(True)
                    self.tag_filter_container.show()

                    self.tag_filter_container.updateGeometry()
                    self.tag_filter_layout.update()

                    if hasattr(self, 'tag_filter_scroll'):
                        self.tag_filter_scroll.setVisible(True)
                        self.tag_filter_scroll.show()
                        self.tag_filter_scroll.ensureWidgetVisible(self.tag_filter_container)
                        print(f"Scroll area size: {self.tag_filter_scroll.size()}")
                        print(f"Container size: {self.tag_filter_container.size()}")

                    self.tag_filter_container.repaint()
                    self.tag_filter_scroll.repaint()

                    # Apply any active text filter after repopulating
                    if hasattr(self, 'tag_search_input'):
                        self.filter_tag_filters()

                    print(f"DEBUG: Final layout check - {self.tag_filter_layout.count()} widgets in layout")
                    for i in range(self.tag_filter_layout.count()):
                        widget = self.tag_filter_layout.itemAt(i).widget()
                        print(f"  Widget {i}: {type(widget).__name__} - visible: {widget.isVisible()}")

                    QMessageBox.information(
                        self,
                        "Success",
                        f"Loaded {len(columns)} tag filters.\nLook for dropdown menus in the Tag Filters section below."
                    )

            except Exception as e:
                print(f"Error refreshing tag filters: {str(e)}")
                QMessageBox.critical(self, "Error", f"Error refreshing tag filters: {str(e)}")

        def get_tag_filter_conditions(self) -> tuple:
            """Get SQL conditions and parameters for tag filters."""
            conditions = []
            params = []
            
            if hasattr(self, 'tag_filters'):
                for tag_column, combo_box in self.tag_filters.items():
                    selected_value = combo_box.currentText()
                    if selected_value != "Any":
                        conditions.append(f"{tag_column} = ?")
                        params.append(selected_value)
            
            return conditions, params

        def filter_tag_filters(self):
            """Show/hide tag filter widgets based on search text."""
            if not hasattr(self, 'tag_filter_layout'):
                return

            text = self.tag_search_input.text().lower() if hasattr(self, 'tag_search_input') else ""

            for i in range(self.tag_filter_layout.count()):
                widget = self.tag_filter_layout.itemAt(i).widget()
                if not widget or not widget.property('tag_name'):
                    continue

                tag_name = widget.property('tag_name').lower()
                widget.setVisible(text in tag_name)

        def _handle_manual_coordinate_update(self):
            """Parse manual coordinate input and update map if valid."""
            try:
                lat_str = self.manual_lat_input.text().strip()
                lon_str = self.manual_lon_input.text().strip()
                lat_format = self.lat_format_combo.currentText()
                lon_format = self.lon_format_combo.currentText()
                radius_m = self.radius_input.value() # Get current radius

                if not lat_str or not lon_str:
                    # Don't update if either field is empty
                    return

                parsed_lat, parsed_lon = None, None

                if lat_format == "Decimal Degrees":
                    parsed_lat = float(lat_str)
                else:
                    parsed_lat = parse_dms_string_to_dd(lat_str, is_latitude=True)
                
                if lon_format == "Decimal Degrees":
                    parsed_lon = float(lon_str)
                else:
                    parsed_lon = parse_dms_string_to_dd(lon_str, is_latitude=False)

                if parsed_lat is not None and parsed_lon is not None:
                    print(f"Manual input parsed: Lat={parsed_lat}, Lon={parsed_lon}, Radius={radius_m}")
                    # Update map widget
                    if hasattr(self.map_widget, 'update_view_and_circle'):
                        self._is_manual_coord_update = True # Set flag before map update
                        self.map_widget.update_view_and_circle(parsed_lat, parsed_lon, radius_m)
                        # self._is_manual_coord_update will be reset by handle_map_selection
                    
                    # Update internal state as if map was clicked (already handled if map_widget calls handleMapClick JS)
                    # self.lat_input_map = parsed_lat
                    # self.lon_input_map = parsed_lon
                    # self.search_btn.setEnabled(True) # Also handled by handle_map_selection
                # else: Error in parsing, do nothing, perform_radius_search will catch it later if user searches

            except ValueError: # Catch float() or DMS parsing errors
                # Silently ignore for now, or show a temporary status bar message
                # The main search function will provide a modal error if search is attempted with invalid input.
                print(f"Invalid coordinate format during live update attempt: Lat='{lat_str}', Lon='{lon_str}'")
            except Exception as e:
                print(f"Error in _handle_manual_coordinate_update: {e}")

        def select_source_dir(self):
            directory = QFileDialog.getExistingDirectory(self, "Select Image Directory")
            if directory:
                self.source_path.setText(directory)
                self.image_files = []
                
                # Create separate lists for different image types
                jpg_files = []
                other_image_files = []
                
                for root, _, files in os.walk(directory):
                    for file in files:
                        lower_file = file.lower()
                        full_path = os.path.join(root, file)
                        
                        # Separate JPG files from other image types
                        if lower_file.endswith(('.jpg', '.jpeg')):
                            jpg_files.append(full_path)
                        elif lower_file.endswith(('.png', '.tiff', '.bmp')):
                            other_image_files.append(full_path)
                
                # Sort each list alphabetically
                jpg_files.sort()
                other_image_files.sort()
                
                # Combine lists with JPG files first
                self.image_files = jpg_files + other_image_files
                
                if self.image_files:
                    self.preview_btn.setEnabled(True)
                    first_file = os.path.basename(self.image_files[0])
                    self.status_label.setText(f"Found {len(self.image_files)} images. First file: {first_file}")
                else:
                    self.status_label.setText("No images found in selected directory")

        def select_db_file(self):
            if self.append_mode.isChecked():
                # When appending, we need to select an existing database
                file_path, _ = QFileDialog.getOpenFileName(
                    self,
                    "Select Existing Database File",
                    "",
                    "SQLite Database (*.db)"
                )
                if file_path:
                    # Verify the database has the required table
                    try:
                        conn = sqlite3.connect(file_path)
                        cursor = conn.cursor()
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='images'")
                        if cursor.fetchone() is None:
                            QMessageBox.warning(self, "Error", "Selected database does not contain the required 'images' table.")
                            conn.close()
                            return
                        conn.close()
                        self.db_path.setText(file_path)
                    except Exception as e:
                        QMessageBox.warning(self, "Error", f"Error verifying database: {str(e)}")
            else:
                # Creating new database
                file_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save Database File",
                    "",
                    "SQLite Database (*.db)"
                )
                if file_path:
                    if not file_path.endswith('.db'):
                        file_path += '.db'
                    self.db_path.setText(file_path)

        def select_search_database(self):
            """Select and load database for radius search."""
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Database File",
                "",
                "SQLite Database (*.db)"
            )
            if file_path:
                self.search_db_path.setText(file_path)
                self.load_search_filters()

        def load_search_filters(self):
            """Load folder and session filters for radius search."""
            try:
                with sqlite3.connect(self.search_db_path.text()) as conn:
                    cursor = conn.cursor()

                    # Get unique folders
                    cursor.execute("""
                        SELECT DISTINCT File_Location_Folder
                        FROM images
                        WHERE File_Location_Folder IS NOT NULL
                        ORDER BY File_Location_Folder
                    """)
                    folders = [row[0] for row in cursor.fetchall()]

                    # Get unique sessions
                    cursor.execute("""
                        SELECT DISTINCT File_Location_Session
                        FROM images
                        WHERE File_Location_Session IS NOT NULL
                        ORDER BY File_Location_Session
                    """)
                    sessions = [row[0] for row in cursor.fetchall()]

                    # Update combo boxes
                    self.search_folder_combo.clear()
                    self.search_folder_combo.addItem("All Folders")
                    self.search_folder_combo.addItems(folders)

                    self.search_session_combo.clear()
                    self.search_session_combo.addItem("All Sessions")
                    self.search_session_combo.addItems(sessions)

            except Exception as e:
                print(f"Error loading filters: {str(e)}")
                QMessageBox.critical(self, "Error", f"Error loading filters: {str(e)}")

        def load_images_on_map(self):
            """Load filtered images with GPS coordinates onto the map."""
            if self.search_db_path.text() == "Not selected":
                QMessageBox.warning(self, "Error", "Please select a database file.")
                return

            try:
                print(f"Loading images from database: {self.search_db_path.text()}")
                print("Getting tag filter conditions...")
                tag_conditions, tag_params = self.get_tag_filter_conditions()
                print(f"Tag conditions: {tag_conditions}")
                print(f"Tag parameters: {tag_params}")
                
                conn = sqlite3.connect(self.search_db_path.text())
                cursor = conn.cursor()

                try:
                    cursor.execute("PRAGMA table_info(images)")
                    columns = [row[1] for row in cursor.fetchall()]

                    prefixes = ["XML_", "EXIF_", "JSON_"]
                    lat_col = lon_col = None
                    data_found = False

                    selected_folder = self.search_folder_combo.currentText()
                    selected_session = self.search_session_combo.currentText()
                    tag_conditions, tag_params = self.get_tag_filter_conditions()

                    for prefix in prefixes:
                        possible_lat = next((c for c in columns if c.startswith(prefix) and "latitude" in c.lower()), None)
                        possible_lon = next((c for c in columns if c.startswith(prefix) and "longitude" in c.lower()), None)
                        if not possible_lat or not possible_lon:
                            continue

                        query = f"SELECT {possible_lat}, {possible_lon}, path, File_Location_Folder, File_Location_Session FROM images WHERE {possible_lat} IS NOT NULL AND {possible_lon} IS NOT NULL AND TRIM({possible_lat}) != '' AND TRIM({possible_lon}) != ''"
                        params = []
                        if selected_folder != "All Folders":
                            query += " AND File_Location_Folder = ?"
                            params.append(selected_folder)
                        if selected_session != "All Sessions":
                            query += " AND File_Location_Session = ?"
                            params.append(selected_session)
                        if tag_conditions:
                            query += " AND " + " AND ".join(tag_conditions)
                            params.extend(tag_params)

                        cursor.execute(query, params)
                        rows = cursor.fetchall()
                        if rows:
                            lat_col, lon_col = possible_lat, possible_lon
                            data_found = True
                            break
                        elif lat_col is None:
                            # remember first available columns in case none have data
                            lat_col, lon_col = possible_lat, possible_lon

                    all_coordinates = []
                    processed_paths = set()

                    if data_found:
                        for lat, lon, path, folder, session in rows:
                            if path in processed_paths:
                                continue
                            try:
                                lat = float(lat)
                                lon = float(lon)
                                all_coordinates.append((lat, lon, f"{path} ({folder}/{session})"))
                                processed_paths.add(path)
                            except (ValueError, TypeError):
                                continue

                    # Always get all matching images for thumbnails
                    query = "SELECT path, File_Location_Folder, File_Location_Session FROM images WHERE 1=1"
                    params = []
                    if selected_folder != "All Folders":
                        query += " AND File_Location_Folder = ?"
                        params.append(selected_folder)
                    if selected_session != "All Sessions":
                        query += " AND File_Location_Session = ?"
                        params.append(selected_session)
                    if tag_conditions:
                        query += " AND " + " AND ".join(tag_conditions)
                        params.extend(tag_params)

                    print(f"Executing query: {query}")
                    print(f"With parameters: {params}")
                    
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
                    total_matching = len(rows)

                    if total_matching == 0:
                        QMessageBox.warning(self, "Warning", "No images found matching the selected tag combination.")
                    else:
                        # Add map markers if we have coordinates
                        if all_coordinates:
                            print(f"Loading {len(all_coordinates)} markers on map")
                            self.map_widget.add_image_markers(all_coordinates)
                            
                        # Always show thumbnails for all matching images
                        results = [(p, 0, None, f, s) for p, f, s in rows]
                        self.handle_search_results(results)
                        
                        # Show appropriate message
                        if all_coordinates:
                            QMessageBox.information(self, "Success", 
                                f"Found {total_matching} images, {len(all_coordinates)} with GPS coordinates.")
                        else:
                            QMessageBox.warning(self, "Warning",
                                f"Found {total_matching} images matching your filters, but none of them have GPS coordinates.")
                finally:
                    conn.close()

            except Exception as e:
                print(f"Error in load_images_on_map: {str(e)}")
                QMessageBox.critical(self, "Error", f"Error loading images: {str(e)}")
                if 'conn' in locals():
                    conn.close()

        def update_circle_radius(self, value):
            """Update the circle radius on the map."""
            self.map_widget.web_view.page().runJavaScript(f"window.updateRadius({value});")

        def handle_map_selection(self, lat: float, lon: float, radius: float):
            """Handle when user makes a selection on the map."""
            print(f"Map selection: lat={lat}, lon={lon}, radius={radius}")  # Debug print
            self.lat_input_map = lat  # Store map-clicked latitude
            self.lon_input_map = lon  # Store map-clicked longitude
            
            # Update manual input fields
            self.manual_lat_input.setText(f"{lat:.6f}")
            self.manual_lon_input.setText(f"{lon:.6f}")

            # Only set format to DD if this isn't a result of a manual update cycle
            if not self._is_manual_coord_update:
                self.lat_format_combo.setCurrentText("Decimal Degrees")
                self.lon_format_combo.setCurrentText("Decimal Degrees")
            
            self.radius_input.setValue(int(radius))
            # Enable search button when a location is selected
            self.search_btn.setEnabled(True)
            self._is_manual_coord_update = False # Reset flag after handling

        def perform_radius_search(self):
            """Perform the radius search with the current map selection and filters."""
            if self.search_db_path.text() == "Not selected":
                QMessageBox.warning(self, "Error", "Please select a database file.")
                return

            search_lat = None
            search_lon = None

            # Try to get coordinates from manual input fields first
            try:
                manual_lat_str = self.manual_lat_input.text().strip()
                manual_lon_str = self.manual_lon_input.text().strip()
                lat_format = self.lat_format_combo.currentText()
                lon_format = self.lon_format_combo.currentText()

                if manual_lat_str and manual_lon_str:
                    if lat_format == "Decimal Degrees":
                        search_lat = float(manual_lat_str)
                    else: # Degrees, Minutes, Seconds
                        search_lat = parse_dms_string_to_dd(manual_lat_str, is_latitude=True)
                    
                    if lon_format == "Decimal Degrees":
                        search_lon = float(manual_lon_str)
                    else: # Degrees, Minutes, Seconds
                        search_lon = parse_dms_string_to_dd(manual_lon_str, is_latitude=False)

                    if search_lat is None or search_lon is None: # Check if DMS parsing failed
                        raise ValueError("Invalid DMS format or value")
                        
                    print(f"Using manually entered coordinates: Lat={search_lat}, Lon={search_lon}")
            except ValueError as e:
                QMessageBox.warning(self, "Input Error", f"Invalid coordinate format or value: {e}. Please check your input.")
                return # Stop if manual input is invalid

            # If manual coordinates were not successfully parsed or were empty, try map-selected coordinates
            if search_lat is None or search_lon is None:
                if hasattr(self, 'lat_input_map') and hasattr(self, 'lon_input_map'):
                    search_lat = self.lat_input_map
                    search_lon = self.lon_input_map
                    print(f"Using map-selected coordinates: Lat={search_lat}, Lon={search_lon}")
                else:
                    QMessageBox.warning(self, "Error", "Please select a location on the map or enter valid coordinates manually.")
                    return
            
            if search_lat is None or search_lon is None: # Should not happen if logic above is correct, but as a safeguard
                QMessageBox.warning(self, "Error", "Coordinates for search not found.")
                return

            self.search_progress.setValue(0)

            # Clear existing thumbnails
            for i in reversed(range(self.thumbnail_layout.count())): 
                self.thumbnail_layout.itemAt(i).widget().setParent(None)

            # Get altitude range values
            min_alt = None if self.min_alt_input.value() == self.min_alt_input.minimum() else self.min_alt_input.value()
            max_alt = None if self.max_alt_input.value() == self.max_alt_input.maximum() else self.max_alt_input.value()

            # Get folder and session filters
            selected_folder = self.search_folder_combo.currentText()
            selected_session = self.search_session_combo.currentText()

            # Get tag filters
            tag_filter_values = {}
            if hasattr(self, 'tag_filters'):
                for tag_column, combo_box in self.tag_filters.items():
                    selected_value = combo_box.currentText()
                    if selected_value != "Any":
                        tag_filter_values[tag_column] = selected_value

            # Store active filters so handle_search_results can access them
            self.active_search_filters = {
                'folder': selected_folder if selected_folder != "All Folders" else None,
                'session': selected_session if selected_session != "All Sessions" else None,
                'tag_filters': tag_filter_values,
            }

            # Create and start the worker with filters
            self.search_worker = RadiusSearchWorker(
                self.search_db_path.text(),
                search_lat, # Use determined latitude
                search_lon, # Use determined longitude
                self.radius_input.value(),
                min_alt,
                max_alt,
                selected_folder if selected_folder != "All Folders" else None,
                selected_session if selected_session != "All Sessions" else None,
                tag_filter_values
            )
            self.search_worker.progress.connect(self.search_progress.setValue)
            self.search_worker.finished.connect(self.handle_search_results)
            self.search_worker.error.connect(self.show_error)
            self.search_worker.start()

        def handle_search_results(self, results):
            """Display search results as thumbnails."""
            print(f"Handling {len(results)} search results")
            
            # Clear existing thumbnails and selections
            print("Clearing existing thumbnails...")
            for i in reversed(range(self.thumbnail_layout.count())): 
                self.thumbnail_layout.itemAt(i).widget().setParent(None)
            self.selected_images.clear()
            self.selection_counter.setText("Selected: 0 images")
            
            if not results:
                print("No results to display")
                QMessageBox.information(self, "Search Results", "No images found matching the criteria.")
                self.select_all_btn.setEnabled(False)
                self.save_selected_btn.setEnabled(False)
                return
            
            # Store image paths for navigation
            self.image_files = [img_path for img_path, _, _, _, _ in results]
            
            # Calculate grid layout
            num_columns = max(1, self.thumbnail_container.width() // 220)  # 200px width + 20px margin
            current_row = 0
            current_col = 0
            
            print("Creating thumbnails...")
            self.search_progress.setRange(0, len(results))
            self.search_progress.show()
            
            for index, (img_path, distance, altitude, folder, session) in enumerate(results):
                try:
                    # Update progress
                    self.search_progress.setValue(index + 1)
                    QApplication.processEvents()  # Allow UI updates
                    
                    # Create thumbnail widget
                    thumbnail = ThumbnailWidget(
                        img_path,
                        distance * 1000,  # Convert km to meters
                        altitude,
                        f"{folder}/{session}"
                    )
                    # Connect signals
                    thumbnail.clicked.connect(self.show_full_image)
                    thumbnail.selection_changed.connect(self.on_thumbnail_selection_changed)
                    
                    # Add to grid
                    self.thumbnail_layout.addWidget(thumbnail, current_row, current_col)
                    
                    # Update grid position
                    current_col += 1
                    if current_col >= num_columns:
                        current_col = 0
                        current_row += 1
                        
                    # Process events every 10 thumbnails to keep UI responsive
                    if index % 10 == 0:
                        QApplication.processEvents()
                        
                except Exception as e:
                    print(f"Error creating thumbnail for {img_path}: {str(e)}")
                    continue
            
            print("Finished creating thumbnails")
            
            # Enable selection controls
            self.select_all_btn.setEnabled(True)
            self.save_selected_btn.setEnabled(False)  # Will be enabled when images are selected
            
            QMessageBox.information(self, "Search Complete", f"Found {len(results)} images matching the criteria.")

        def toggle_select_all(self):
            """Toggle selection of all thumbnails."""
            # Get current state of first thumbnail to determine action
            first_thumbnail = self.thumbnail_layout.itemAt(0).widget() if self.thumbnail_layout.count() > 0 else None
            select_all = not (first_thumbnail and first_thumbnail.is_selected()) if first_thumbnail else True
            
            # Update all thumbnails
            for i in range(self.thumbnail_layout.count()):
                thumbnail = self.thumbnail_layout.itemAt(i).widget()
                if thumbnail:
                    thumbnail.set_selected(select_all)
            
            # Update button text
            self.select_all_btn.setText("Deselect All" if select_all else "Select All")

        def save_selected_images(self):
            """Save selected images to a user-specified directory."""
            if not self.selected_images:
                QMessageBox.warning(self, "Warning", "No images selected")
                return
            
            # Get destination directory
            dest_dir = QFileDialog.getExistingDirectory(
                self,
                "Select Destination Directory",
                "",
                QFileDialog.Option.ShowDirsOnly
            )
            
            if not dest_dir:
                return
            
            # Copy files
            success_count = 0
            error_count = 0
            for src_path in self.selected_images:
                try:
                    # Create destination path
                    dest_path = os.path.join(dest_dir, os.path.basename(src_path))
                    
                    # Copy file
                    shutil.copy2(src_path, dest_path)
                    success_count += 1
                except Exception as e:
                    print(f"Error copying {src_path}: {str(e)}")
                    error_count += 1
            
            # Show results
            message = f"Successfully copied {success_count} images"
            if error_count > 0:
                message += f"\nFailed to copy {error_count} images"
            QMessageBox.information(self, "Save Complete", message)

        def show_full_image(self, image_path):
            """Show full-size image in a dialog."""
            dialog = ImagePreviewDialog(image_path, self.image_files, self)
            dialog.exec()

        def toggle_append_mode(self, state):
            """Handle changes in append mode checkbox state."""
            if state and self.db_path.text() != "Not selected":
                # If switching to append mode with a selected database, verify it exists
                if not os.path.exists(self.db_path.text()):
                    QMessageBox.warning(self, "Warning", "Selected database file does not exist.")
                    self.append_mode.setChecked(False)

        def metadata_source_changed(self, text):
            # Add any additional logic you want to execute when metadata source changes
            print(f"Metadata source changed: {text}")

        def configure_mapping(self):
            """Open the mapping configuration dialog."""
            dialog = MappingDialog(self)
            dialog.exec()

        def load_field_mapping(self):
            """Load the field mapping configuration."""
            try:
                mapping_file = os.path.join(os.path.dirname(__file__), "metadata_mapping.json")
                if os.path.exists(mapping_file):
                    with open(mapping_file, 'r') as f:
                        return json.load(f)
                return {
                    "xml_to_exif": {},
                    "exif_to_xml": {}
                }
            except Exception as e:
                print(f"Error loading field mapping: {str(e)}")
                return {
                    "xml_to_exif": {},
                    "exif_to_xml": {}
                }

        def apply_field_mapping(self, metadata, is_xml_source):
            """Apply field mapping to metadata dictionary."""
            mapping = self.load_field_mapping()
            mapped_metadata = {}
            
            if is_xml_source:
                # Apply XML to EXIF mapping
                for xml_field, value in metadata.items():
                    exif_field = mapping["xml_to_exif"].get(xml_field, xml_field)
                    mapped_metadata[exif_field] = value
            else:
                # Apply EXIF to XML mapping
                for exif_field, value in metadata.items():
                    xml_field = mapping["exif_to_xml"].get(exif_field, exif_field)
                    mapped_metadata[xml_field] = value
            
            return mapped_metadata

        def display_exif_data(self, metadata: Dict[str, str], is_xml_source: bool):
            """Display ALL metadata with prefixes in the table and add folder name fields."""
            self.exif_data = {}  # Start with a fresh dictionary
            
            # First add our custom location fields
            if self.image_files and len(self.image_files) > 0:
                image_path = self.image_files[0]
                folder = os.path.basename(os.path.dirname(image_path))
                session_path = os.path.dirname(os.path.dirname(image_path))
                session = os.path.basename(session_path) if session_path else ''
                
                self.exif_data['File_Location_Folder'] = folder
                self.exif_data['File_Location_Session'] = session
            
            # NEW APPROACH: Use ALL metadata directly with prefixes (no mapping needed)
            # The metadata already comes with EXIF_, JSON_, XML_ prefixes from extract_exif_data
            self.exif_data.update(metadata)
            
            self.table.setRowCount(len(self.exif_data))
            
            # Group fields by prefix for better display
            prefixed_fields = []
            other_fields = []
            
            for key, value in self.exif_data.items():
                if key.startswith(('EXIF_', 'JSON_', 'XML_')):
                    prefixed_fields.append((key, value))
                else:
                    other_fields.append((key, value))
            
            # Sort prefixed fields by prefix, then by name
            prefixed_fields.sort(key=lambda x: (x[0].split('_')[0], x[0]))
            
            # Combine: other fields first, then prefixed fields
            all_fields = other_fields + prefixed_fields
            
            for i, (key, value) in enumerate(all_fields):
                # Create checkbox using the utility function, checked by default
                checkbox_widget = GUIUtils.create_table_checkbox_widget(checked=True, parent=self.table)
                    
                # Add items to table
                self.table.setCellWidget(i, 0, checkbox_widget)
                self.table.setItem(i, 1, QTableWidgetItem(key))
                self.table.setItem(i, 2, QTableWidgetItem(str(value)))
            
            self.preview_btn.setEnabled(True)
            self.process_btn.setEnabled(True)
            
            # Show comprehensive status
            exif_count = len([k for k in metadata.keys() if k.startswith('EXIF_')])
            json_count = len([k for k in metadata.keys() if k.startswith('JSON_')])
            xml_count = len([k for k in metadata.keys() if k.startswith('XML_')])
            
            status_parts = []
            if exif_count > 0:
                status_parts.append(f"{exif_count} EXIF fields")
            if json_count > 0:
                status_parts.append(f"{json_count} JSON fields")
            if xml_count > 0:
                status_parts.append(f"{xml_count} XML fields")
            
            status_msg = f"Ready to process images - Found: {', '.join(status_parts)}"
            self.status_label.setText(status_msg)

        def preview_first_image(self):
            if not self.image_files:
                return
            
            self.status_label.setText("Extracting metadata from first image...")
            self.preview_btn.setEnabled(False)
            
            # Use the imported ExifExtractorWorker
            self.worker = ExifExtractorWorker(self.image_files[0])
            self.worker.exif_extracted.connect(self.display_exif_data)
            self.worker.error.connect(self.show_error)
            self.worker.start()

        def process_images(self):
            if not self.image_files or not self.db_path.text() or self.db_path.text() == "Not selected":
                QMessageBox.warning(self, "Error", "Please select both source directory and database file.")
                return

            self.selected_fields = []
            for i in range(self.table.rowCount()):
                checkbox_widget = self.table.cellWidget(i, 0)
                if checkbox_widget:
                    checkbox = checkbox_widget.findChild(QCheckBox)
                    if checkbox and checkbox.isChecked():
                        field = self.table.item(i, 1).text()
                        self.selected_fields.append(field)

            if not self.selected_fields:
                QMessageBox.warning(self, "Error", "Please select at least one metadata field from the preview table.")
                return

            try:
                # process_tags_for_batch now returns all_image_tags and unique_tag_names
                all_image_tags, unique_tag_names = process_tags_for_batch(self.image_files, self.db_path.text())
                if unique_tag_names:
                    tag_count_msg = sum(len(tags) for tags in all_image_tags.values())
                    # Count images that actually received tags
                    image_count_msg = len([p for p, t in all_image_tags.items() if t]) 
                    QMessageBox.information(
                        self, 
                        "Tags Discovered", 
                        f"Discovered {len(unique_tag_names)} unique tag types affecting {image_count_msg} images "
                        f"with {tag_count_msg} total tag assignments. Tag columns will be added by the worker if needed."
                    )
                else:
                    logger.info("No tags discovered from tags.config files during MainWindow.process_images")
            except Exception as e:
                logger.error(f"Error discovering tags from tags.config files: {str(e)}", exc_info=True)
                QMessageBox.warning(self, "Warning", f"Error discovering tags from tags.config files: {str(e)}")
                all_image_tags = {}
                unique_tag_names = []
            
            # IMPORTANT: Database deletion logic is REMOVED from here.
            # It will be handled by the BatchProcessWorker based on append_mode.

            self.progress_bar.setValue(0)
            self.status_label.setText("Processing images...")
            self.process_btn.setEnabled(False)
            
            self.batch_worker = BatchProcessWorker(
                self.image_files, 
                self.db_path.text(), 
                self.field_mapping_config, 
                self.selected_fields,      
                self.append_mode.isChecked(),
                all_image_tags,         # Pass the per-image tags data
                unique_tag_names      # Pass the list of all unique tag column names
            )
            self.batch_worker.progress.connect(self.progress_bar.setValue)
            self.batch_worker.file_processed.connect(self.update_status)
            self.batch_worker.finished.connect(self.processing_finished)
            self.batch_worker.error.connect(self.show_error)
            self.batch_worker.start()

        def update_status(self, file_path):
            self.status_label.setText(f"Processed: {file_path}")

        def processing_finished(self):
            self.progress_bar.setValue(100)
            self.status_label.setText("Processing complete!")
            self.preview_btn.setEnabled(True)
            self.process_btn.setEnabled(True)
            QMessageBox.information(self, "Success", "EXIF data extraction complete!")

        def show_error(self, message):
            QMessageBox.warning(self, "Error", message)
            self.preview_btn.setEnabled(True)
            self.process_btn.setEnabled(True)

        def on_search_mode_changed(self):
            """Handle changes in the tag search mode."""
            is_filter_mode = self.filter_mode_radio.isChecked()
            
            # Update UI based on mode
            self.tag_filter_scroll.setVisible(is_filter_mode)
            self.tag_search_input.setPlaceholderText(
                "Filter visible tags..." if is_filter_mode else "Search for tags in database..."
            )
            
            # Clear the search input when switching modes
            self.tag_search_input.clear()
            
            # Show/hide the refresh button based on mode
            refresh_btn = None
            for i in range(self.tag_filter_layout.count()):
                widget = self.tag_filter_layout.itemAt(i).widget()
                if isinstance(widget, QPushButton) and widget.text() == "Refresh Tag Filters":
                    refresh_btn = widget
                    break
            
            if refresh_btn:
                refresh_btn.setVisible(is_filter_mode)

        def on_tag_filter_changed(self):
            """Handle changes in the tag search input for filter mode."""
            if self.filter_mode_radio.isChecked():
                self.filter_tag_filters()

        def on_tag_search_submitted(self):
            """Handle Enter key press in the search input."""
            if not self.filter_mode_radio.isChecked():
                self.search_tags_in_database()

        def search_tags_in_database(self):
            """Search for tags directly in the database and update map if coordinates are available."""
            search_text = self.tag_search_input.text().strip().lower()
            
            if not search_text or self.search_db_path.text() == "Not selected":
                return
                
            # Clear existing thumbnails
            for i in reversed(range(self.thumbnail_layout.count())): 
                widget = self.thumbnail_layout.itemAt(i).widget()
                if widget:
                    widget.setParent(None)
            
            # Clear existing map markers and reset view
            self.map_widget.create_map()  # This will reset the map view
            
            try:
                with sqlite3.connect(self.search_db_path.text()) as conn:
                    cursor = conn.cursor()
                    
                    # Get all tag columns
                    cursor.execute("PRAGMA table_info(images)")
                    columns = cursor.fetchall()
                    tag_columns = [row[1] for row in columns if row[1].startswith('Tag_')]
                    
                    if not tag_columns:
                        return
                    
                    # Find GPS coordinate columns (prioritize XML > EXIF > JSON)
                    all_columns = [row[1] for row in columns]
                    lat_col = lon_col = None
                    for prefix in ["XML_", "EXIF_", "JSON_"]:
                        possible_lat = next((c for c in all_columns if c.startswith(prefix) and "latitude" in c.lower()), None)
                        possible_lon = next((c for c in all_columns if c.startswith(prefix) and "longitude" in c.lower()), None)
                        if possible_lat and possible_lon:
                            lat_col, lon_col = possible_lat, possible_lon
                            break
                    
                    # Build query to search across all tag columns
                    conditions = []
                    params = []
                    for col in tag_columns:
                        conditions.append(f"LOWER({col}) LIKE ?")
                        params.append(f"%{search_text}%")
                    
                    # Base query including GPS coordinates if available
                    select_cols = ["path", "File_Location_Folder", "File_Location_Session"]
                    if lat_col and lon_col:
                        select_cols.extend([lat_col, lon_col])
                    
                    # Get folder and session filters
                    selected_folder = self.search_folder_combo.currentText()
                    selected_session = self.search_session_combo.currentText()
                    
                    query = f"""
                        SELECT DISTINCT {', '.join(select_cols)}
                        FROM images
                        WHERE ({" OR ".join(conditions)})
                    """
                    
                    # Add folder filter
                    if selected_folder != "All Folders":
                        query += " AND File_Location_Folder = ?"
                        params.append(selected_folder)
                    
                    # Add session filter
                    if selected_session != "All Sessions":
                        query += " AND File_Location_Session = ?"
                        params.append(selected_session)
                    
                    cursor.execute(query, params)
                    results = cursor.fetchall()
                    
                    if results:
                        # Process results and update map if coordinates are available
                        formatted_results = []
                        map_coordinates = []
                        
                        for row in results:
                            path = row[0]
                            folder = row[1]
                            session = row[2]
                            
                            # Check for valid GPS coordinates
                            lat = lon = None
                            if lat_col and lon_col and len(row) > 3:
                                try:
                                    # Handle both string and float inputs
                                    lat_val = str(row[3]) if isinstance(row[3], float) else row[3]
                                    lon_val = str(row[4]) if isinstance(row[4], float) else row[4]
                                    
                                    lat = float(lat_val) if lat_val and str(lat_val).strip() else None
                                    lon = float(lon_val) if lon_val and str(lon_val).strip() else None
                                    
                                    if lat is not None and lon is not None:
                                        map_coordinates.append((lat, lon, path))
                                except (ValueError, TypeError, AttributeError):
                                    pass
                            
                            formatted_results.append((path, 0, None, folder, session))
                        
                        # Update map if we have coordinates
                        if map_coordinates:
                            # Calculate center point for map
                            avg_lat = sum(coord[0] for coord in map_coordinates) / len(map_coordinates)
                            avg_lon = sum(coord[1] for coord in map_coordinates) / len(map_coordinates)
                            
                            # Update map view with new markers
                            self.map_widget.create_map((avg_lat, avg_lon))
                            self.map_widget.add_image_markers(map_coordinates)
                        
                        # Clear selection tracking
                        self.selected_images.clear()
                        self.selection_counter.setText("Selected: 0 images")
                        
                        # Update thumbnails with new results
                        self.handle_search_results(formatted_results)
                        
                        # Enable/disable buttons based on results
                        self.select_all_btn.setEnabled(bool(formatted_results))
                        self.save_selected_btn.setEnabled(False)
                    else:
                        QMessageBox.information(self, "Search Results", "No images found with matching tags.")
                        self.handle_search_results([])  # Clear any existing results
                        
            except sqlite3.Error as e:
                print(f"Error searching tags in database: {e}")
                QMessageBox.critical(self, "Error", f"Error searching tags: {str(e)}")

        def on_tag_filter_value_changed(self, value):
            """Handle changes in tag filter dropdown values."""
            # Clear existing thumbnails
            for i in reversed(range(self.thumbnail_layout.count())): 
                widget = self.thumbnail_layout.itemAt(i).widget()
                if widget:
                    widget.setParent(None)
            
            # Clear existing map markers and reset view
            self.map_widget.create_map()
            
            # Show progress in status bar
            self.search_progress.setRange(0, 0)  # Indeterminate progress
            self.search_progress.show()
            QApplication.processEvents()  # Ensure UI updates
            
            try:
                # Trigger a new search with current filters
                self.load_images_on_map()
            finally:
                # Reset progress bar
                self.search_progress.setRange(0, 100)
                self.search_progress.hide()
            # Clear existing map markers and reset view
            self.map_widget.create_map()
            
            # Trigger a new search with current filters
            self.load_images_on_map()

    class MappingDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Metadata Field Mapping")
            self.setMinimumWidth(600)
            self.setup_ui()
            self.load_mapping()

        def setup_ui(self):
            layout = QVBoxLayout(self)

            # Add tabs for XML->EXIF and EXIF->XML mappings
            tabs = QTabWidget()
            self.xml_to_exif_tab = QWidget()
            self.exif_to_xml_tab = QWidget()
            tabs.addTab(self.xml_to_exif_tab, "XML to EXIF")
            tabs.addTab(self.exif_to_xml_tab, "EXIF to XML")
            layout.addWidget(tabs)

            # Setup XML to EXIF mapping table
            xml_layout = QVBoxLayout(self.xml_to_exif_tab)
            self.xml_to_exif_table = QTableWidget()
            self.xml_to_exif_table.setColumnCount(2)
            self.xml_to_exif_table.setHorizontalHeaderLabels(["XML Field", "EXIF Field"])
            self.xml_to_exif_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            xml_layout.addWidget(self.xml_to_exif_table)

            # Add/Remove buttons for XML to EXIF
            xml_btn_layout = QHBoxLayout()
            add_xml_btn = QPushButton("Add Mapping")
            add_xml_btn.clicked.connect(lambda: self.add_mapping(self.xml_to_exif_table))
            remove_xml_btn = QPushButton("Remove Selected")
            remove_xml_btn.clicked.connect(lambda: self.remove_mapping(self.xml_to_exif_table))
            xml_btn_layout.addWidget(add_xml_btn)
            xml_btn_layout.addWidget(remove_xml_btn)
            xml_layout.addLayout(xml_btn_layout)

            # Setup EXIF to XML mapping table
            exif_layout = QVBoxLayout(self.exif_to_xml_tab)
            self.exif_to_xml_table = QTableWidget()
            self.exif_to_xml_table.setColumnCount(2)
            self.exif_to_xml_table.setHorizontalHeaderLabels(["EXIF Field", "XML Field"])
            self.exif_to_xml_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            exif_layout.addWidget(self.exif_to_xml_table)

            # Add/Remove buttons for EXIF to XML
            exif_btn_layout = QHBoxLayout()
            add_exif_btn = QPushButton("Add Mapping")
            add_exif_btn.clicked.connect(lambda: self.add_mapping(self.exif_to_xml_table))
            remove_exif_btn = QPushButton("Remove Selected")
            remove_exif_btn.clicked.connect(lambda: self.remove_mapping(self.exif_to_xml_table))
            exif_btn_layout.addWidget(add_exif_btn)
            exif_btn_layout.addWidget(remove_exif_btn)
            exif_layout.addLayout(exif_btn_layout)

            # Save and Cancel buttons
            button_box = QHBoxLayout()
            save_btn = QPushButton("Save")
            save_btn.clicked.connect(self.save_mapping)
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(self.reject)
            button_box.addWidget(save_btn)
            button_box.addWidget(cancel_btn)
            layout.addLayout(button_box)

        def load_mapping(self):
            """Load mapping from JSON file."""
            try:
                mapping_file = os.path.join(os.path.dirname(__file__), "metadata_mapping.json")
                if os.path.exists(mapping_file):
                    with open(mapping_file, 'r') as f:
                        self.mapping = json.load(f)
                else:
                    # Create default mapping if file doesn't exist
                    self.mapping = {
                        "xml_to_exif": {},
                        "exif_to_xml": {}
                    }
                
                # Populate XML to EXIF table
                self.xml_to_exif_table.setRowCount(len(self.mapping["xml_to_exif"]))
                for i, (xml_field, exif_field) in enumerate(self.mapping["xml_to_exif"].items()):
                    self.xml_to_exif_table.setItem(i, 0, QTableWidgetItem(xml_field))
                    self.xml_to_exif_table.setItem(i, 1, QTableWidgetItem(exif_field))

                # Populate EXIF to XML table
                self.exif_to_xml_table.setRowCount(len(self.mapping["exif_to_xml"]))
                for i, (exif_field, xml_field) in enumerate(self.mapping["exif_to_xml"].items()):
                    self.exif_to_xml_table.setItem(i, 0, QTableWidgetItem(exif_field))
                    self.exif_to_xml_table.setItem(i, 1, QTableWidgetItem(xml_field))

            except Exception as e:
                QMessageBox.warning(self, "Error", f"Error loading mapping: {str(e)}")

        def add_mapping(self, table):
            """Add a new row to the mapping table."""
            row = table.rowCount()
            table.setRowCount(row + 1)
            table.setItem(row, 0, QTableWidgetItem(""))
            table.setItem(row, 1, QTableWidgetItem(""))

        def remove_mapping(self, table):
            """Remove selected rows from the mapping table."""
            rows = set(item.row() for item in table.selectedItems())
            for row in sorted(rows, reverse=True):
                table.removeRow(row)

        def save_mapping(self):
            """Save mapping to JSON file."""
            try:
                # Get XML to EXIF mappings
                xml_to_exif = {}
                for row in range(self.xml_to_exif_table.rowCount()):
                    xml_field = self.xml_to_exif_table.item(row, 0)
                    exif_field = self.xml_to_exif_table.item(row, 1)
                    if xml_field and exif_field and xml_field.text() and exif_field.text():
                        xml_to_exif[xml_field.text()] = exif_field.text()

                # Get EXIF to XML mappings
                exif_to_xml = {}
                for row in range(self.exif_to_xml_table.rowCount()):
                    exif_field = self.exif_to_xml_table.item(row, 0)
                    xml_field = self.exif_to_xml_table.item(row, 1)
                    if exif_field and xml_field and exif_field.text() and xml_field.text():
                        exif_to_xml[exif_field.text()] = xml_field.text()

                # Save to file
                mapping_file = os.path.join(os.path.dirname(__file__), "metadata_mapping.json")
                with open(mapping_file, 'w') as f:
                    json.dump({
                        "xml_to_exif": xml_to_exif,
                        "exif_to_xml": exif_to_xml
                    }, f, indent=4)

                self.accept()
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Error saving mapping: {str(e)}")

    if __name__ == '__main__':
        try:
            print("Starting application...")
            app = QApplication(sys.argv)
            print("QApplication created successfully")
            
            # Check if QWebEngineView is available
            try:
                from PyQt6.QtWebEngineWidgets import QWebEngineView
                print("QWebEngineView imported successfully")
            except ImportError as e:
                print(f"Error importing QWebEngineView: {e}")
                print("Please install PyQt6-WebEngine by running:")
                print("pip install PyQt6-WebEngine")
                sys.exit(1)
            
            print("Creating main window...")
            window = MainWindow()
            print("Main window created successfully")
            
            print("Showing main window...")
            window.show()
            
            print("Entering main event loop...")
            sys.exit(app.exec())
        except Exception as e:
            print(f"Fatal error: {str(e)}")
            print("Traceback:")
            traceback.print_exc()
            sys.exit(1)
except Exception as e:
    print(f"Error during import/setup: {str(e)}")
    print("Traceback:")
    traceback.print_exc()
    sys.exit(1) 