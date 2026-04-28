import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from .config_manager import ConfigManager

# Create a logger instance
logger = logging.getLogger('UpstoxApp')
logger.setLevel(logging.DEBUG) # Set a default high level to capture everything

sr_logger = logging.getLogger('SR_Details')
sr_logger.setLevel(logging.INFO)

# Add a NullHandler to prevent "No handlers could be found" warnings
# if the logger is used before it's configured.
logger.addHandler(logging.NullHandler())

def configure_logger(config_manager: ConfigManager):
    """
    Configures the global logger instance with handlers and formatters
    based on the provided configuration. This should be called once at startup.
    """
    # Remove all existing handlers (like the NullHandler) to ensure a clean slate
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    log_file = config_manager.get('app', 'log_file', fallback='bot.log')
    log_level_str = config_manager.get('app', 'log_level', fallback='INFO')
    
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)


    # Create a rotating file handler for all logs
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    # Standard log level for file
    file_handler.setLevel(log_level)
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Create a minimal console handler for trade summary only
    # Note: Disabled during UI Backtests to avoid duplicate entries in the UI tail
    if os.environ.get('UI_BACKTEST_MODE') != 'True':
        class TradeSummaryFilter(logging.Filter):
            def filter(self, record):
                # Only allow records containing '[TRADE_SUMMARY]' in msg.
                # CRITICAL and above are intentionally NOT passed through here —
                # they are already captured by the rotating file_handler.
                # Passing them here caused double-logging on EC2 when stdout is
                # redirected to the same file as the file_handler.
                return '[TRADE_SUMMARY]' in record.getMessage()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(TradeSummaryFilter()) # Only allow trade summaries to console
        console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

    # Configure SR Details Logger
    sr_file_handler = RotatingFileHandler(
        'sr_details.log',
        maxBytes=10*1024*1024,
        backupCount=3
    )
    sr_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    sr_logger.addHandler(sr_file_handler)
    # Prevent SR logs from propagating to the main UpstoxApp logger
    sr_logger.propagate = False

    logger.info("Logger has been successfully configured.")

def log_sr_details(message: str):
    """Logs S&R details to the specific sr_details.log file."""
    sr_logger.info(message)

def log_trade_summary(summary: str):
    """
    Log trade summary to console only (not file).
    """
    # PERFORMANCE: Throttle console summary logging in UI backtests
    # to avoid flooding the TTY/Stdout which blocks the process.
    if os.environ.get('UI_BACKTEST_MODE') == 'True':
        if not hasattr(log_trade_summary, '_last_log_time'):
            log_trade_summary._last_log_time = 0

        import time
        now = time.monotonic()
        if (now - log_trade_summary._last_log_time) < 0.1: # 100ms throttle
            return
        log_trade_summary._last_log_time = now

    logger.info(f"[TRADE_SUMMARY] {summary}")
