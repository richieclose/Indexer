import os
import json
import sqlite3
import logging
import re
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog, QProgressBar, QTableWidget, QTableWidgetItem, QMessageBox, QCheckBox, QHeaderView, QTabWidget, QLineEdit, QSpinBox, QDoubleSpinBox, QSplitter, QGroupBox, QSlider, QComboBox, QScrollArea, QGridLayout, QDialog, QFormLayout, QTreeView, QButtonGroup, QRadioButton)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QObject, pyqtSlot, QTimer, QDir
from PyQt6.QtGui import QImage, QPixmap, QStandardItemModel, QStandardItem, QColor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtGui import QShortcut

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

class TagCache:
    """Cache mapping a ``tags.config`` file path to its parsed tag dictionary."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, str]] = {}

    def get(self, config_path: str) -> Dict[str, str]:
        """Return cached tags for ``config_path``, parsing if unseen."""
        if config_path not in self._cache:
            from exif_extractor_gui import parse_tags_config
            self._cache[config_path] = parse_tags_config(config_path)
        return self._cache[config_path]
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

def find_applicable_tags(image_path: str, tag_cache: Optional[TagCache] = None) -> Dict[str, str]:
    """Find all applicable tags for an image by walking up the directory tree.

    If ``tag_cache`` is provided, parsed ``tags.config`` files will be cached so
    repeated calls avoid re-reading the same files.
    """
    applicable_tags = {}
    
    # Start from the image's directory and walk up to root
    current_dir = os.path.dirname(os.path.abspath(image_path))
    
    # Collect tags from all levels (parent tags can be overridden by child tags)
    tags_stack = []
    
    while current_dir:
        config_path = os.path.join(current_dir, 'tags.config')
        if os.path.exists(config_path):
            if tag_cache is not None:
                level_tags = tag_cache.get(config_path)
            else:
                from exif_extractor_gui import parse_tags_config
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
    tag_cache = TagCache()

    for image_path in image_files:
        tags = find_applicable_tags(image_path, tag_cache)
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

