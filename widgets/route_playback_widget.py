"""Widget for playing back routes from geotagged images."""

import os
import tempfile
from datetime import datetime
from typing import List, Dict, Any, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QProgressBar, QTableWidget, QTableWidgetItem, QCheckBox,
    QHeaderView, QGroupBox, QSlider, QComboBox, QSpinBox,
    QSplitter, QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView

import folium
import plotly.graph_objects as go

from database import DatabaseManager
from config import DEFAULT_ZOOM_LEVEL
from geopy.distance import geodesic

class RoutePlaybackWidget(QWidget):
    """Widget for visualizing and playing back image routes."""
    
    def __init__(self):
        """Initialize the route playback widget."""
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
        """Set up the widget's UI."""
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
        self.files_list.setColumnCount(4)
        self.files_list.setHorizontalHeaderLabels(["Select", "File Path", "Capture Time", "Location"])
        self.files_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
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
        """Select and load database file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Database File",
            "",
            "SQLite Database (*.db)"
        )
        if file_path:
            self.db_path.setText(file_path)
            self.load_database_filters()

    def load_database_filters(self):
        """Load folder and session filters from database."""
        try:
            db_manager = DatabaseManager(self.db_path.text())
            
            # Load folders
            folders = db_manager.get_unique_folders()
            self.folder_combo.clear()
            self.folder_combo.addItem("All Folders")
            self.folder_combo.addItems(folders)
            
            # Load sessions
            sessions = db_manager.get_unique_sessions()
            self.session_combo.clear()
            self.session_combo.addItem("All Sessions")
            self.session_combo.addItems(sessions)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error loading filters: {str(e)}")

    def apply_filters(self):
        """Apply folder and session filters to the image list."""
        try:
            db_manager = DatabaseManager(self.db_path.text())
            
            # Get selected filters
            selected_folder = self.folder_combo.currentText()
            selected_session = self.session_combo.currentText()
            
            # Build query conditions
            conditions = []
            params = []
            
            if selected_folder != "All Folders":
                conditions.append("File_Location_Folder = ?")
                params.append(selected_folder)
            
            if selected_session != "All Sessions":
                conditions.append("File_Location_Session = ?")
                params.append(selected_session)
            
            # Execute query
            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                query = """
                    SELECT path, GPS_Latitude, GPS_Longitude, GPS_Altitude,
                           Capture_Time, File_Location_Folder, File_Location_Session
                    FROM images 
                    WHERE GPS_Latitude IS NOT NULL 
                    AND GPS_Longitude IS NOT NULL
                """
                
                if conditions:
                    query += " AND " + " AND ".join(conditions)
                
                query += " ORDER BY Capture_Time"
                
                cursor.execute(query, params)
                records = cursor.fetchall()
                
                # Update table
                self.files_list.setRowCount(len(records))
                for i, (path, lat, lon, alt, time, folder, session) in enumerate(records):
                    # Create checkbox
                    checkbox = QCheckBox()
                    checkbox_widget = QWidget()
                    checkbox_layout = QHBoxLayout(checkbox_widget)
                    checkbox_layout.addWidget(checkbox)
                    checkbox_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    checkbox_layout.setContentsMargins(0, 0, 0, 0)
                    
                    # Format location string
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
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error applying filters: {str(e)}")

    def toggle_all_selections(self, state: int):
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

    def load_selected_as_route(self):
        """Load selected images as a route."""
        try:
            selected_paths = []
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

            db_manager = DatabaseManager(self.db_path.text())
            
            for path in selected_paths:
                # Get image data from database
                image_data = db_manager.get_image_data(path)
                if not image_data:
                    continue
                
                try:
                    lat = float(image_data['GPS_Latitude'])
                    lon = float(image_data['GPS_Longitude'])
                    alt = float(image_data['GPS_Altitude'].replace('m', '')) if image_data.get('GPS_Altitude') else 0
                    
                    # Calculate distance from previous point
                    if prev_coords:
                        distance = geodesic(prev_coords, (lat, lon)).kilometers
                        total_distance += distance
                    prev_coords = (lat, lon)
                    
                    # Parse timestamp
                    timestamp = None
                    if image_data.get('Capture_Time'):
                        try:
                            timestamp = datetime.strptime(image_data['Capture_Time'], '%Y:%m:%d %H:%M:%S')
                        except ValueError:
                            try:
                                timestamp = datetime.strptime(image_data['Capture_Time'], '%Y-%m-%d %H:%M:%S')
                            except ValueError:
                                pass
                    
                    # Add to route data
                    self.route_data.append({
                        'latitude': lat,
                        'longitude': lon,
                        'altitude': alt,
                        'timestamp': timestamp,
                        'path': path,
                        'total_distance': total_distance
                    })
                    
                except (ValueError, TypeError) as e:
                    print(f"Error processing {path}: {str(e)}")
                    continue

            if self.route_data:
                # Sort route data by timestamp if available
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
            QMessageBox.critical(self, "Error", f"Error loading route: {str(e)}")

    def clear_selection(self):
        """Clear all selections and reset filters."""
        self.select_all_checkbox.setChecked(False)
        self.folder_combo.setCurrentText("All Folders")
        self.session_combo.setCurrentText("All Sessions")
        self.route_data = []
        
        # Clear creation flags
        if hasattr(self, 'map_created'):
            delattr(self, 'map_created')
        if hasattr(self, 'profile_created'):
            delattr(self, 'profile_created')
            
        # Clear the views
        self.map_widget.setHtml("")
        self.altitude_widget.setHtml("")
        self.update_display()

    def toggle_playback(self):
        """Toggle playback state."""
        self.is_playing = not self.is_playing
        self.play_button.setText("Pause" if self.is_playing else "Play")
        
        if self.is_playing:
            self.timer.start(100)  # Update every 100ms
        else:
            self.timer.stop()

    def change_speed(self, speed_text: str):
        """Change the playback speed."""
        self.playback_speed = float(speed_text.replace('x', ''))

    def reset_playback(self):
        """Reset playback to the beginning."""
        self.current_index = 0
        self.timeline_slider.setValue(0)
        self.update_display()

    def slider_changed(self, value: int):
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
            return

        try:
            # Only create the map once when route is first loaded
            if not hasattr(self, 'map_created'):
                # Create the map centered on the first point
                m = folium.Map(
                    location=[self.route_data[0]['latitude'], self.route_data[0]['longitude']],
                    zoom_start=DEFAULT_ZOOM_LEVEL
                )

                # Add the route line
                coordinates = [(point['latitude'], point['longitude']) for point in self.route_data]
                folium.PolyLine(
                    coordinates,
                    weight=3,
                    color='blue',
                    opacity=0.8
                ).add_to(m)

                # Add current position marker with a unique ID
                current = self.route_data[self.current_index]
                folium.Marker(
                    [current['latitude'], current['longitude']],
                    popup=f"Altitude: {current['altitude']}m<br>Time: {current['timestamp']}",
                    icon=folium.Icon(color='red'),
                    element_id='current_marker'
                ).add_to(m)

                # Inject JavaScript functions for marker updates
                js_code = """
                <script>
                var currentMarker = null;

                function initializeMarker() {
                    // Find marker by element ID
                    var markerElement = document.getElementById('current_marker');
                    if (markerElement) {
                        currentMarker = markerElement.__marker;
                    }
                }

                function updateMarkerPosition(lat, lng, altitude, timestamp) {
                    if (!currentMarker) {
                        initializeMarker();
                    }
                    if (currentMarker) {
                        currentMarker.setLatLng([lat, lng]);
                        currentMarker.setPopupContent(`Altitude: ${altitude}m<br>Time: ${timestamp}`);
                    }
                }

                // Initialize marker after map loads
                document.addEventListener('DOMContentLoaded', initializeMarker);
                </script>
                """
                m.get_root().html.add_child(folium.Element(js_code))

                # Save map to a temporary file
                temp_dir = tempfile.gettempdir()
                self.map_file = os.path.join(temp_dir, 'route_map.html')
                m.save(self.map_file)

                # Load the map file once
                self.map_widget.settings().setAttribute(
                    self.map_widget.settings().WebAttribute.LocalContentCanAccessRemoteUrls,
                    True
                )
                self.map_widget.settings().setAttribute(
                    self.map_widget.settings().WebAttribute.LocalContentCanAccessFileUrls,
                    True
                )
                self.map_widget.load(QUrl.fromLocalFile(self.map_file))
                
                # Set flag after initial load
                self.map_widget.loadFinished.connect(lambda ok: setattr(self, 'map_created', True))
            else:
                # Just update marker position using JavaScript
                current = self.route_data[self.current_index]
                timestamp_str = current['timestamp'].strftime('%H:%M:%S') if current['timestamp'] else '--:--:--'
                js = f"updateMarkerPosition({current['latitude']}, {current['longitude']}, {current['altitude']}, '{timestamp_str}');"
                self.map_widget.page().runJavaScript(js)

        except Exception as e:
            print(f"Error updating map: {str(e)}")

    def update_altitude_profile(self):
        """Update the altitude profile graph."""
        if not self.route_data:
            return

        try:
            # Create initial profile only once
            if not hasattr(self, 'profile_created'):
                # Create distance and altitude arrays
                distances = [point['total_distance'] for point in self.route_data]
                altitudes = [point['altitude'] for point in self.route_data]

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

                # Update layout with uirevision to maintain state
                fig.update_layout(
                    title='Altitude Profile',
                    xaxis_title='Distance (km)',
                    yaxis_title='Altitude (m)',
                    showlegend=False,
                    margin=dict(l=0, r=0, t=30, b=0),
                    uirevision='true'  # Maintain zoom/pan state
                )

                # Inject JavaScript function for marker updates
                js_code = """
                <script>
                function updateAltitudeMarker(distance, altitude) {
                    Plotly.restyle('altitude_plot', {
                        x: [[distance]],
                        y: [[altitude]]
                    }, [1]);  // Update second trace (marker)
                }
                </script>
                """

                # Save to temporary file
                temp_dir = tempfile.gettempdir()
                self.profile_file = os.path.join(temp_dir, 'altitude_profile.html')
                
                with open(self.profile_file, 'w', encoding='utf-8') as f:
                    html_content = fig.to_html(
                        include_plotlyjs='cdn',
                        full_html=True,
                        div_id='altitude_plot'
                    )
                    # Insert our JavaScript before the closing body tag
                    html_content = html_content.replace('</body>', f'{js_code}</body>')
                    f.write(html_content)

                # Load the profile once
                self.altitude_widget.settings().setAttribute(
                    self.altitude_widget.settings().WebAttribute.LocalContentCanAccessRemoteUrls,
                    True
                )
                self.altitude_widget.settings().setAttribute(
                    self.altitude_widget.settings().WebAttribute.LocalContentCanAccessFileUrls,
                    True
                )
                self.altitude_widget.load(QUrl.fromLocalFile(self.profile_file))
                
                # Set flag after initial load
                self.altitude_widget.loadFinished.connect(lambda ok: setattr(self, 'profile_created', True))
            else:
                # Just update marker position using JavaScript
                current = self.route_data[self.current_index]
                js = f"updateAltitudeMarker({current['total_distance']}, {current['altitude']});"
                self.altitude_widget.page().runJavaScript(js)

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
            diff = self.route_data[i]['altitude'] - self.route_data[i-1]['altitude']
            if diff > 0:
                elevation_gain += diff

        self.total_distance_label.setText(f"Total Distance: {total_distance:.1f} km")
        self.elevation_gain_label.setText(f"Elevation Gain: {elevation_gain:.1f} m")
        self.current_altitude_label.setText(f"Current Altitude: {current['altitude']:.1f} m")
        
        # Handle timestamp display
        if current['timestamp']:
            time_str = current['timestamp'].strftime('%H:%M:%S')
        else:
            time_str = "--:--:--"
        self.current_time_label.setText(f"Current Time: {time_str}") 