from typing import Annotated

from fastapi import Depends
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from src.config import settings

from src.core import models  # noqa: F401

DATABASE_URL = settings.DATABASE_URL


def build_engine(database_url: str | None = None):
    url = database_url or DATABASE_URL
    return create_engine(url, echo=False)


engine = build_engine(DATABASE_URL)


@event.listens_for(engine, "connect")
def set_search_path(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET search_path TO public")
    cursor.close()


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def db_init():
    create_db_and_tables()


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]