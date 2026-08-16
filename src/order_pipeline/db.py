from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy metadata root. Business tables live in `order_pipeline.models`."""
