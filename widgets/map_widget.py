"""Map widget for displaying and interacting with geographic data."""

import os
import tempfile
from typing import List, Tuple

from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

import folium

from config import DEFAULT_MAP_TILE_URL, DEFAULT_ZOOM_LEVEL

class MapHandler(QObject):
    """Handler for JavaScript-Python communication in the map widget."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent

    @pyqtSlot(float, float, float)
    def handleMapClick(self, lat: float, lon: float, radius: float):
        """Handle map click events from JavaScript.
        
        Args:
            lat: Clicked latitude
            lon: Clicked longitude
            radius: Current circle radius in meters
        """
        if self.parent:
            self.parent.selection_made.emit(lat, lon, radius)

class MapWidget(QWidget):
    """Interactive map widget using Leaflet."""
    
    selection_made = pyqtSignal(float, float, float)  # lat, lon, radius

    def __init__(self):
        """Initialize the map widget."""
        super().__init__()
        self.setup_ui()

    def setup_ui(self):
        """Set up the widget's UI."""
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
        """Returns the HTML template with the map content.
        
        Args:
            map_content: JavaScript code to be injected into the template
            
        Returns:
            Complete HTML document as string
        """
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
                var map = L.map('map').setView([0, 0], {DEFAULT_ZOOM_LEVEL});
                L.tileLayer('{DEFAULT_MAP_TILE_URL}', {{
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
        """Create the map centered at the given coordinates.
        
        Args:
            center: Tuple of (latitude, longitude) for map center
        """
        # Generate JavaScript to set the view
        map_content = f"map.setView([{center[0]}, {center[1]}], {DEFAULT_ZOOM_LEVEL});"
        
        # Set the HTML directly
        html = self.get_html_template(map_content)
        self.web_view.setHtml(html, QUrl("qrc:///"))

    def add_image_markers(self, coordinates: List[Tuple[float, float, str]]):
        """Add markers for images to the map.
        
        Args:
            coordinates: List of tuples (latitude, longitude, popup_text)
        """
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
            map.setView([{center_lat}, {center_lon}], {DEFAULT_ZOOM_LEVEL});
            {' '.join(markers_js)}
        """
        
        # Set the HTML directly
        html = self.get_html_template(map_content)
        self.web_view.setHtml(html, QUrl("qrc:///"))
        print(f"Added {len(coordinates)} markers to map")  # Debug print 

    def update_view_and_circle(self, lat: float, lon: float, radius_m: float):
        """Update map view to center on new coordinates and draw/update circle and marker."""
        js_command = f"""
            if (typeof map !== 'undefined') {{
                map.setView([{lat}, {lon}], map.getZoom() || {DEFAULT_ZOOM_LEVEL}); // Keep current zoom or use default
                if (centerMarker) {{
                    map.removeLayer(centerMarker);
                }}
                if (circle) {{
                    map.removeLayer(circle);
                }}
                centerMarker = L.marker([{lat}, {lon}]).addTo(map);
                currentRadius = {radius_m};
                circle = L.circle([{lat}, {lon}], {{
                    color: 'red',
                    fillColor: '#f03',
                    fillOpacity: 0.2,
                    radius: currentRadius
                }}).addTo(map);
                // Optionally, notify Python that an update occurred, similar to a map click
                // This helps keep the main window's state (like enabling search button) consistent.
                if (window.handler) {{
                    window.handler.handleMapClick({lat}, {lon}, currentRadius);
                }}
            }} else {{
                console.error('Map object not found for update_view_and_circle');
            }}
        """
        print(f"MapWidget: Executing JS for update_view_and_circle - Lat: {lat}, Lon: {lon}, Radius: {radius_m}")
        self.web_view.page().runJavaScript(js_command) 