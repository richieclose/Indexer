import os
import unittest

from database import DatabaseManager

class TestGetImagesInRadius(unittest.TestCase):
    def setUp(self):
        self.db_path = 'temp_test.db'
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = DatabaseManager(self.db_path)
        # Insert one valid record and one with blank GPS values
        images = [
            {
                'path': 'valid.jpg',
                'File_Location_Folder': 'folder',
                'File_Location_Session': 'session',
                'EXIF_GPS_Latitude': '10.0',
                'EXIF_GPS_Longitude': '20.0'
            },
            {
                'path': 'blank.jpg',
                'File_Location_Folder': 'folder',
                'File_Location_Session': 'session',
                'EXIF_GPS_Latitude': '',
                'EXIF_GPS_Longitude': ''
            }
        ]
        self.db.bulk_insert_images(images)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_blank_gps_not_returned(self):
        results = self.db.get_images_in_radius(
            lat=10.0,
            lon=20.0,
            lat_delta=1.0,
            lon_delta=1.0
        )
        paths = [r[0] for r in results]
        self.assertIn('valid.jpg', paths)
        self.assertNotIn('blank.jpg', paths)

if __name__ == '__main__':
    unittest.main()
