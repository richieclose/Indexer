from workers import RadiusSearchWorker


def radius_search(db_path: str, latitude: float, longitude: float, radius: float, **kwargs):
    """Run a radius search synchronously using ``RadiusSearchWorker``."""
    worker = RadiusSearchWorker(db_path, latitude, longitude, radius, **kwargs)
    worker.run()
    return worker
