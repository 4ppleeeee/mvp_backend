import argparse

from sqlmodel import Session

from app.config import Settings
from app.db import create_db_engine, init_db
from app.rag import RagIndex, backfill_sources


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill TripGuard evidence RAG index")
    parser.parse_args(argv)

    settings = Settings()
    engine = create_db_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        indexed = backfill_sources(session, RagIndex.from_settings(settings))
        session.commit()
    print(f"indexed_sources={indexed}")


if __name__ == "__main__":
    main()
