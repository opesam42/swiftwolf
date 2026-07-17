from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, SQLModel, create_engine

from src.config import settings

# SQLModel.metadata only knows about a table once its model class has been
# imported somewhere — importing here (rather than relying on main.py/routes.py
# or a job script to have done it first) guarantees create_db_and_tables()
# always sees every table, regardless of which entrypoint calls it.
from src.core import models  # noqa: F401

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