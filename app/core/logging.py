"""Logging setup."""

import logging


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Keep SQL noise out of the logs unless someone turns it on on purpose.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
