from sqlmodel import Session

from app.config import Settings
from app.db import create_db_engine, init_db
from app.rag import RagIndex, backfill_sources


def main() -> None:
    settings = Settings()
    engine = create_db_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        indexed = backfill_sources(session, RagIndex.from_settings(settings))
        session.commit()
    print(f"indexed_sources={indexed}")


if __name__ == "__main__":
    main()
