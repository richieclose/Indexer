"""Logging configuration for the EXIF Extractor application."""

import os
import logging
import logging.handlers
from datetime import datetime

def setup_logging(log_dir: str = None) -> None:
    """Set up logging configuration.
    
    Args:
        log_dir: Directory to store log files. If None, logs to console only.
    """
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    
    # Set up file handler if log directory is provided
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(
            log_dir,
            f'exif_extractor_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
        )
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    
    if log_dir:
        root_logger.addHandler(file_handler)
    
    # Create loggers for each module
    modules = ['exif_utils', 'database', 'workers', 'widgets']
    for module in modules:
        logger = logging.getLogger(module)
        logger.setLevel(logging.DEBUG)
        
    logging.info("Logging system initialized") 