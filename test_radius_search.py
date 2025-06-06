import unittest

from workers import RadiusSearchWorker

class TestRadiusSearchWorker(unittest.TestCase):
    def test_no_exception_at_pole(self):
        worker = RadiusSearchWorker('TestDB/TEST.db', 90.0, 0.0, 10)
        try:
            worker.run()
        except Exception as e:
            self.fail(f"Worker raised exception: {e}")

if __name__ == '__main__':
    unittest.main()
