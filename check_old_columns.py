#!/usr/bin/env python3
"""Analyze old vs new columns in the database."""

import sqlite3

def analyze_database_columns():
    """Analyze the database column structure and data distribution."""
    db_path = r'D:\TEST.db'
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get all columns
        cursor.execute('PRAGMA table_info(images)')
        columns = [col[1] for col in cursor.fetchall()]
        
        # Categorize columns
        prefixed_cols = [c for c in columns if c.startswith(('EXIF_', 'JSON_', 'XML_'))]
        path_cols = ['path', 'File_Location_Folder', 'File_Location_Session']
        old_cols = [c for c in columns if not c.startswith(('EXIF_', 'JSON_', 'XML_')) and c not in path_cols]
        
        print(f'Database Column Analysis')
        print(f'=' * 50)
        print(f'Total columns: {len(columns)}')
        print(f'Prefixed columns (new): {len(prefixed_cols)}')
        print(f'Path/location columns: {len(path_cols)}')
        print(f'Old non-prefixed columns: {len(old_cols)}')
        
        # Check total records
        cursor.execute('SELECT COUNT(*) FROM images')
        total_records = cursor.fetchone()[0]
        print(f'Total records: {total_records}')
        
        # Check if old columns have any data
        if old_cols:
            print(f'\nOld non-prefixed columns (should be empty):')
            empty_old_cols = []
            non_empty_old_cols = []
            
            for col in old_cols:
                cursor.execute(f'SELECT COUNT(*) FROM images WHERE "{col}" IS NOT NULL AND "{col}" != ""')
                count = cursor.fetchone()[0]
                if count == 0:
                    empty_old_cols.append(col)
                else:
                    non_empty_old_cols.append((col, count))
                print(f'  {col}: {count} non-empty records')
            
            print(f'\nSummary:')
            print(f'  Empty old columns: {len(empty_old_cols)}')
            print(f'  Non-empty old columns: {len(non_empty_old_cols)}')
            
            if empty_old_cols:
                print(f'\nEmpty old columns that could be removed:')
                for col in empty_old_cols[:10]:  # Show first 10
                    print(f'  {col}')
                if len(empty_old_cols) > 10:
                    print(f'  ... and {len(empty_old_cols) - 10} more')
        
        # Show sample of prefixed columns with data
        if prefixed_cols:
            print(f'\nSample of new prefixed columns with data:')
            for col in prefixed_cols[:10]:
                cursor.execute(f'SELECT COUNT(*) FROM images WHERE "{col}" IS NOT NULL AND "{col}" != ""')
                count = cursor.fetchone()[0]
                print(f'  {col}: {count} records')
        
        conn.close()
        
    except Exception as e:
        print(f'Error analyzing database: {e}')

if __name__ == "__main__":
    analyze_database_columns() 