from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, SQLModel, create_engine

from src.config import settings

DATABASE_URL = settings.DATABASE_URL


def build_engine(database_url: str | None = None):
    url = database_url or DATABASE_URL
    return create_engine(url, echo=False)


engine = build_engine(DATABASE_URL)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def db_init():
    create_db_and_tables()


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]