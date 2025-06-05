"""Utility functions for handling EXIF data from images."""

from __future__ import annotations # For forward type references
import os
import json
import logging
import re # Added re for XML metadata extraction
from datetime import datetime
from fractions import Fraction
from typing import Dict, Any, Optional, Tuple, Union, List

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

from config import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_ALTITUDE, GPS_TIMESTAMP,
    GPS_DATE_STAMP, GPS_LATITUDE_REF, GPS_LONGITUDE_REF,
    GPS_ALTITUDE_REF
)

# PyQt6 imports for GUIUtils and other potential GUI helpers
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QCheckBox
from PyQt6.QtCore import Qt

# Constants for XML metadata keys
XML_KEY_GPS_LATITUDE = 'GPS_Latitude'
XML_KEY_GPS_LONGITUDE = 'GPS_Longitude'
XML_KEY_GPS_ALTITUDE = 'GPS_Altitude'
XML_KEY_DEPTH = 'Depth'
XML_KEY_PITCH = 'Pitch'
XML_KEY_ROLL = 'Roll'
XML_KEY_YAW = 'Yaw'
XML_KEY_CAPTURE_TIME = 'Capture_Time'

# Prefixes for grouped XML keys
XML_PREFIX_ACQUISITION = 'Acquisition_'
XML_PREFIX_VERSION = 'Version_'

# Configure logging
logger = logging.getLogger(__name__)

def parse_dms_string_to_dd(dms_string: str, is_latitude: bool) -> Optional[float]:
    """Convert a DMS string to decimal degrees.

    Args:
        dms_string: The string representing the coordinate in DMS or DD format.
        is_latitude: True if the coordinate is latitude, False for longitude.

    Returns:
        Decimal degrees as float, or None if parsing fails.
    """
    original_dms_string = dms_string
    dms_string = dms_string.strip()
    if not dms_string:
        return None

    hemisphere = None
    sign = 1.0

    # Check for explicit hemisphere at the end
    if dms_string and dms_string[-1].upper() in ['N', 'S', 'E', 'W']:
        hemisphere = dms_string[-1].upper()
        dms_string = dms_string[:-1].strip()

    # Check for leading sign
    if dms_string.startswith('-'):
        sign = -1.0
        dms_string = dms_string[1:].strip()
    elif dms_string.startswith('+'):
        dms_string = dms_string[1:].strip()

    # Replace common DMS symbols (degrees, minutes, seconds) and other non-numeric/non-dot chars (except -)
    # with spaces to help splitting. We specifically keep dots for decimal values.
    # This regex aims to isolate numeric parts.
    cleaned_string = re.sub(r"[^\d\s\.]", " ", dms_string) # Keep digits, spaces, dots.
    parts = [p for p in cleaned_string.split() if p]  # Split by space and remove empty strings

    degrees, minutes, seconds = 0.0, 0.0, 0.0

    try:
        if len(parts) == 1:
            degrees = float(parts[0])
        elif len(parts) == 2:
            degrees = float(parts[0])
            minutes = float(parts[1])
        elif len(parts) == 3:
            degrees = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
        else:
            # Fallback: if not 1, 2, or 3 numeric parts, try to parse the original (sign-processed) string as a single float.
            # This is for cases like a simple DD entry "40.123" or "-70.456".
            try:
                dd_val = float(dms_string) # Use the string after sign and hemisphere removal
                degrees = abs(dd_val) # Magnitude for DMS logic, sign is handled separately
                if dd_val < 0: sign = -1.0 # Capture sign if it was like -40.123
                # If we successfully parse as float, we assume it's DD, so minutes/seconds are 0
            except ValueError:
                logger.warning(f"Could not parse DMS string '{original_dms_string}': Unrecognized format. Parts: {parts}")
                return None

        # Validate DMS components if they were parsed as D, M, S
        if len(parts) > 1: # Only if we parsed minutes or seconds
            if not (0 <= minutes < 60 and 0 <= seconds < 60):
                logger.warning(f"Invalid minutes/seconds in DMS string '{original_dms_string}'. Min: {minutes}, Sec: {seconds}")
                return None
        
        # Degree magnitude check (absolute value, sign applied later)
        if is_latitude and not (0 <= degrees <= 90):
             logger.warning(f"Degree value {degrees} out of range [0, 90] for latitude in '{original_dms_string}'.")
             return None
        if not is_latitude and not (0 <= degrees <= 180):
             logger.warning(f"Degree value {degrees} out of range [0, 180] for longitude in '{original_dms_string}'.")
             return None

        dd = degrees + (minutes / 60.0) + (seconds / 3600.0)
        dd *= sign  # Apply sign from leading +/- (or if original dms_string was a negative DD)

        # Apply hemisphere (this can override the sign if both are present and conflicting)
        if hemisphere:
            if is_latitude:
                if hemisphere == 'S':
                    dd = -abs(dd)
                elif hemisphere == 'N':
                    dd = abs(dd)
                elif hemisphere not in ['N', 'S']:
                    logger.warning(f"Invalid hemisphere '{hemisphere}' for latitude in '{original_dms_string}'.")
                    return None
            else:  # Longitude
                if hemisphere == 'W':
                    dd = -abs(dd)
                elif hemisphere == 'E':
                    dd = abs(dd)
                elif hemisphere not in ['E', 'W']:
                    logger.warning(f"Invalid hemisphere '{hemisphere}' for longitude in '{original_dms_string}'.")
                    return None
        
        # Final range validation
        if is_latitude and not (-90 <= dd <= 90):
            logger.warning(f"Final latitude {dd:.6f} out of range [-90, 90] from DMS '{original_dms_string}'.")
            return None
        elif not is_latitude and not (-180 <= dd <= 180):
            logger.warning(f"Final longitude {dd:.6f} out of range [-180, 180] from DMS '{original_dms_string}'.")
            return None
            
        return dd

    except ValueError:  # Catch float conversion errors from parts
        logger.error(f"Could not parse numeric components of DMS string '{original_dms_string}' to float. Parts: {parts}")
        return None

def convert_to_degrees(value: Tuple[Union[int, float], ...]) -> Optional[float]:
    """Convert GPS coordinates to decimal degrees.
    
    Args:
        value: Tuple of (degrees, minutes, seconds)
        
    Returns:
        Decimal degrees as float, or None if conversion fails
    """
    try:
        d = float(value[0])
        m = float(value[1])
        s = float(value[2])
        return d + (m / 60.0) + (s / 3600.0)
    except (ValueError, TypeError, IndexError) as e:
        logger.error(f"Error converting GPS value {value}: {str(e)}")
        return None

def format_gps_timestamp(timestamp: Tuple[Union[int, float], ...]) -> str:
    """Format GPS timestamp into HH:MM:SS format.
    
    Args:
        timestamp: Tuple of (hours, minutes, seconds)
        
    Returns:
        Formatted timestamp string
    """
    try:
        if isinstance(timestamp, tuple) and len(timestamp) == 3:
            hours = int(float(timestamp[0]))
            minutes = int(float(timestamp[1]))
            seconds = int(float(timestamp[2]))
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except (ValueError, TypeError, IndexError) as e:
        logger.error(f"Error formatting GPS timestamp {timestamp}: {str(e)}")
    return str(timestamp)

def format_shutter_speed(value: Union[Fraction, float, int]) -> str:
    """Convert shutter speed to a readable format.
    
    Args:
        value: Shutter speed value
        
    Returns:
        Formatted shutter speed string
    """
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return str(value.numerator)
        if value < 1:
            return f"1/{int(1/float(value))}"
        return f"{value.numerator}/{value.denominator}"
    return str(value)

def get_gps_info(raw_exif_gps_ifd: Dict[int, Any]) -> Dict[str, str]:
    """
    Extracts GPS information from a pre-resolved GPS IFD dictionary.
    Args:
        raw_exif_gps_ifd: The dictionary containing GPS tags and values, 
                          typically obtained from exif.get(TAGS.getid('GPSInfo')).
                          Keys are numeric GPS tags (e.g., config.GPS_LATITUDE).
    Returns:
        Dictionary of processed GPS data with string keys (e.g., 'GPS_Latitude').
    """
    if not raw_exif_gps_ifd:
        logger.debug("get_gps_info called with empty or None raw_exif_gps_ifd")
        return {}

    gps_data = {}
    logger.debug(f"Processing raw GPS IFD: {raw_exif_gps_ifd}")
    try:
        # Latitude
        lat_val = raw_exif_gps_ifd.get(GPS_LATITUDE) # GPS_LATITUDE is numeric ID from config
        lat_ref = raw_exif_gps_ifd.get(GPS_LATITUDE_REF, 'N')
        if lat_val is not None: # Ensure value exists before type checking
            if isinstance(lat_val, tuple) and len(lat_val) == 3:
                lat = convert_to_degrees(lat_val)
                if lat is not None:
                    if lat_ref.upper() == 'S': # Standard is N/S/E/W
                        lat = -lat
                    gps_data['GPS_Latitude'] = f"{lat:.6f}"
            else:
                logger.warning(f"GPS_Latitude value not a valid tuple: {lat_val}")

        # Longitude
        lon_val = raw_exif_gps_ifd.get(GPS_LONGITUDE)
        lon_ref = raw_exif_gps_ifd.get(GPS_LONGITUDE_REF, 'E')
        if lon_val is not None:
            if isinstance(lon_val, tuple) and len(lon_val) == 3:
                lon = convert_to_degrees(lon_val)
                if lon is not None:
                    if lon_ref.upper() == 'W':
                        lon = -lon
                    gps_data['GPS_Longitude'] = f"{lon:.6f}"
            else:
                logger.warning(f"GPS_Longitude value not a valid tuple: {lon_val}")
        
        # Altitude
        alt_val = raw_exif_gps_ifd.get(GPS_ALTITUDE)
        # GPS_ALTITUDE_REF is a byte: 0 for above sea level, 1 for below sea level
        alt_ref_byte = raw_exif_gps_ifd.get(GPS_ALTITUDE_REF, 0) 
        if alt_val is not None:
            try:
                alt = float(alt_val) # Value is often a Fraction or float/int
                if isinstance(alt_val, Fraction):
                    alt = float(alt_val.numerator / alt_val.denominator)
                else:
                    alt = float(alt_val)

                if alt_ref_byte == 1: # Below sea level
                    alt = -alt
                gps_data['GPS_Altitude'] = f"{alt:.1f}m" # Standard format, e.g. "123.0m"
            except (ValueError, TypeError):
                 logger.warning(f"Could not parse GPS_Altitude value: {alt_val}")

        # Timestamp (UTC)
        ts_val = raw_exif_gps_ifd.get(GPS_TIMESTAMP)
        if ts_val is not None:
            if isinstance(ts_val, tuple) and len(ts_val) == 3:
                # Value is (HH, MM, SS) as Fractions or floats/ints
                gps_data['GPS_TimeStamp'] = format_gps_timestamp(ts_val) 
            else:
                logger.warning(f"GPS_TimeStamp value not a valid tuple: {ts_val}")

        # Datestamp (UTC)
        ds_val = raw_exif_gps_ifd.get(GPS_DATE_STAMP)
        if ds_val is not None: # Typically a string 'YYYY:MM:DD'
            gps_data['GPS_DateStamp'] = str(ds_val)
        else:
            logger.debug("GPS_DateStamp not found in GPS IFD.")

        # Combine date and time if both are available
        if 'GPS_DateStamp' in gps_data and 'GPS_TimeStamp' in gps_data:
            try:
                date_str = str(gps_data['GPS_DateStamp']).replace(':', '-') # Ensure YYYY-MM-DD
                time_str = gps_data['GPS_TimeStamp'] # Already HH:MM:SS from format_gps_timestamp
                
                # Validate before combining
                datetime.strptime(date_str, "%Y-%m-%d")
                datetime.strptime(time_str, "%H:%M:%S")
                gps_data['GPS_DateTime'] = f"{date_str} {time_str}"

            except ValueError as ve:
                logger.error(f"Error validating/combining GPS date '{gps_data.get('GPS_DateStamp')}' and time '{gps_data.get('GPS_TimeStamp')}': {str(ve)}")
                # Do not add GPS_DateTime if parts are invalid
            except Exception as e:
                 logger.error(f"Unexpected error combining GPS date and time: {str(e)}", exc_info=True)
        
    except Exception as e:
        logger.error(f"General error processing GPS data from IFD: {str(e)}", exc_info=True)

    logger.debug(f"Returning processed GPS data: {gps_data}")
    return gps_data

# XML metadata extraction functions - moved from exif_extractor_gui.py
def convert_to_float(value: Union[str, int, float, None]) -> Optional[float]:
    try:
        return float(value) if value is not None and value != '' else None
    except (ValueError, TypeError):
        return None

def extract_attributes(tag_str):
    """Returns a dictionary of attribute-value pairs."""
    return dict(re.findall(r'(\w+)="([^"]*)"', tag_str))

# Renamed and modified to accept comment string directly
def extract_xml_metadata_from_comment_string(comment: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract metadata from XML comment string."""
    if comment is None:
        return None
    try:
        # Comment is already decoded by the caller if it comes from img.info.get('comment')
        # If not, ensure it's a string.
        if isinstance(comment, bytes):
            comment_str = comment.decode('utf-8', 'ignore')
        else:
            comment_str = str(comment)

        logger.debug(f"Processing XML comment string: {comment_str[:200]}...")
        metadata = {}
        
        # Extract coordinates
        coords_match = re.search(r'<Coords\s+([^>]+?)/?>', comment_str)
        if coords_match:
            logger.debug("Found coordinates in XML")
            coords_attrs = extract_attributes(coords_match.group(1))
            metadata[XML_KEY_GPS_LATITUDE] = convert_to_float(coords_attrs.get('lat'))
            metadata[XML_KEY_GPS_LONGITUDE] = convert_to_float(coords_attrs.get('long'))
        
        # Extract depth and altitude
        depth_match = re.search(r'<Depth\s+([^>]+?)/?>', comment_str)
        if depth_match:
            logger.debug("Found depth/altitude in XML")
            depth_attrs = extract_attributes(depth_match.group(1))
            metadata[XML_KEY_GPS_ALTITUDE] = convert_to_float(depth_attrs.get('altitude'))
            metadata[XML_KEY_DEPTH] = convert_to_float(depth_attrs.get('depth'))
        
        # Extract direction
        direction_match = re.search(r'<Direction\s+([^>]+?)/?>', comment_str)
        if direction_match:
            logger.debug("Found direction data in XML")
            direction_attrs = extract_attributes(direction_match.group(1))
            metadata[XML_KEY_PITCH] = convert_to_float(direction_attrs.get('pitch'))
            metadata[XML_KEY_ROLL] = convert_to_float(direction_attrs.get('roll'))
            metadata[XML_KEY_YAW] = convert_to_float(direction_attrs.get('yaw'))
        
        # Extract acquisition data
        acq_match = re.search(r'<acquisition>(.*?)</acquisition>', comment_str, re.DOTALL)
        if acq_match:
            logger.debug("Found acquisition data in XML")
            acq_content = acq_match.group(1)
            for tag in ['exposure', 'digital_gain', 'analog_gain', 'sensor_gain', 'aperture', 'focus', 'name', 'camera_session_name']:
                tag_match = re.search(f'<{tag}>(.*?)</{tag}>', acq_content)
                if tag_match:
                    metadata[f'{XML_PREFIX_ACQUISITION}{tag}'] = tag_match.group(1)
        
        # Extract version and hardware information
        versions_match = re.search(r'<versions>(.*?)</versions>', comment_str, re.DOTALL)
        if versions_match:
            logger.debug("Found version information in XML")
            versions_content = versions_match.group(1)
            for tag in ['software', 'fpga', 'pic', 'serial_number']:
                tag_match = re.search(f'<{tag}>(.*?)</{tag}>', versions_content)
                if tag_match:
                    metadata[f'{XML_PREFIX_VERSION}{tag}'] = tag_match.group(1)
        
        # Extract image time and date
        img_search = re.search(r'<image\s+([^>]+?)/?>', comment_str)
        if img_search:
            logger.debug("Found image time/date in XML")
            img_attrs = extract_attributes(img_search.group(1))
            if 'time' in img_attrs and 'date' in img_attrs:
                metadata[XML_KEY_CAPTURE_TIME] = f"{img_attrs['date']} {img_attrs['time']}"
        
        logger.debug(f"Extracted XML metadata: {metadata}")
        return metadata if metadata else None
    except Exception as e:
        logger.error(f"Error extracting XML metadata from comment string: {str(e)}")
        return None

def extract_json_metadata_from_comment_string(comment: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract metadata from JSON comment string."""
    if comment is None:
        return None
    try:
        import json
        # Comment is already decoded by the caller if it comes from img.info.get('comment')
        if isinstance(comment, bytes):
            comment_str = comment.decode('utf-8', 'ignore')
        else:
            comment_str = str(comment)

        logger.debug(f"Processing JSON comment string: {comment_str[:200]}...")
        
        # Try to parse as JSON
        try:
            json_data = json.loads(comment_str)
        except json.JSONDecodeError:
            return None
        
        metadata = {}
        
        # Extract position data
        if 'position' in json_data:
            pos = json_data['position']
            metadata['GPS_Latitude'] = convert_to_float(pos.get('lat'))
            metadata['GPS_Longitude'] = convert_to_float(pos.get('long'))
            metadata['GPS_Altitude'] = convert_to_float(pos.get('altitude'))
            metadata['Depth'] = convert_to_float(pos.get('depth'))
            metadata['Roll'] = convert_to_float(pos.get('roll'))
            metadata['Pitch'] = convert_to_float(pos.get('pitch'))
            metadata['Yaw'] = convert_to_float(pos.get('yaw'))
            metadata['Position_Extrapolated'] = pos.get('extrapolated')
            metadata['Position_Time'] = pos.get('time')
            metadata['Position_Received'] = pos.get('received')
            metadata['Position_Age'] = pos.get('age')
            metadata['Transponder_ID'] = pos.get('transponder_id')
        
        # Extract acquisition data
        if 'acquisition' in json_data:
            acq = json_data['acquisition']
            metadata['Acquisition_exposure'] = acq.get('exposure')
            metadata['Acquisition_digital_gain'] = acq.get('digital_gain')
            metadata['Acquisition_analog_gain'] = acq.get('analog_gain')
            metadata['Acquisition_sensor_gain'] = acq.get('sensor_gain')
            metadata['Acquisition_aperture'] = acq.get('aperture')
            metadata['Acquisition_focus'] = acq.get('focus')
            metadata['Acquisition_name'] = acq.get('name')
            metadata['Acquisition_camera_session_name'] = acq.get('camera_session_name')
            metadata['Acquisition_camera_sub_session_name'] = acq.get('camera_sub_session_name')
            metadata['Acquisition_time'] = acq.get('time')
            metadata['Acquisition_seq'] = acq.get('seq')
            metadata['Acquisition_focus_enc'] = acq.get('focus_enc')
            metadata['Acquisition_width'] = acq.get('width')
            metadata['Acquisition_height'] = acq.get('height')
            metadata['Acquisition_seq_slot'] = acq.get('seq_slot')
            metadata['Acquisition_dequeue_time'] = acq.get('dequeue_time')
            metadata['Acquisition_estimated_range'] = acq.get('estimated_range')
        
        # Extract version information
        if 'versions' in json_data:
            vers = json_data['versions']
            metadata['Version_software'] = vers.get('software')
            metadata['Version_fpga'] = vers.get('fpga')
            metadata['Version_pic'] = vers.get('pic')
            metadata['Version_serial_number'] = vers.get('serial_number')
        
        # Extract NTP information
        if 'ntp' in json_data:
            ntp = json_data['ntp']
            metadata['NTP_ntpq'] = ntp.get('ntpq')
            metadata['NTP_state'] = ntp.get('state')
            metadata['NTP_sync_level'] = ntp.get('sync_level')
        
        # Extract errors, pps, slg
        metadata['Errors'] = json_data.get('errors')
        metadata['PPS'] = json_data.get('pps')
        metadata['SLG'] = json_data.get('slg')
        
        logger.debug(f"Extracted JSON metadata: {metadata}")
        return metadata if metadata else None
    except Exception as e:
        logger.error(f"Error extracting JSON metadata from comment string: {str(e)}")
        return None

def extract_clarity_xml_metadata_from_comment_string(comment: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract metadata from Clarity processing XML comment string."""
    if comment is None:
        return None
    try:
        # Comment is already decoded by the caller if it comes from img.info.get('comment')
        if isinstance(comment, bytes):
            comment_str = comment.decode('utf-8', 'ignore')
        else:
            comment_str = str(comment)

        logger.debug(f"Processing Clarity XML comment string: {comment_str[:200]}...")
        
        # Check if this is clarity-processing XML
        if '<clarity-processing' not in comment_str:
            return None
        
        metadata = {}
        
        # Extract clarity-processing attributes
        clarity_match = re.search(r'<clarity-processing\s+([^>]+)', comment_str)
        if clarity_match:
            clarity_attrs = extract_attributes(clarity_match.group(1))
            metadata['Clarity_Processing_Date'] = clarity_attrs.get('Date')
            metadata['Clarity_Processing_ImageName'] = clarity_attrs.get('ImageName')
            metadata['Clarity_Processing_Version'] = clarity_attrs.get('Version')
        
        # Extract Camera configuration - handle nested Camera tags properly
        # Find the Config section first  
        config_section = re.search(r'<Config>(.*?)</Config>', comment_str, re.DOTALL)
        if config_section:
            config_content = config_section.group(1)
            # Find the outer Camera section - this contains all camera fields including nested Camera UUID
            # We need to find the outermost Camera tags, not just the first match
            
            # Use a more sophisticated approach to handle nested tags
            # Find all camera fields directly within the config content
            camera_fields = [
                'Camera', 'Name', 'Model', 'SerialNumber', 'Firmware',
                'F', 'K1', 'K2', 'K3', 'P1', 'P2', 'Width', 'Height',
                'OffsetX', 'OffsetY'
            ]
            
            for field in camera_fields:
                # Look for each field directly in the config content
                field_match = re.search(f'<{field}>(.*?)</{field}>', config_content)
                if field_match:
                    value = field_match.group(1)
                    # Try to convert numeric values to float
                    if field in ['F', 'K1', 'K2', 'K3', 'P1', 'P2', 'OffsetX', 'OffsetY']:
                        metadata[f'Camera_{field}'] = convert_to_float(value)
                    elif field in ['Width', 'Height']:
                        try:
                            metadata[f'Camera_{field}'] = int(value)
                        except (ValueError, TypeError):
                            metadata[f'Camera_{field}'] = value
                    else:
                        metadata[f'Camera_{field}'] = value
            
        logger.debug(f"Extracted Clarity XML metadata: {metadata}")
        return metadata if metadata else None
    except Exception as e:
        logger.error(f"Error extracting Clarity XML metadata from comment string: {str(e)}")
        return None

def extract_exif_data(image_path: str) -> Tuple[Dict[str, Any], bool]:
    """Extract all possible metadata from an image and its sidecar files, with robust type conversion."""
    logger.debug(f"Starting metadata extraction for: {image_path}")
    
    final_metadata: Dict[str, Any] = {}
    has_comment_metadata = False
    sources_found: List[str] = []

    try:
        # 1. Extract and process EXIF from image itself
        with Image.open(image_path) as img:
            raw_exif_dict = img._getexif() # Dict with int keys, raw Pillow values
            comment_bytes = img.info.get('comment')

            if raw_exif_dict:
                logger.debug(f"Processing raw EXIF data for {image_path}...")
                processed_exif_for_prefixing = {} # Temp dict for EXIF_ keying
                for tag_id, raw_value in raw_exif_dict.items():
                    tag_name = TAGS.get(tag_id, None)
                    if not tag_name: continue
                    if isinstance(raw_value, bytes): continue # Skip binary data

                    processed_value_for_tag = None
                    if tag_name == 'GPSInfo':
                        if isinstance(raw_value, dict):
                            # get_gps_info should return string/float values directly usable
                            gps_data_dict = get_gps_info(raw_value) 
                            if gps_data_dict:
                                # These are already named correctly (e.g., GPS_Latitude), not generic tag_name
                                processed_exif_for_prefixing.update(gps_data_dict)
                        continue # Skip storing the raw GPSInfo dict itself
                    elif isinstance(raw_value, Fraction):
                        # Convert Fractions to string (e.g., "1/100" or "100")
                        if raw_value.denominator == 1:
                            processed_value_for_tag = str(raw_value.numerator)
                        else:
                            processed_value_for_tag = f"{raw_value.numerator}/{raw_value.denominator}"
                    elif isinstance(raw_value, tuple):
                        # Convert tuples of simple types to comma-separated string
                        # If tuple contains Fractions, they must be stringified first
                        try:
                            processed_value_for_tag = ", ".join(
                                (str(x.numerator) if x.denominator == 1 else f"{x.numerator}/{x.denominator}") if isinstance(x, Fraction) else str(x) 
                                for x in raw_value
                            )
                        except TypeError: # Fallback for non-stringable tuple contents
                            processed_value_for_tag = str(raw_value)
                    else:
                        # Default conversion for other types
                        processed_value_for_tag = str(raw_value)
                    
                    if processed_value_for_tag is not None:
                        processed_exif_for_prefixing[tag_name] = processed_value_for_tag
                
                if processed_exif_for_prefixing:
                    for k, v in processed_exif_for_prefixing.items():
                        final_metadata[f'EXIF_{k}'] = v
                    sources_found.append('EXIF')
                    logger.debug(f"Added {len(processed_exif_for_prefixing)} processed EXIF fields for {image_path}")

        # 2. Extract comment-based metadata
        comment_str = None
        if comment_bytes:
            try: comment_str = comment_bytes.decode('utf-8', 'ignore')
            except Exception as e: logger.error(f"Error decoding comment for {image_path}: {e}")

        if comment_str:
            json_comment_data = extract_json_metadata_from_comment_string(comment_str)
            if json_comment_data:
                has_comment_metadata = True; sources_found.append('JSON_Comment')
                for k, v_json in json_comment_data.items(): final_metadata[f'JSON_{k}'] = str(v_json) # Ensure str
                logger.debug(f"Added {len(json_comment_data)} JSON fields from comment for {image_path}")

            clarity_xml_data = extract_clarity_xml_metadata_from_comment_string(comment_str)
            if clarity_xml_data:
                has_comment_metadata = True; sources_found.append('XML_ClarityComment')
                for k, v_xml_c in clarity_xml_data.items(): final_metadata[f'XML_{k}'] = str(v_xml_c) # Ensure str
                logger.debug(f"Added {len(clarity_xml_data)} Clarity XML fields from comment for {image_path}")
            elif not clarity_xml_data: # Only try original XML if Clarity XML not found
                original_xml_data = extract_xml_metadata_from_comment_string(comment_str)
                if original_xml_data:
                    has_comment_metadata = True; sources_found.append('XML_OriginalComment')
                    for k, v_xml_o in original_xml_data.items(): final_metadata[f'XML_{k}'] = str(v_xml_o) # Ensure str
                    logger.debug(f"Added {len(original_xml_data)} Original XML fields from comment for {image_path}")
        
        # 3. Standardize known GPS altitude fields to numeric (float) from their string representations
        altitude_keys_to_process = ['EXIF_GPS_Altitude', 'JSON_GPS_Altitude', 'XML_GPS_Altitude']
        for alt_key in altitude_keys_to_process:
            if alt_key in final_metadata and final_metadata[alt_key] is not None:
                original_value_str = str(final_metadata[alt_key]) # Ensure it's a string first
                try:
                    cleaned_value_str = original_value_str.lower().replace('m', '').strip()
                    if cleaned_value_str: # Avoid float('') error
                        final_metadata[alt_key] = float(cleaned_value_str)
                    else:
                        final_metadata[alt_key] = None # Set to None if empty after cleaning
                except ValueError:
                    logger.warning(f"Could not convert altitude string '{original_value_str}' to float for {alt_key}. Setting to None.")
                    final_metadata[alt_key] = None
                except Exception as e:
                    logger.error(f"Unexpected error converting altitude '{original_value_str}' for {alt_key}: {e}. Setting to None.", exc_info=True)
                    final_metadata[alt_key] = None
        
        # 4. Safeguard: Double-check for any remaining Fraction objects (should be rare)
        # This loop is mostly a defensive measure. Primary conversion should happen during initial EXIF processing.
        for key, value in list(final_metadata.items()): # Use list(items()) if modifying dict during iteration
            if isinstance(value, Fraction):
                logger.warning(f"Safeguard: Converting remaining Fraction for key '{key}' from {value}")
                if value.denominator == 1: final_metadata[key] = str(value.numerator)
                else: final_metadata[key] = f"{value.numerator}/{value.denominator}"
            elif isinstance(value, tuple) and value and all(isinstance(x, Fraction) for x in value):
                logger.warning(f"Safeguard: Converting remaining tuple of Fractions for key '{key}'")
                final_metadata[key] = ", ".join(
                    (str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}") for f in value
                )

        # 5. Add Capture_Time (logic from previous version, ensure keys are correct)
        # This assumes EXIF_DateTime, XML_Capture_Time, JSON_Capture_Time are already in final_metadata with prefixes
        # It creates a new key 'EXIF_Capture_Time' as a standardized capture time field.
        # This seems a bit off, as 'Capture_Time' is a general field in the DB. Let's aim for a single 'Capture_Time'.
        # For now, retaining the logic that creates 'EXIF_Capture_Time' based on priority.
        # A better approach would be a single 'Capture_Time' field populated by priority.
        # Let's assume the database uses a general 'Capture_Time' and populate that.
        
        # Standardized Capture_Time field (without EXIF_ prefix for the generic field)
        # This will be added to final_metadata and thus to the DB if selected or dynamically added.
        std_capture_time = None
        if final_metadata.get('EXIF_DateTimeOriginal'): # Highest priority
            std_capture_time = final_metadata['EXIF_DateTimeOriginal']
        elif final_metadata.get('EXIF_DateTime'): # Next priority for EXIF
            std_capture_time = final_metadata['EXIF_DateTime']
        elif final_metadata.get('XML_Capture_Time'):
            std_capture_time = final_metadata['XML_Capture_Time']
        elif final_metadata.get('JSON_Capture_Time'): # Assuming JSON might provide this
            std_capture_time = final_metadata['JSON_Capture_Time']
        
        if std_capture_time:
            final_metadata['Capture_Time'] = str(std_capture_time) # Ensure it's a string
            logger.debug(f"Set standardized Capture_Time to: {std_capture_time}")

        # Debug logging of types before returning (as added in the previous step)
        logger.debug(f"Final check of types in final_metadata for {image_path} before returning:")
        for key, value in final_metadata.items():
            if isinstance(value, Fraction):
                logger.error(f"  UNEXPECTED FRACTION STILL PRESENT: Key: {key}, Type: {type(value)}, Value: {value}")
            elif isinstance(value, tuple) and value and all(isinstance(x, Fraction) for x in value):
                logger.error(f"  UNEXPECTED TUPLE OF FRACTIONS STILL PRESENT: Key: {key}, Type: {type(value)}, Value: {value}")
            # else: logger.debug(f"  Key: {key}, Type: {type(value)}") # Reduce verbosity for non-error cases

        if final_metadata:
            logger.info(f"Extracted metadata from {len(sources_found)} sources for {image_path}: {', '.join(sources_found)}")
            logger.info(f"Total fields extracted: {len(final_metadata)}")
            return final_metadata, has_comment_metadata
        else:
            logger.info(f"No processable metadata found for {image_path}. Returning empty dict.")
            return {}, False # Ensure this path returns an empty dict
            
    except FileNotFoundError:
        logger.error(f"Image file not found: {image_path}")
        return {}, False 
    except Exception as e:
        logger.error(f"Error extracting metadata from {image_path}: {str(e)}", exc_info=True)
        return {}, False

class GUIUtils:
    @staticmethod
    def create_table_checkbox_widget(checked: bool = False, parent: Optional[QWidget] = None) -> QWidget:
        """
        Creates a QWidget containing a single QCheckBox, centered,
        suitable for use in a QTableWidget cell.
        """
        checkbox = QCheckBox(parent)
        checkbox.setChecked(checked)
        
        checkbox_widget = QWidget(parent)
        checkbox_layout = QHBoxLayout(checkbox_widget)
        checkbox_layout.addWidget(checkbox)
        checkbox_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        checkbox_layout.setContentsMargins(0, 0, 0, 0)
        return checkbox_widget

# If EXIF related classes or functions are defined below, keep them.
# For example:
# class EXIFProcessor:
# ... 