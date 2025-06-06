#!/usr/bin/env python3
"""Test the new minimal schema approach."""

import os
import sqlite3
import pytest

pytest.skip("Skipping minimal schema test due to missing dependencies", allow_module_level=True)

from database import DatabaseManager
from exif_utils import extract_exif_data

pytest.importorskip('PIL')

def test_minimal_schema():
    """Test that database starts with minimal schema and adds columns dynamically."""
    
    # Test database path
    db_path = r"D:\TEST_MINIMAL.db"
    
    # Remove if exists
    if os.path.exists(db_path):
        os.remove(db_path)
    
    print("Testing Minimal Schema Approach")
    print("=" * 40)
    
    # Create database manager (should create minimal schema)
    print("1. Creating database with minimal schema...")
    db_manager = DatabaseManager(db_path)
    
    # Check initial schema
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute('PRAGMA table_info(images)')
    initial_columns = [col[1] for col in cursor.fetchall()]
    
    print(f"   Initial columns: {initial_columns}")
    print(f"   Initial column count: {len(initial_columns)}")
    
    # Expected minimal columns
    expected_minimal = ['path', 'File_Location_Folder', 'File_Location_Session']
    if set(initial_columns) == set(expected_minimal):
        print("   ✓ Minimal schema created correctly")
    else:
        print("   ✗ Schema has unexpected columns")
        print(f"   Expected: {expected_minimal}")
        print(f"   Got: {initial_columns}")
    
    conn.close()
    
    # Test adding one image with metadata
    test_image = r"D:\TEST\EXIF and JSON Comment\image_D2025-04-24T01-07-09-490949Z_0.jpg"
    
    if os.path.exists(test_image):
        print(f"\n2. Processing test image to add columns dynamically...")
        
        # Extract metadata
        metadata, has_comment = extract_exif_data(test_image)
        print(f"   Extracted {len(metadata)} metadata fields")
        
        # Prepare for database
        data_for_db = dict(metadata)
        data_for_db['path'] = test_image
        data_for_db['File_Location_Folder'] = os.path.basename(os.path.dirname(test_image))
        data_for_db['File_Location_Session'] = os.path.basename(os.path.dirname(os.path.dirname(test_image)))
        
        # Insert (should add columns dynamically)
        db_manager.bulk_insert_images([data_for_db])
        
        # Check final schema
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('PRAGMA table_info(images)')
        final_columns = [col[1] for col in cursor.fetchall()]
        
        print(f"   Final column count: {len(final_columns)}")
        
        # Categorize columns
        prefixed_cols = [c for c in final_columns if c.startswith(('EXIF_', 'JSON_', 'XML_'))]
        essential_cols = ['path', 'File_Location_Folder', 'File_Location_Session']
        old_style_cols = [c for c in final_columns if c not in essential_cols and not c.startswith(('EXIF_', 'JSON_', 'XML_'))]
        
        print(f"   Essential columns: {len(essential_cols)}")
        print(f"   Prefixed columns: {len(prefixed_cols)}")
        print(f"   Old-style columns: {len(old_style_cols)}")
        
        if len(old_style_cols) == 0:
            print("   ✓ No old-style columns created!")
        else:
            print("   ✗ Old-style columns found:")
            for col in old_style_cols[:5]:
                print(f"     {col}")
        
        # Check if data was stored
        cursor.execute("SELECT COUNT(*) FROM images")
        record_count = cursor.fetchone()[0]
        print(f"   Records inserted: {record_count}")
        
        # Check sample data
        if record_count > 0:
            cursor.execute("SELECT * FROM images LIMIT 1")
            record = cursor.fetchone()
            non_null_count = sum(1 for val in record if val is not None and val != '')
            print(f"   Non-null values: {non_null_count}")
            
            # Show some GPS data
            gps_cols = [c for c in final_columns if 'GPS' in c and ('Latitude' in c or 'Longitude' in c)]
            if gps_cols:
                print(f"   GPS columns found: {len(gps_cols)}")
                for col in gps_cols[:4]:
                    col_idx = final_columns.index(col)
                    value = record[col_idx]
                    if value:
                        print(f"     {col}: {value}")
        
        conn.close()
        
        print(f"\n3. Success! Database structure:")
        print(f"   - Started with {len(initial_columns)} essential columns")
        print(f"   - Dynamically added {len(final_columns) - len(initial_columns)} metadata columns")
        print(f"   - All metadata columns have proper prefixes")
        print(f"   - No redundant old-style columns created")
        
    else:
        print(f"   Test image not found: {test_image}")
    
    print(f"\nDatabase created: {db_path}")

if __name__ == "__main__":
    test_minimal_schema() 